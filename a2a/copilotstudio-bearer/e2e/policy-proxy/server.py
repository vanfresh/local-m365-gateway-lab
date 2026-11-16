#!/usr/bin/env python3
"""A2A bearer gateway: lab API key → mint Entra-shaped tokens → Nango → SSE to Copilot stand-in."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

import yaml

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
NANGO_URL = os.environ.get("NANGO_URL", "http://nango-server:3003").rstrip("/")
NANGO_SECRET = os.environ.get("NANGO_SECRET_KEY", "")
NANGO_PROVIDER = os.environ.get("NANGO_PROVIDER_CONFIG_KEY", "copilot-a2a-lab")
COPILOT_A2A_URL = os.environ.get("COPILOT_A2A_URL", "http://copilot-a2a-stand-in:8080").rstrip("/")
LOGIN_STANDIN_URL = os.environ.get("LOGIN_STANDIN_URL", "http://microsoft-login-stand-in:8080").rstrip("/")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:4800").rstrip("/")
POLICY_FILE = Path(os.environ.get("CONNECTION_POLICY", "/app/connection_policy.yaml"))
AUDIT_LOG = os.environ.get("AUDIT_LOG", "/logs/audit.log")
CLIENT_MAP = {
    "agent-a": {"policy": "agent-a", "connection_id": "conn-a", "lab_sub": "user-a"},
    "agent-b": {"policy": "agent-b", "connection_id": "conn-b", "lab_sub": "user-b"},
}
# Hi Agent Create A2A bearer field max 128 chars — short lab keys only.
LAB_API_KEYS = {
    "lab-agent-a": "agent-a",
    "lab-agent-b": "agent-b",
}
STREAM_PATH = "/a2a/v1/message:stream"


def load_policy() -> dict:
    data = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def audit(event: dict) -> None:
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.time(), **event}) + "\n")


def http_json(method: str, url: str, payload=None, headers=None, timeout=30):
    data = None if payload is None else json.dumps(payload).encode()
    req_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            body = json.loads(raw.decode() or "{}") if raw else {}
            return response.status, body, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:800]}
        return exc.code, body, dict(exc.headers)


def upsert_nango_connection(connection_id: str, access_token: str, refresh_token: str | None, expires_in: int | None) -> None:
    if not NANGO_SECRET:
        raise RuntimeError("NANGO_SECRET_KEY not set")
    expires_at = None
    if expires_in:
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(expires_in)))
    credentials = {"type": "OAUTH2", "access_token": access_token}
    if refresh_token:
        credentials["refresh_token"] = refresh_token
    if expires_at:
        credentials["expires_at"] = expires_at
    payload = {
        "provider_config_key": NANGO_PROVIDER,
        "connection_id": connection_id,
        "credentials": credentials,
    }
    status, body, _ = http_json(
        "POST",
        NANGO_URL + "/connections",
        payload,
        headers={"Authorization": f"Bearer {NANGO_SECRET}"},
    )
    if status == 409:
        status, body, _ = http_json(
            "PATCH",
            f"{NANGO_URL}/connections/{quote(connection_id)}",
            {"credentials": credentials},
            headers={
                "Authorization": f"Bearer {NANGO_SECRET}",
                "Provider-Config-Key": NANGO_PROVIDER,
            },
        )
    if status >= 400:
        raise RuntimeError(f"nango upsert failed {status}: {body}")
    audit({"event": "nango_upsert", "connection_id": connection_id, "status": status})


def nango_access_token(connection_id: str) -> str:
    url = (
        f"{NANGO_URL}/connections/{quote(connection_id)}"
        f"?provider_config_key={quote(NANGO_PROVIDER)}"
    )
    status, body, _ = http_json(
        "GET",
        url,
        headers={
            "Authorization": f"Bearer {NANGO_SECRET}",
            "Provider-Config-Key": NANGO_PROVIDER,
        },
    )
    if status >= 400:
        raise RuntimeError(f"nango get connection failed {status}: {body}")
    creds = body.get("credentials") if isinstance(body, dict) else None
    if not isinstance(creds, dict) or not creds.get("access_token"):
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            creds = data.get("credentials") or data
        if not isinstance(creds, dict) or not creds.get("access_token"):
            raise RuntimeError(f"nango connection missing access_token: {body}")
    return creds["access_token"]


def mint_lab_idp_tokens(subject: str) -> dict:
    status, body, _ = http_json(
        "POST",
        f"{LOGIN_STANDIN_URL}/_lab/issue",
        {"sub": subject},
    )
    if status >= 400 or not isinstance(body, dict) or not body.get("access_token"):
        raise RuntimeError(f"lab IdP mint failed {status}: {body}")
    return body


def resolve_caller(raw_token: str) -> str:
    if raw_token not in LAB_API_KEYS:
        raise ValueError("unknown_lab_bearer")
    return LAB_API_KEYS[raw_token]


def sync_and_ensure_connection(client_id: str) -> str:
    meta = CLIENT_MAP[client_id]
    connection_id = meta["connection_id"]
    idp = mint_lab_idp_tokens(meta["lab_sub"])
    upsert_nango_connection(
        connection_id,
        idp["access_token"],
        idp.get("refresh_token"),
        idp.get("expires_in"),
    )
    return connection_id


def policy_allows(client_id: str, method: str, path: str, body: bytes) -> tuple[bool, str]:
    policy = load_policy().get(CLIENT_MAP[client_id]["policy"]) or {}
    methods = [m.upper() for m in policy.get("methods") or []]
    if methods and method.upper() not in methods:
        return False, f"method {method} not allowed"
    prefixes = [p.get("prefix", "") for p in policy.get("paths") or [] if isinstance(p, dict)]
    if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
        return False, f"path {path} not allowed"
    deny_text = policy.get("deny_text_contains") or []
    if body and deny_text:
        blob = body.decode(errors="replace")
        for needle in deny_text:
            if needle and needle in blob:
                return False, f"denied text contains {needle}"
    return True, "ok"


def agent_card() -> dict:
    stream = f"{PUBLIC_BASE_URL}{STREAM_PATH}"
    return {
        "name": "Hi Agent → Copilot A2A Gateway",
        "description": (
            "Lab gateway. Paste bearer lab-agent-a (or lab-agent-b). "
            "Max 128 chars — do NOT paste a JWT. Gateway mints Copilot tokens into Nango."
        ),
        "url": stream,
        "version": "1.0.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "securitySchemes": {
            "bearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "lab-api-key",
                "description": "Short lab key: lab-agent-a or lab-agent-b (≤128 chars).",
            }
        },
        "security": [{"bearer": []}],
        "skills": [
            {
                "id": "copilot-lab",
                "name": "Copilot lab stream",
                "description": "Streams a stand-in Copilot A2A response.",
                "tags": ["lab", "a2a"],
            }
        ],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def _stream_copilot(self, method: str, path: str, body: bytes, entra_token: str, client_id: str, connection_id: str) -> None:
        upstream = f"{COPILOT_A2A_URL}{path}"
        headers = {
            "Authorization": f"Bearer {entra_token}",
            "Accept": "text/event-stream",
            "Content-Type": self.headers.get("Content-Type", "application/json"),
        }
        request = urllib.request.Request(upstream, data=body or None, headers=headers, method=method)
        headers_sent = False
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                content_type = response.headers.get("Content-Type") or "text/event-stream"
                self.close_connection = True
                self.send_response(response.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                headers_sent = True
                while True:
                    chunk = response.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                audit(
                    {
                        "event": "proxied_sse",
                        "client": client_id,
                        "connection_id": connection_id,
                        "path": path,
                        "upstream_status": response.status,
                    }
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            audit(
                {
                    "event": "proxied_sse_error",
                    "client": client_id,
                    "connection_id": connection_id,
                    "path": path,
                    "upstream_status": exc.code,
                }
            )
            if not headers_sent:
                ctype = exc.headers.get("Content-Type") if exc.headers else None
                self._send(exc.code, raw, ctype or "application/json")

    def _is_public_agent_card(self, path: str) -> bool:
        return path in {
            "/",
            "/.well-known/agent-card.json",
            "/.well-known/agent.json",
            "/agent-card.json",
            "/agent.json",
            "/a2a/.well-known/agent-card.json",
            "/a2a/.well-known/agent.json",
            "/a2a/v1/.well-known/agent-card.json",
            "/a2a/v1/.well-known/agent.json",
        }

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if self.command == "OPTIONS":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/health", "/healthz"):
            self._json(200, {"ok": True})
            return
        if self.command == "GET" and (
            self._is_public_agent_card(path)
            or path == STREAM_PATH
            or path.rstrip("/") == "/a2a/v1"
            or path.startswith("/a2a/")
        ):
            self._json(200, agent_card())
            return
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._json(401, {"error": "missing_bearer"})
            return
        token = auth[7:].strip()
        try:
            client_id = resolve_caller(token)
        except Exception as exc:
            self._json(401, {"error": "invalid_token", "detail": str(exc)})
            return
        allowed, reason = policy_allows(client_id, self.command, path, body)
        if not allowed:
            audit({"event": "policy_deny", "client": client_id, "method": self.command, "path": path, "reason": reason})
            self._json(403, {"error": "policy_denied", "reason": reason})
            return
        try:
            connection_id = sync_and_ensure_connection(client_id)
            entra = nango_access_token(connection_id)
        except Exception as exc:
            audit({"event": "sync_failed", "client": client_id, "detail": str(exc)})
            self._json(502, {"error": "nango_sync_failed", "detail": str(exc)})
            return
        if path.startswith("/a2a/") and self.command == "POST":
            self._stream_copilot(self.command, path, body, entra, client_id, connection_id)
            return
        self._json(404, {"error": "not_found", "path": path})


if __name__ == "__main__":
    print(f"a2a bearer gateway on {HOST}:{PORT} public={PUBLIC_BASE_URL}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
