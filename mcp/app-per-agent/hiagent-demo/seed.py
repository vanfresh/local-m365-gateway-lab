#!/usr/bin/env python3
"""Register m365_a / m365_b as M2M MCP servers; mint two virtual keys.

Hi Agent auth is Bearer virtual key per agent. LiteLLM holds Entra app
client_id/secret and refreshes Graph tokens via client_credentials.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
POLICY_FILE = ROOT / "connection_policy.yaml"
KEY_A_FILE = ROOT / ".agent-a-virtual-key"
KEY_B_FILE = ROOT / ".agent-b-virtual-key"
GATEWAY = os.environ.get("GATEWAY", "http://127.0.0.1:4300").rstrip("/")
MASTER = os.environ.get("MASTER_KEY", "sk-local-master")
SOFTERIA_URL = os.environ.get("SOFTERIA_INTERNAL", "http://softeria-m365-mcp:3000").rstrip("/") + "/mcp"
LOGIN_TOKEN = os.environ.get("LOGIN_TOKEN_URL", "http://microsoft-login-stand-in:8080/token")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:4300").rstrip("/")

APP_CREDS = {
    "m365_a": ("app-agent-a", "secret-a"),
    "m365_b": ("app-agent-b", "secret-b"),
}


def http_json(method: str, url: str, payload: dict | None = None, token: str | None = None, timeout: int = 30):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:500]}
        return exc.code, body


def load_policy() -> dict:
    data = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit("connection_policy.yaml must be a mapping")
    return data


def tool_names(entry: dict) -> list[str]:
    return [name for name, value in entry.items() if name != "allow_all" and isinstance(value, dict)]


def wait_gateway(seconds: int = 120) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            status, _ = http_json("GET", GATEWAY + "/health/liveliness")
            if status < 400:
                return
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit(f"LiteLLM not ready at {GATEWAY}")


def list_servers() -> dict[str, str]:
    status, existing = http_json("GET", GATEWAY + "/v1/mcp/server", token=MASTER)
    if status >= 400:
        raise SystemExit(f"cannot list MCP servers: {status} {existing}")
    servers = existing if isinstance(existing, list) else existing.get("data") or existing.get("servers") or []
    if isinstance(existing, dict) and not servers:
        servers = existing.get("mcp_servers") or []
    have: dict[str, str] = {}
    for item in servers:
        if isinstance(item, dict):
            name = item.get("server_name") or item.get("alias") or ""
            sid = item.get("server_id")
            if name and sid:
                have[name] = sid
    return have


def payload_for(path: str, tools: list[str]) -> dict:
    client_id, client_secret = APP_CREDS[path]
    return {
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
        "token_url": LOGIN_TOKEN,
        "scopes": ["https://graph.microsoft.com/.default"],
        "allowed_tools": tools,
    }


def delete_keys_with_alias(alias: str) -> None:
    status, body = http_json("GET", GATEWAY + "/key/list?return_full_object=true", token=MASTER)
    if status >= 400:
        print(f"key list skipped ({status})")
        return
    keys = body if isinstance(body, list) else body.get("keys") or body.get("data") or []
    for item in keys:
        if not isinstance(item, dict):
            continue
        if item.get("key_alias") != alias and item.get("alias") != alias:
            continue
        token_id = item.get("token") or item.get("key_name") or item.get("key")
        if not token_id:
            continue
        del_status, del_body = http_json("POST", GATEWAY + "/key/delete", {"keys": [token_id]}, MASTER)
        print(f"deleted prior key alias={alias} status={del_status} {del_body}")


def generate_virtual_key(alias: str, mcp_servers: list[str], key_file: Path) -> str:
    delete_keys_with_alias(alias)
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
        raise SystemExit(f"key/generate failed {status}: {body}")
    key = body.get("key") or body.get("token") or (body.get("data") or {}).get("key")
    if not key or not str(key).startswith("sk-"):
        raise SystemExit(f"key/generate returned no sk- key: {body}")
    key_file.write_text(str(key).strip() + "\n", encoding="utf-8")
    return str(key).strip()


def smoke_tools_list(path: str, key: str) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "app-per-agent-seed", "version": "0"},
        },
    }
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {key}",
        "MCP-Protocol-Version": "2025-03-26",
    }
    request = urllib.request.Request(
        f"{GATEWAY}/{path}/mcp", data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            status = response.status
            session = response.headers.get("mcp-session-id")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"smoke initialize {path} failed {exc.code}: {exc.read()[:300]!r}") from exc
    if status >= 400:
        raise SystemExit(f"smoke initialize {path} http={status}")
    # tools/list
    listed = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    headers2 = dict(headers)
    if session:
        headers2["mcp-session-id"] = session
    request2 = urllib.request.Request(
        f"{GATEWAY}/{path}/mcp",
        data=json.dumps(listed).encode(),
        headers=headers2,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request2, timeout=60) as response:
            raw = response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"smoke tools/list {path} failed {exc.code}: {exc.read()[:300]!r}") from exc
    if "list-mail-folder-messages" not in raw and "send-mail" not in raw:
        raise SystemExit(f"smoke tools/list {path} unexpected: {raw[:400]}")
    print(f"smoke {path}: tools/list OK")


def smoke_cross_denied(path: str, key: str) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "cross-deny", "version": "0"},
        },
    }
    request = urllib.request.Request(
        f"{GATEWAY}/{path}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raise SystemExit(f"expected grant denial for {path}, got {response.status}")
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise SystemExit(f"cross-deny {path}: expected 401/403, got {exc.code}")
        print(f"smoke cross-deny {path}: HTTP {exc.code} OK")


def main() -> None:
    wait_gateway()
    policy = load_policy()
    have = list_servers()
    seeded: list[str] = []
    for path, entry in policy.items():
        if path not in APP_CREDS or not isinstance(entry, dict):
            continue
        tools = tool_names(entry)
        body = payload_for(path, tools)
        server_id = have.get(path)
        if server_id:
            body["server_id"] = server_id
            status, result = http_json("PUT", GATEWAY + "/v1/mcp/server", body, MASTER)
        else:
            status, result = http_json("POST", GATEWAY + "/v1/mcp/server", body, MASTER)
        if status >= 400:
            raise SystemExit(f"seeding {path} failed {status}: {result}")
        print(f"seeded {path}: client_id={APP_CREDS[path][0]} tools={tools}")
        seeded.append(path)

    key_a = generate_virtual_key("agent-a", ["m365_a"], KEY_A_FILE)
    key_b = generate_virtual_key("agent-b", ["m365_b"], KEY_B_FILE)
    print(f"agent_a_virtual_key={key_a}")
    print(f"agent_b_virtual_key={key_b}")
    smoke_tools_list("m365_a", key_a)
    smoke_tools_list("m365_b", key_b)
    smoke_cross_denied("m365_b", key_a)
    smoke_cross_denied("m365_a", key_b)
    print(f"public_base={PUBLIC_BASE}")
    print("ok")


if __name__ == "__main__":
    main()
