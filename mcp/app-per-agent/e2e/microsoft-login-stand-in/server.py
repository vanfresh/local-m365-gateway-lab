#!/usr/bin/env python3
"""M2M Entra stand-in: client_credentials only.

Maps known app client_id/secret pairs to Graph-audience JWTs with distinct
sub (mailbox identity) and azp (app id) claims.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SECRET = os.environ.get("JWT_SECRET", "lab-jwt-secret").encode()
GRAPH_AUD = "https://graph.microsoft.com"
LOG_PATH = os.environ.get("LOGIN_LOG", "/logs/login.jsonl")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
EXPIRES_IN = int(os.environ.get("EXPIRES_IN", "3600"))

# client_id -> (client_secret, mailbox subject)
CLIENTS = {
    "app-agent-a": ("secret-a", "user-a"),
    "app-agent-b": ("secret-b", "user-b"),
}

lock = threading.Lock()


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_jwt(payload: dict) -> str:
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = b64(hmac.new(SECRET, f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def log_event(event: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    line = json.dumps({"at": time.time(), **event})
    with lock:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def issue(client_id: str, subject: str) -> dict:
    now = int(time.time())
    token = sign_jwt(
        {
            "iss": "app-per-agent-login-stand-in",
            "aud": GRAPH_AUD,
            "sub": subject,
            "azp": client_id,
            "appid": client_id,
            "roles": ["Mail.Read", "Mail.Send"],
            "iat": now,
            "exp": now + EXPIRES_IN,
        }
    )
    log_event({"event": "client_credentials", "client_id": client_id, "sub": subject})
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": EXPIRES_IN,
        "scope": "https://graph.microsoft.com/.default",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode() if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path not in ("/token", "/default/token", "/oauth2/v2.0/token"):
            self._json(404, {"error": "not_found"})
            return
        form = self._form()
        grant = form.get("grant_type", "")
        if grant != "client_credentials":
            self._json(
                400,
                {
                    "error": "unsupported_grant_type",
                    "error_description": "app-per-agent stand-in accepts client_credentials only",
                },
            )
            return
        client_id = form.get("client_id", "")
        client_secret = form.get("client_secret", "")
        known = CLIENTS.get(client_id)
        if not known or not hmac.compare_digest(known[0], client_secret):
            log_event({"event": "unauthorized_client", "client_id": client_id})
            self._json(401, {"error": "invalid_client"})
            return
        self._json(200, issue(client_id, known[1]))


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"app-per-agent login stand-in on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
