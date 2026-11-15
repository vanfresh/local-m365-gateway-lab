#!/usr/bin/env python3
"""Register m365, agent_a, and agent_b for user OAuth (authorization_code).

Does not prime Graph tokens. Hi Agent (or any MCP OAuth client) must complete
browser login. A virtual key is still minted for LiteLLM grants / UI only —
not the intended Hi Agent plugin auth.
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
KEY_FILE = ROOT / ".hiagent-virtual-key"
KEY_ALIAS = "hiagent-demo"
KEY_USER_ID = "hiagent-demo"
GATEWAY = os.environ.get("GATEWAY", "http://127.0.0.1:4100").rstrip("/")
MASTER = os.environ.get("MASTER_KEY", "sk-local-master")
SOFTERIA_URL = os.environ.get("SOFTERIA_INTERNAL", "http://softeria-m365-mcp:3000").rstrip("/") + "/mcp"
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:4100").rstrip("/")
LOGIN_AUTHORIZE = os.environ.get("LOGIN_AUTHORIZE_URL", PUBLIC_BASE + "/login/authorize")
LOGIN_TOKEN = os.environ.get("LOGIN_TOKEN_URL", "http://microsoft-login-stand-in:8080/token")
MCP_ALIASES = ("m365", "agent_a", "agent_b")


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
            return response.status, json.loads(raw) if raw else {}, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:500]}
        return exc.code, body, dict(exc.headers)


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
            status, _, _ = http_json("GET", GATEWAY + "/health/liveliness")
            if status < 400:
                return
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit(f"LiteLLM not ready at {GATEWAY}")


def list_servers() -> dict[str, str]:
    status, existing, _ = http_json("GET", GATEWAY + "/v1/mcp/server", token=MASTER)
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
    return {
        "server_name": path,
        "alias": path,
        "url": SOFTERIA_URL,
        "transport": "http",
        "auth_type": "oauth2",
        "oauth2_flow": "authorization_code",
        "per_server_oauth_discovery": False,
        "client_id": "litellm-gateway",
        "client_secret": "litellm-gateway-secret",
        "authorization_url": LOGIN_AUTHORIZE,
        "token_url": LOGIN_TOKEN,
        "scopes": ["openid", "offline_access", "Mail.Read", "Mail.Send", "Sites.Read.All"],
        "allowed_tools": tools,
    }


def delete_keys_with_alias(alias: str) -> None:
    status, body, _ = http_json("GET", GATEWAY + "/key/list?return_full_object=true", token=MASTER)
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
        del_status, del_body, _ = http_json(
            "POST", GATEWAY + "/key/delete", {"keys": [token_id]}, MASTER
        )
        print(f"deleted prior key alias={alias} status={del_status} {del_body}")


def generate_virtual_key(aliases: list[str]) -> str:
    """Optional LiteLLM grant key — not the intended Hi Agent plugin auth."""
    delete_keys_with_alias(KEY_ALIAS)
    status, body, _ = http_json(
        "POST",
        GATEWAY + "/key/generate",
        {
            "key_alias": KEY_ALIAS,
            "user_id": KEY_USER_ID,
            "models": [],
            "object_permission": {"mcp_servers": aliases},
        },
        MASTER,
    )
    if status >= 400:
        raise SystemExit(f"key/generate failed {status}: {body}")
    key = body.get("key") or body.get("token") or (body.get("data") or {}).get("key")
    if not key or not str(key).startswith("sk-"):
        raise SystemExit(f"key/generate returned no sk- key: {body}")
    KEY_FILE.write_text(str(key).strip() + "\n", encoding="utf-8")
    return str(key).strip()


def smoke_oauth_challenge(path: str, key: str) -> None:
    """Without a stored Graph credential, MCP should 401 with WWW-Authenticate."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "user-oauth-seed", "version": "0"},
        },
    }
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {key}",
    }
    request = urllib.request.Request(
        f"{GATEWAY}/{path}/mcp", data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raise SystemExit(
                f"expected OAuth challenge 401 for {path}, got {response.status}"
            )
    except urllib.error.HTTPError as exc:
        www = exc.headers.get("WWW-Authenticate") or exc.headers.get("www-authenticate") or ""
        if exc.code != 401 or "authorization" not in www.lower():
            raw = exc.read().decode()[:300]
            raise SystemExit(
                f"OAuth challenge smoke failed for {path}: http={exc.code} "
                f"www-authenticate={www!r} body={raw}"
            )
        print(f"smoke {path}: OAuth challenge 401 OK ({www[:80]}…)")


def main() -> None:
    wait_gateway()
    policy = load_policy()
    have = list_servers()
    seeded: list[str] = []
    for path, entry in policy.items():
        if not isinstance(entry, dict):
            continue
        tools = tool_names(entry)
        body = payload_for(path, tools)
        server_id = have.get(path)
        if server_id:
            body["server_id"] = server_id
            status, result, _ = http_json("PUT", GATEWAY + "/v1/mcp/server", body, MASTER)
        else:
            status, result, _ = http_json("POST", GATEWAY + "/v1/mcp/server", body, MASTER)
        if status >= 400:
            raise SystemExit(f"seeding {path} failed {status}: {result}")
        print(f"seeded {path}: tools={tools}")
        seeded.append(path)

    aliases = [name for name in MCP_ALIASES if name in seeded] or seeded
    key = generate_virtual_key(aliases)
    print(f"virtual_key_alias={KEY_ALIAS} (LiteLLM grants only — not Hi Agent plugin Bearer)")
    print(f"virtual_key={key}")
    smoke_oauth_challenge("m365", key)
    print(f"authorize_url={LOGIN_AUTHORIZE}")
    print(f"token_url_public={PUBLIC_BASE}/login/token")
    print(f"public_base={PUBLIC_BASE}")
    print(f"oauth_client_id=litellm-gateway")
    print(f"oauth_client_secret=litellm-gateway-secret")
    print(f"scopes=openid offline_access Mail.Read Mail.Send Sites.Read.All")
    print("ok")


if __name__ == "__main__":
    main()
