"""Shared helpers for the app-per-agent (M2M) lab suite."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from mcp_client import Client

E2E = Path(__file__).resolve().parents[1]
LOGS = E2E / "logs"
LOGS.mkdir(exist_ok=True)

os.environ.setdefault("GATEWAY", "http://127.0.0.1:4200")
os.environ.setdefault("LOGIN", "http://127.0.0.1:8281")
os.environ.setdefault("GRAPH", "http://127.0.0.1:8282")
os.environ.setdefault("SOFTERIA", "http://127.0.0.1:3200")
os.environ.setdefault("DATABASE_URL", "postgresql://litellm:litellm-local@127.0.0.1:5435/litellm")
os.environ.setdefault("MASTER_KEY", "sk-local-master")
os.environ.setdefault("GRAPH_LOG_FILE", str(LOGS / "graph.jsonl"))
os.environ.setdefault("LOGIN_LOG_FILE", str(LOGS / "login.jsonl"))
os.environ.setdefault("AUDIT_LOG", str(LOGS / "audit.log"))
os.environ.setdefault("SOFTERIA_INTERNAL", "http://softeria-m365-mcp:3000")
os.environ.setdefault("LOGIN_INTERNAL", "http://microsoft-login-stand-in:8080")
os.environ.setdefault("CONNECTION_POLICY", str(E2E / "litellm" / "connection_policy.yaml"))

GATEWAY = os.environ["GATEWAY"].rstrip("/")
LOGIN = os.environ["LOGIN"].rstrip("/")
GRAPH = os.environ["GRAPH"].rstrip("/")
SOFTERIA = os.environ["SOFTERIA"].rstrip("/")
MASTER = os.environ["MASTER_KEY"]
SOFTERIA_URL = os.environ["SOFTERIA_INTERNAL"].rstrip("/") + "/mcp"
LOGIN_INTERNAL = os.environ["LOGIN_INTERNAL"].rstrip("/")
POLICY_FILE = Path(os.environ["CONNECTION_POLICY"])

# Entra app stand-in credentials per MCP alias
APP_CREDS = {
    "m365_a": ("app-agent-a", "secret-a"),
    "m365_b": ("app-agent-b", "secret-b"),
}


def mcp_url(path: str) -> str:
    return f"{GATEWAY}/{path}/mcp"


def http_json(method: str, url: str, payload: dict | None = None, token: str | None = None, timeout: int = 30):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:1500]}
        return exc.code, body


def _reachable(url: str, accept) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return accept(response.status)
    except urllib.error.HTTPError as exc:
        return accept(exc.code)
    except Exception:
        return False


def wait_ready() -> None:
    checks = (
        (GATEWAY + "/health/liveliness", lambda status: status == 200),
        (LOGIN + "/health", lambda status: status == 200),
        (GRAPH + "/health", lambda status: status == 200),
        (SOFTERIA + "/mcp", lambda status: status < 500),
    )
    deadline = time.time() + 180
    pending = list(checks)
    while time.time() < deadline and pending:
        pending = [item for item in pending if not _reachable(item[0], item[1])]
        if pending:
            time.sleep(2)
    if pending:
        names = ", ".join(url for url, _ in pending)
        raise RuntimeError(f"lab is not reachable: {names}")


def load_policy() -> dict:
    with open(POLICY_FILE, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise RuntimeError("connection_policy.yaml must be a mapping")
    return data


def policy_tool_names(entry: dict) -> list[str] | None:
    if entry.get("allow_all") is True:
        return None
    return [name for name, value in entry.items() if name != "allow_all" and isinstance(value, dict)]


def graph_log() -> list[dict]:
    path = os.environ.get("GRAPH_LOG_FILE", "/logs/graph.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def login_log() -> list[dict]:
    path = os.environ.get("LOGIN_LOG_FILE", "/logs/login.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def reset_graph_log() -> None:
    http_json("POST", GRAPH + "/_control", {"reset_log": True, "clear": True})


def control_graph(payload: dict) -> None:
    status, body = http_json("POST", GRAPH + "/_control", payload)
    if status >= 400:
        raise RuntimeError(f"graph control failed: {status} {body}")


def server_payload(path: str, allowed: list[str] | None, allow_all: bool = False) -> dict:
    client_id, client_secret = APP_CREDS[path]
    payload = {
        "server_name": path,
        "alias": path,
        "url": SOFTERIA_URL,
        "transport": "http",
        "auth_type": "oauth2",
        "oauth2_flow": "client_credentials",
        "per_server_oauth_discovery": False,
        # LiteLLM stores M2M secrets in the credentials blob (not top-level fields).
        "credentials": {
            "client_id": client_id,
            "client_secret": client_secret,
        },
        "token_url": LOGIN_INTERNAL + "/token",
        "scopes": ["https://graph.microsoft.com/.default"],
    }
    if not allow_all:
        payload["allowed_tools"] = allowed or []
    return payload


def seed_servers() -> None:
    policy = load_policy()
    existing_status, existing = http_json("GET", GATEWAY + "/v1/mcp/server", token=MASTER)
    if existing_status >= 400:
        raise RuntimeError(f"cannot list MCP servers: {existing_status} {existing}")
    have = {}
    servers = existing if isinstance(existing, list) else existing.get("data") or existing.get("servers") or []
    if isinstance(existing, dict) and not servers:
        servers = existing.get("mcp_servers") or []
    for item in servers:
        if isinstance(item, dict):
            have[item.get("server_name") or item.get("alias") or ""] = item.get("server_id")
    for path, entry in policy.items():
        if not isinstance(entry, dict):
            continue
        if path not in APP_CREDS:
            continue
        tools = policy_tool_names(entry)
        payload = server_payload(path, tools, allow_all=tools is None)
        server_id = have.get(path)
        if server_id:
            payload["server_id"] = server_id
            status, body = http_json("PUT", GATEWAY + "/v1/mcp/server", payload, MASTER)
        else:
            status, body = http_json("POST", GATEWAY + "/v1/mcp/server", payload, MASTER)
        if status >= 400:
            raise RuntimeError(f"seeding {path} failed {status}: {body}")


def mint_key(alias: str, mcp_servers: list[str]) -> str:
    status, body = http_json(
        "POST",
        GATEWAY + "/key/generate",
        {
            "key_alias": alias,
            "user_id": alias,
            "models": [],
            "object_permission": {"mcp_servers": mcp_servers},
        },
        MASTER,
    )
    if status >= 400:
        raise RuntimeError(f"key/generate failed {status}: {body}")
    key = body.get("key") or body.get("token") or (body.get("data") or {}).get("key")
    if not key:
        raise RuntimeError(f"key/generate returned no key: {body}")
    return str(key)


def open_session(client: Client, path: str) -> None:
    opened = client.mcp(
        mcp_url(path),
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "app-per-agent-suite", "version": "1.0"},
        },
    )
    if opened.get("http_status", 500) >= 400:
        raise RuntimeError(f"initialize {path} failed: {opened}")
    client.mcp(mcp_url(path), "notifications/initialized", {})


def call_tool(client: Client, path: str, name: str, arguments: dict) -> dict:
    return client.mcp(
        mcp_url(path),
        "tools/call",
        {"name": name, "arguments": arguments},
        rpc_id=2,
    )


def tool_names(client: Client, path: str) -> list[str]:
    listed = client.mcp(mcp_url(path), "tools/list", {}, rpc_id=3)
    body = listed.get("body") or {}
    result = body.get("result") or {}
    tools = result.get("tools") or []
    return [tool.get("name", "") for tool in tools]


def graph_count() -> int:
    return len(graph_log())


def send_mail_arguments(address: str, subject: str, content: str) -> dict:
    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": content},
        "toRecipients": [{"emailAddress": {"address": address}}],
    }
    return {"body": {"message": message, "saveToSentItems": False}}


def call_denied(result: dict) -> bool:
    if result.get("http_status", 200) >= 400:
        return True
    body = result.get("body") if isinstance(result.get("body"), dict) else {}
    if body.get("error"):
        return True
    inner = body.get("result") if isinstance(body.get("result"), dict) else {}
    if inner.get("isError") is True:
        return True
    return "rejected by connection policy" in json.dumps(result)
