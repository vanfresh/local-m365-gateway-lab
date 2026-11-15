#!/usr/bin/env python3
"""Entra stand-in with OIDC bits for Keycloak identity brokering.

Supports authorization_code (+ optional PKCE), refresh, discovery, userinfo,
and id_token. Subjects rotate user-a / user-b unless login_hint is set.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
time = __import__("time")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

SECRET = os.environ.get("JWT_SECRET", "lab-jwt-secret").encode()
GRAPH_AUD = "https://graph.microsoft.com"
ISSUER = os.environ.get("ISSUER", "http://microsoft-login-stand-in:8080")
LOG_PATH = os.environ.get("LOGIN_LOG", "/logs/login.jsonl")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
EXPIRES_IN = int(os.environ.get("EXPIRES_IN", "3600"))

lock = threading.Lock()
codes: dict = {}
refresh_tokens: dict = {}
subjects = ["user-a", "user-b"]
subject_index = 0


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_jwt(payload: dict) -> str:
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = b64(hmac.new(SECRET, f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def log_event(event: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with lock:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), **event}) + "\n")


def next_subject(explicit: str | None) -> str:
    global subject_index
    if explicit:
        return explicit
    with lock:
        subject = subjects[subject_index % len(subjects)]
        subject_index += 1
        return subject


def issue(subject: str, nonce: str | None = None, client_id: str = "keycloak-broker") -> dict:
    now = int(time.time())
    access = sign_jwt(
        {
            "iss": ISSUER,
            "aud": GRAPH_AUD,
            "sub": subject,
            "preferred_username": f"{subject}@example.com",
            "email": f"{subject}@example.com",
            "scp": "Mail.Read Mail.Send",
            "iat": now,
            "exp": now + EXPIRES_IN,
        }
    )
    id_claims = {
        "iss": ISSUER,
        "aud": client_id or "keycloak-broker",
        "sub": subject,
        "preferred_username": f"{subject}@example.com",
        "email": f"{subject}@example.com",
        "iat": now,
        "exp": now + EXPIRES_IN,
    }
    if nonce:
        id_claims["nonce"] = nonce
    id_token = sign_jwt(id_claims)
    refresh = secrets.token_urlsafe(24)
    with lock:
        refresh_tokens[refresh] = subject
    log_event({"event": "authorization_code", "sub": subject, "expires_in": EXPIRES_IN})
    return {
        "access_token": access,
        "id_token": id_token,
        "token_type": "Bearer",
        "expires_in": EXPIRES_IN,
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
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode() if length else ""
        return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        if path in ("/health", "/healthz"):
            self._json(200, {"ok": True})
            return
        if path == "/.well-known/openid-configuration":
            self._json(
                200,
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "userinfo_endpoint": ISSUER + "/userinfo",
                    "jwks_uri": ISSUER + "/jwks",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["HS256"],
                    "scopes_supported": ["openid", "offline_access", "Mail.Read", "Mail.Send"],
                    "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
                },
            )
            return
        if path == "/jwks":
            self._json(200, {"keys": []})
            return
        if path == "/userinfo":
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            claims = _claims(token)
            if not claims:
                self._json(401, {"error": "invalid_token"})
                return
            sub = claims.get("sub", "unknown")
            self._json(
                200,
                {
                    "sub": sub,
                    "preferred_username": f"{sub}@example.com",
                    "email": f"{sub}@example.com",
                },
            )
            return
        if path in ("/authorize", "/default/authorize", "/oauth2/v2.0/authorize"):
            self._authorize(query)
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/_control":
            self._control()
            return
        if path in ("/token", "/default/token", "/oauth2/v2.0/token"):
            self._token()
            return
        self._json(404, {"error": "not_found"})

    def _authorize(self, query: dict) -> None:
        if query.get("response_type", "code") != "code":
            self._json(400, {"error": "unsupported_response_type"})
            return
        redirect = query.get("redirect_uri")
        if not redirect:
            self._json(400, {"error": "invalid_request", "error_description": "redirect_uri required"})
            return
        # PKCE optional so Keycloak broker works without challenge.
        challenge = query.get("code_challenge") or ""
        code = secrets.token_urlsafe(18)
        subject = next_subject(query.get("login_hint"))
        with lock:
            codes[code] = {
                "challenge": challenge,
                "redirect_uri": redirect,
                "subject": subject,
                "nonce": query.get("nonce") or "",
                "client_id": query.get("client_id") or "keycloak-broker",
            }
        location = redirect + ("&" if "?" in redirect else "?") + urlencode(
            {"code": code, "state": query.get("state", "")}
        )
        log_event({"event": "authorize", "sub": subject})
        self._send(302, b"", "text/plain", {"Location": location})

    def _token(self) -> None:
        form = self._form()
        grant = form.get("grant_type", "")
        log_event({"event": "token", "grant_type": grant})
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
        challenge = saved.get("challenge") or ""
        if challenge:
            verifier = form.get("code_verifier", "")
            digest = b64(hashlib.sha256(verifier.encode()).digest())
            if not hmac.compare_digest(digest, challenge):
                self._json(400, {"error": "invalid_grant", "error_description": "PKCE verification failed"})
                return
        self._json(
            200,
            issue(
                saved["subject"],
                nonce=saved.get("nonce") or None,
                client_id=saved.get("client_id") or "keycloak-broker",
            ),
        )

    def _refresh(self, form: dict) -> None:
        presented = form.get("refresh_token", "")
        with lock:
            subject = refresh_tokens.pop(presented, None)
        if not subject:
            self._json(400, {"error": "invalid_grant"})
            return
        self._json(200, issue(subject))

    def _control(self) -> None:
        global subject_index
        length = int(self.headers.get("Content-Length") or "0")
        payload = json.loads(self.rfile.read(length) or b"{}")
        with lock:
            if payload.get("reset_subjects"):
                subject_index = 0
            if payload.get("reset_log"):
                os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
                open(LOG_PATH, "w", encoding="utf-8").close()
            snapshot = {"subject_index": subject_index}
        self._json(200, snapshot)


def _claims(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception:
        return None


if __name__ == "__main__":
    print(f"login stand-in (OIDC) on {HOST}:{PORT} issuer={ISSUER}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
