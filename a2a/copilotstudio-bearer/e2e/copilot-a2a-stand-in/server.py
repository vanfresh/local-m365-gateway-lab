#!/usr/bin/env python3
"""Fake Copilot Studio A2A endpoint (SSE message:stream)."""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LOG_PATH = os.environ.get("A2A_LOG", "/logs/a2a.jsonl")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
EXPECT_AUD = os.environ.get("EXPECT_AUD", "https://api.powerplatform.com/copilotstudio-a2a")
STREAM_PATHS = {
    "/a2a/v1/message:stream",
    "/a2a/v1/message/stream",
}


def log_event(event: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.time(), **event}) + "\n")


def claims(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception:
        return None


def agent_card(base: str = "") -> dict:
    url = (base or "").rstrip("/") + "/a2a/v1/message:stream"
    return {
        "name": "Copilot A2A Lab Stand-in",
        "description": "Fake Copilot Studio A2A agent for local Hi Agent demos.",
        "url": url,
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "echo",
                "name": "Echo",
                "description": "Streams a short acknowledgement over SSE.",
                "tags": ["lab"],
            }
        ],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth_sub(self) -> tuple[str | None, str | None]:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None, "missing_bearer"
        token = auth[7:]
        c = claims(token)
        if not c:
            return None, "invalid_token"
        aud = c.get("aud")
        if EXPECT_AUD and aud and aud != EXPECT_AUD:
            return None, f"bad_aud:{aud}"
        return str(c.get("sub") or "unknown"), None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/health", "/healthz"):
            self._json(200, {"ok": True})
            return
        if path in ("/.well-known/agent-card.json", "/.well-known/agent.json", "/agent-card.json"):
            self._json(200, agent_card())
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/_control":
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if payload.get("reset_log"):
                os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
                open(LOG_PATH, "w", encoding="utf-8").close()
            self._json(200, {"ok": True})
            return
        if path not in STREAM_PATHS and not path.endswith("message:stream"):
            self._json(404, {"error": "not_found", "path": path})
            return
        sub, err = self._auth_sub()
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            payload = {}
        log_event(
            {
                "event": "message_stream",
                "path": path,
                "sub": sub,
                "authorized": err is None,
                "error": err,
            }
        )
        if err:
            self._json(401, {"error": err})
            return
        task_id = str(uuid.uuid4())
        context_id = (
            ((payload.get("params") or {}) if isinstance(payload, dict) else {}).get("message") or {}
        )
        if isinstance(context_id, dict):
            context_id = context_id.get("contextId") or str(uuid.uuid4())
        else:
            context_id = str(uuid.uuid4())
        user_text = _extract_text(payload) or "hello"
        events = [
            {
                "jsonrpc": "2.0",
                "id": payload.get("id") if isinstance(payload, dict) else "1",
                "result": {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {"state": "submitted"},
                    "kind": "task",
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "tasks/statusUpdate",
                "params": {"id": task_id, "status": {"state": "working"}},
            },
            {
                "jsonrpc": "2.0",
                "method": "tasks/artifactUpdate",
                "params": {
                    "id": task_id,
                    "artifact": {
                        "parts": [
                            {
                                "kind": "text",
                                "text": f"Copilot stand-in for {sub}: {user_text}",
                            }
                        ]
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "tasks/statusUpdate",
                "params": {"id": task_id, "status": {"state": "completed"}, "final": True},
            },
        ]
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        for event in events:
            chunk = f"data: {json.dumps(event)}\n\n".encode()
            self.wfile.write(chunk)
            self.wfile.flush()
            time.sleep(0.05)
        log_event({"event": "stream_complete", "sub": sub, "task_id": task_id})


def _extract_text(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    params = payload.get("params") or {}
    message = params.get("message") if isinstance(params, dict) else {}
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or message.get("content") or []
    if isinstance(parts, str):
        return parts
    texts = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                if part.get("text"):
                    texts.append(str(part["text"]))
                elif part.get("kind") == "text" and part.get("text"):
                    texts.append(str(part["text"]))
    return " ".join(texts).strip()


if __name__ == "__main__":
    print(f"copilot-a2a stand-in on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
