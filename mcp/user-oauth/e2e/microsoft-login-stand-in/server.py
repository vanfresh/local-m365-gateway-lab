#!/usr/bin/env python3
"""Stand-in for Microsoft Entra ID.

Issues authorization-code and refresh-token grants. Access tokens are JWTs
whose audience is https://graph.microsoft.com. A control endpoint on this
same port injects refresh failures for Suite 3.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

SECRET = os.environ.get("JWT_SECRET", "lab-jwt-secret").encode()
GRAPH_AUD = "https://graph.microsoft.com"
LOG_PATH = os.environ.get("LOGIN_LOG", "/logs/login.jsonl")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))

lock = threading.Lock()
codes = {}
refresh_tokens = {}
subjects = ["user-a", "user-b"]
subject_index = 0
refresh_mode = "ok"  # ok | invalid_grant | hang | refuse
expires_in = int(os.environ.get("EXPIRES_IN", "30"))


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


def next_subject(explicit: str | None) -> str:
    global subject_index
    if explicit:
        return explicit
    with lock:
        subject = subjects[subject_index % len(subjects)]
        subject_index += 1
        return subject


def issue(subject: str) -> dict:
    now = int(time.time())
    token = sign_jwt(
        {
            "iss": "microsoft-login-stand-in",
            "aud": GRAPH_AUD,
            "sub": subject,
            "scp": "Mail.Read Mail.Send ChannelMessage.Send",
            "iat": now,
            "exp": now + expires_in,
        }
    )
    refresh = secrets.token_urlsafe(24)
    with lock:
        refresh_tokens[refresh] = subject
    log_event({"event": "issued", "sub": subject, "expires_in": expires_in})
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "refresh_token": refresh,
        "scope": "openid offline_access Mail.Read Mail.Send",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode() if length else ""
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        if path in ("/health", "/healthz"):
            self._json(200, {"ok": True})
            return
        if path in ("/authorize", "/default/authorize"):
            self._authorize(query)
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/_control":
            self._control()
            return
        if path in ("/token", "/default/token"):
            self._token()
            return
        if path in ("/revoke", "/default/revoke"):
            form = self._form()
            with lock:
                refresh_tokens.pop(form.get("token", ""), None)
            log_event({"event": "revoked"})
            self._json(200, {})
            return
        self._json(404, {"error": "not_found"})

    def _authorize(self, query: dict) -> None:
        if query.get("response_type") != "code":
            self._json(400, {"error": "unsupported_response_type"})
            return
        if query.get("code_challenge_method", "S256") != "S256" or not query.get("code_challenge"):
            self._json(400, {"error": "invalid_request", "error_description": "PKCE S256 required"})
            return
        redirect = query.get("redirect_uri")
        if not redirect:
            self._json(400, {"error": "invalid_request", "error_description": "redirect_uri required"})
            return
        code = secrets.token_urlsafe(18)
        subject = next_subject(query.get("login_hint"))
        with lock:
            codes[code] = {
                "challenge": query["code_challenge"],
                "redirect_uri": redirect,
                "subject": subject,
            }
        location = redirect + ("&" if "?" in redirect else "?") + urlencode(
            {"code": code, "state": query.get("state", "")}
        )
        log_event({"event": "authorize", "sub": subject})
        self._send(302, b"", "text/plain", {"Location": location})

    def _token(self) -> None:
        with lock:
            mode = refresh_mode
        form = self._form()
        grant = form.get("grant_type", "")
        log_event({"event": "token", "grant_type": grant, "mode": mode})
        if grant == "client_credentials":
            self._json(400, {"error": "unauthorized_client", "error_description": "authorization code only"})
            return
        if grant == "refresh_token" and mode == "refuse":
            self.close_connection = True
            return
        if grant == "refresh_token" and mode == "hang":
            time.sleep(120)
            self._json(504, {"error": "timeout"})
            return
        if grant == "refresh_token" and mode == "invalid_grant":
            self._json(400, {"error": "invalid_grant"})
            return
        if grant == "authorization_code":
            self._exchange(form)
            return
        if grant == "refresh_token":
            self._refresh(form)
            return
        self._json(400, {"error": "unsupported_grant_type"})

    def _exchange(self, form: dict) -> None:
        code = form.get("code", "")
        with lock:
            saved = codes.pop(code, None)
        if not saved:
            self._json(400, {"error": "invalid_grant"})
            return
        if form.get("redirect_uri") and form["redirect_uri"] != saved["redirect_uri"]:
            self._json(400, {"error": "invalid_grant"})
            return
        verifier = form.get("code_verifier", "")
        digest = b64(hashlib.sha256(verifier.encode()).digest())
        if not hmac.compare_digest(digest, saved["challenge"]):
            self._json(400, {"error": "invalid_grant", "error_description": "PKCE verification failed"})
            return
        self._json(200, issue(saved["subject"]))

    def _refresh(self, form: dict) -> None:
        presented = form.get("refresh_token", "")
        with lock:
            subject = refresh_tokens.pop(presented, None)
        if not subject:
            self._json(400, {"error": "invalid_grant"})
            return
        self._json(200, issue(subject))

    def _control(self) -> None:
        global refresh_mode, expires_in, subject_index
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        with lock:
            if "refresh" in payload:
                refresh_mode = payload["refresh"]
            if "expires_in" in payload:
                expires_in = int(payload["expires_in"])
            if payload.get("reset_subjects"):
                subject_index = 0
            if payload.get("reset_log"):
                os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
                open(LOG_PATH, "w", encoding="utf-8").close()
            snapshot = {"refresh": refresh_mode, "expires_in": expires_in, "subject_index": subject_index}
        log_event({"event": "control", **snapshot})
        self._json(200, snapshot)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
