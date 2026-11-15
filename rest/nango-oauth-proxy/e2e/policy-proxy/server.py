#!/usr/bin/env python3
"""Thin policy proxy: Keycloak JWT → sync IdP tokens to Nango → guardrail → Graph via Nango."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import yaml

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "hiagent-lab")
KEYCLOAK_IDP_ALIAS = os.environ.get("KEYCLOAK_IDP_ALIAS", "microsoft-standin")
NANGO_URL = os.environ.get("NANGO_URL", "http://nango-server:3003").rstrip("/")
NANGO_SECRET = os.environ.get("NANGO_SECRET_KEY", "")
NANGO_PROVIDER = os.environ.get("NANGO_PROVIDER_CONFIG_KEY", "microsoft-graph-lab")
POLICY_FILE = Path(os.environ.get("CONNECTION_POLICY", "/app/connection_policy.yaml"))
AUDIT_LOG = os.environ.get("AUDIT_LOG", "/logs/audit.log")
CLIENT_MAP = {
    "agent-a": {"policy": "agent-a", "connection_id": "conn-a"},
    "agent-b": {"policy": "agent-b", "connection_id": "conn-b"},
}

_jwks_cache: dict | None = None
_jwks_at = 0.0


def load_policy() -> dict:
    data = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def audit(event: dict) -> None:
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.time(), **event}) + "\n")


def b64url_json(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()))


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


def http_raw(method: str, url: str, data: bytes | None, headers: dict, timeout=60):
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def verify_keycloak_token(token: str) -> dict:
    """Decode and lightly validate Keycloak access token (HS/RS via payload checks)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a jwt")
    claims = b64url_json(parts[1])
    now = int(time.time())
    if int(claims.get("exp", 0)) < now - 30:
        raise ValueError("expired")
    iss = str(claims.get("iss", ""))
    if KEYCLOAK_REALM not in iss and "hiagent-lab" not in iss:
        # still accept if azp is a known client (lab tunnel issuer rewrite)
        if claims.get("azp") not in CLIENT_MAP and claims.get("clientId") not in CLIENT_MAP:
            # resource access / azp
            pass
    azp = claims.get("azp") or claims.get("client_id") or claims.get("clientId")
    if not azp:
        # confidential client tokens often put client in azp
        for cid in CLIENT_MAP:
            if cid in json.dumps(claims):
                azp = cid
                break
    if azp not in CLIENT_MAP:
        raise ValueError(f"unknown client azp={azp}")
    claims["_client"] = azp
    return claims


def fetch_idp_tokens(user_access_token: str) -> dict | None:
    """Keycloak stored IdP tokens: GET /realms/{realm}/broker/{alias}/token"""
    url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/broker/{KEYCLOAK_IDP_ALIAS}/token"
    status, body, _ = http_json(
        "GET",
        url,
        headers={"Authorization": f"Bearer {user_access_token}"},
    )
    if status >= 400:
        audit({"event": "idp_token_fetch_failed", "status": status, "body": body})
        return None
    return body if isinstance(body, dict) else None


def upsert_nango_connection(connection_id: str, access_token: str, refresh_token: str | None, expires_in: int | None) -> None:
    if not NANGO_SECRET:
        raise RuntimeError("NANGO_SECRET_KEY not set")
    expires_at = None
    if expires_in:
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(expires_in)))
    credentials = {
        "type": "OAUTH2",
        "access_token": access_token,
    }
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
        # connection exists — PATCH credentials
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


def sync_and_ensure_connection(client_id: str, user_access_token: str) -> str:
    meta = CLIENT_MAP[client_id]
    connection_id = meta["connection_id"]
    idp = fetch_idp_tokens(user_access_token)
    if not idp or not idp.get("access_token"):
        raise RuntimeError("failed to fetch IdP tokens from Keycloak broker")
    upsert_nango_connection(
        connection_id,
        idp["access_token"],
        idp.get("refresh_token"),
        idp.get("expires_in"),
    )
    return connection_id


def _param_bag(query: str, body: bytes) -> dict[str, str]:
    """Collect string params from query + JSON body (Hi Agent often puts path vars in JSON)."""
    bag: dict[str, str] = {}
    if query:
        for key, values in parse_qs(query, keep_blank_values=True).items():
            if values and values[-1] != "":
                bag[key] = values[-1]
    if body:
        try:
            parsed = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if isinstance(value, (str, int, float, bool)):
                    bag[str(key)] = str(value)
    return bag


def expand_path_templates(path: str, query: str, body: bytes) -> str:
    """Replace {name} / %7Bname%7D in the path using query/JSON params.

    Hi Agent Create-tool often sends the literal template in the URL and the
    value only in the debug JSON body — without this, policy sees
    /mailFolders/%7BmailFolderId%7D/... and 403s.
    """
    decoded = unquote(path)
    bag = _param_bag(query, body)
    if not bag:
        return decoded

    def repl_braces(match: re.Match[str]) -> str:
        name = match.group(1)
        return bag.get(name, match.group(0))

    expanded = re.sub(r"\{([A-Za-z0-9_]+)\}", repl_braces, decoded)
    # Also handle already-encoded braces if somehow still present
    expanded = re.sub(
        r"%7B([A-Za-z0-9_]+)%7D",
        lambda m: bag.get(m.group(1), m.group(0)),
        expanded,
        flags=re.IGNORECASE,
    )
    return expanded


def policy_allows(client_id: str, method: str, path: str, body: bytes) -> tuple[bool, str]:
    policy = load_policy().get(CLIENT_MAP[client_id]["policy"]) or {}
    methods = [m.upper() for m in policy.get("methods") or []]
    if methods and method.upper() not in methods:
        return False, f"method {method} not allowed"
    prefixes = [p.get("prefix", "") for p in policy.get("paths") or [] if isinstance(p, dict)]
    if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
        return False, f"path {path} not allowed"
    deny_keys = policy.get("deny_body_keys") or []
    allow_to = policy.get("allow_to")
    if body and (deny_keys or allow_to):
        try:
            parsed = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            parsed = {}
        blob = json.dumps(parsed)
        for key in deny_keys:
            if key in parsed or f'"{key}"' in blob:
                # only deny if key present with non-empty value
                val = parsed.get(key)
                if val not in (None, [], {}, ""):
                    return False, f"denied body key {key}"
        if allow_to and method.upper() == "POST":
            # Graph sendMail shape: {"message":{"toRecipients":[{"emailAddress":{"address":"..."}}]}}
            addresses = re.findall(r'"address"\s*:\s*"([^"]+)"', blob)
            if not addresses:
                return False, "missing to recipient"
            for addr in addresses:
                if addr not in allow_to:
                    return False, f"recipient {addr} not allowlisted"
    return True, "ok"


OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "M365 lab via Nango", "version": "1.0.0"},
    "servers": [{"url": "/"}],
    "paths": {
        "/v1.0/me/mailFolders/{mailFolderId}/messages": {
            "get": {
                "operationId": "listMailFolderMessages",
                "summary": "List messages in a mail folder",
                "parameters": [
                    {
                        "name": "mailFolderId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "default": "folder-x"},
                    }
                ],
                "responses": {"200": {"description": "OK"}},
                "security": [{"oauth2": []}],
            }
        },
        "/v1.0/me/sendMail": {
            "post": {
                "operationId": "sendMail",
                "summary": "Send mail",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "message": {"type": "object"},
                                    "saveToSentItems": {"type": "boolean"},
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "OK"}},
                "security": [{"oauth2": []}],
            }
        },
    },
    "components": {
        "securitySchemes": {
            "oauth2": {
                "type": "oauth2",
                "flows": {
                    "authorizationCode": {
                        "authorizationUrl": "/.well-known/hint-auth-url",
                        "tokenUrl": "/.well-known/hint-token-url",
                        "scopes": {"openid": "openid"},
                    }
                },
            }
        }
    },
}


def parse_oauth_token_params(raw: bytes, content_type: str, query: str) -> dict[str, str]:
    """Merge query + body (form or JSON) into a flat string map for token exchange."""
    params: dict[str, str] = {}
    if query:
        for key, values in parse_qs(query, keep_blank_values=True).items():
            if values:
                params[key] = values[-1]
    ctype = (content_type or "").split(";")[0].strip().lower()
    if raw:
        if ctype == "application/json" or (raw[:1] in (b"{", b"[") and "urlencoded" not in ctype):
            try:
                payload = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if value is None:
                        continue
                    params[str(key)] = value if isinstance(value, str) else json.dumps(value)
        else:
            for key, values in parse_qs(raw.decode(), keep_blank_values=True).items():
                if values:
                    params[key] = values[-1]
    # Hi Agent sometimes omits grant_type — infer it.
    if not params.get("grant_type"):
        if params.get("code"):
            params["grant_type"] = "authorization_code"
        elif params.get("refresh_token"):
            params["grant_type"] = "refresh_token"
    return params


def forward_keycloak_token(params: dict[str, str], authorization: str | None) -> tuple[int, bytes, str]:
    token_url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
    data = urlencode(params).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    if authorization:
        headers["Authorization"] = authorization
    status, raw, resp_headers = http_raw("POST", token_url, data, headers)
    content_type = resp_headers.get("Content-Type") or resp_headers.get("content-type") or "application/json"
    audit(
        {
            "event": "oauth_token_shim",
            "grant_type": params.get("grant_type"),
            "has_code": bool(params.get("code")),
            "upstream_status": status,
        }
    )
    return status, raw, content_type


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

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/health", "/healthz"):
            self._json(200, {"ok": True})
            return
        if path == "/openapi.json":
            self._json(200, OPENAPI)
            return
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        # OAuth token shim for Hi Agent (injects grant_type; always form-encodes to Keycloak)
        if path in ("/oauth/token", "/login/token") and self.command == "POST":
            params = parse_oauth_token_params(body, self.headers.get("Content-Type", ""), parsed.query)
            if not params.get("grant_type"):
                self._json(
                    400,
                    {
                        "error": "invalid_request",
                        "error_description": "Missing form parameter: grant_type (and no code/refresh_token to infer)",
                    },
                )
                return
            status, raw, content_type = forward_keycloak_token(params, self.headers.get("Authorization"))
            self._send(status, raw, content_type)
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._json(401, {"error": "missing_bearer"})
            return
        token = auth[7:]
        try:
            claims = verify_keycloak_token(token)
        except Exception as exc:
            self._json(401, {"error": "invalid_token", "detail": str(exc)})
            return
        client_id = claims["_client"]
        path = expand_path_templates(path, parsed.query, body)
        allowed, reason = policy_allows(client_id, self.command, path, body)
        if not allowed:
            audit({"event": "policy_deny", "client": client_id, "method": self.command, "path": path, "reason": reason})
            self._json(403, {"error": "policy_denied", "reason": reason})
            return
        try:
            connection_id = sync_and_ensure_connection(client_id, token)
        except Exception as exc:
            audit({"event": "sync_failed", "client": client_id, "detail": str(exc)})
            self._json(502, {"error": "nango_sync_failed", "detail": str(exc)})
            return
        # Forward expanded path to Nango proxy
        nango_url = f"{NANGO_URL}/proxy{path}"
        if parsed.query:
            nango_url += "?" + parsed.query
        headers = {
            "Authorization": f"Bearer {NANGO_SECRET}",
            "Provider-Config-Key": NANGO_PROVIDER,
            "Connection-Id": connection_id,
            "Accept": self.headers.get("Accept", "application/json"),
        }
        if body:
            headers["Content-Type"] = self.headers.get("Content-Type", "application/json")
        status, raw, resp_headers = http_raw(self.command, nango_url, body or None, headers)
        audit(
            {
                "event": "proxied",
                "client": client_id,
                "connection_id": connection_id,
                "method": self.command,
                "path": path,
                "upstream_status": status,
            }
        )
        content_type = resp_headers.get("Content-Type") or resp_headers.get("content-type") or "application/json"
        self._send(status, raw, content_type)


if __name__ == "__main__":
    print(f"policy-proxy on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
