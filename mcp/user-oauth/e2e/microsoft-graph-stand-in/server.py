#!/usr/bin/env python3
"""Stand-in for Microsoft Graph.

Accepts a bearer token only when its audience is https://graph.microsoft.com.
Records every request. A control endpoint forces the next responses to fail.
"""

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

GRAPH_AUD = "https://graph.microsoft.com"
LOG_PATH = os.environ.get("GRAPH_LOG", "/logs/graph.jsonl")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))

lock = threading.Lock()
pending = []


def b64decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def token_claims(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        return json.loads(b64decode(parts[1]))
    except (json.JSONDecodeError, ValueError):
        return None


def audience_ok(decoded: dict) -> bool:
    audience = decoded.get("aud")
    if isinstance(audience, list):
        return GRAPH_AUD in audience
    return audience == GRAPH_AUD


def log_event(event: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with lock:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), **event}) + "\n")


def read_log() -> list:
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def success_body(method: str, path: str, decoded: dict) -> dict:
    subject = decoded.get("sub", "unknown")
    folder = ""
    marker = "/mailFolders/"
    if marker in path:
        folder = path.split(marker, 1)[1].split("/", 1)[0]
    if method == "GET" and path.rstrip("/").endswith("/messages"):
        return {
            "value": [
                {
                    "id": f"msg-{subject}",
                    "subject": f"hello-{subject}",
                    "parentFolderId": folder,
                    "from": {"emailAddress": {"address": f"{subject}@example.com"}},
                }
            ]
        }
    if method == "GET" and path.rstrip("/").endswith("/me"):
        return {"id": subject, "userPrincipalName": f"{subject}@example.com"}
    if method in ("POST", "PATCH", "PUT", "DELETE"):
        return {"id": "created-1", "subject": f"hello-{subject}", "parentFolderId": folder}
    return {"id": "item-1", "value": [{"id": "item-1"}]}


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

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/health", "/healthz"):
            self._json(200, {"ok": True})
            return
        if path == "/_control":
            self._control()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        decoded = token_claims(token) if token else None
        authorized = bool(decoded and audience_ok(decoded))
        log_event(
            {
                "method": self.command,
                "path": path,
                "query": parsed.query,
                "body": raw.decode("utf-8", errors="replace")[:4000],
                "sub": (decoded or {}).get("sub"),
                "aud": (decoded or {}).get("aud"),
                "authorized": authorized,
            }
        )
        if not authorized:
            self._json(
                401,
                {"error": {"code": "InvalidAuthenticationToken", "message": "audience rejected"}},
            )
            return
        with lock:
            fault = pending.pop(0) if pending else None
        if fault:
            if fault.get("hang"):
                time.sleep(int(fault.get("seconds", 30)))
            status = int(fault.get("status", 200))
            if "raw" in fault:
                self._send(status, str(fault["raw"]).encode(), fault.get("content_type", "application/json"))
                return
            self._json(status, fault.get("body", {"error": {"code": "forced", "message": "forced failure"}}))
            return
        self._json(200, success_body(self.command, path, decoded or {}))

    def _control(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        with lock:
            if payload.get("reset_log"):
                open(LOG_PATH, "w", encoding="utf-8").close()
            if payload.get("clear"):
                pending.clear()
            times = int(payload.get("times", 1))
            fault = {
                key: value
                for key, value in payload.items()
                if key not in ("times", "reset_log", "clear")
            }
            if fault:
                pending.extend([dict(fault) for _ in range(times)])
            snapshot = {"queued": len(pending)}
        self._json(200, snapshot)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
