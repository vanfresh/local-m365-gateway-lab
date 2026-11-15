"""Authorization-code client against Keycloak (with IdP broker hint)."""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None = None
    raw: dict | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        _NoRedirect(),
    )


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(
    keycloak: str,
    realm: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = "http://127.0.0.1:8765/callback",
    idp_hint: str = "microsoft-standin",
    login_hint: str | None = None,
) -> Tokens:
    auth = f"{keycloak.rstrip('/')}/realms/{realm}/protocol/openid-connect/auth"
    token_url = f"{keycloak.rstrip('/')}/realms/{realm}/protocol/openid-connect/token"
    verifier, challenge = _pkce()
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "openid",
        "redirect_uri": redirect_uri,
        "kc_idp_hint": idp_hint,
        "state": "lab",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = auth + "?" + urllib.parse.urlencode(params)
    opener = _opener()
    current = url
    code = None
    for _ in range(20):
        req = urllib.request.Request(current, method="GET")
        try:
            with opener.open(req, timeout=20) as resp:
                raise RuntimeError(f"unexpected 200 at {current}: {resp.read()[:200]!r}")
        except urllib.error.HTTPError as exc:
            loc = exc.headers.get("Location") or ""
            if not loc:
                raise RuntimeError(f"no redirect from {current}: {exc.code} {exc.read()[:300]!r}") from exc
            # Only treat Hi Agent/e2e callback as done (avoid matching session_code=).
            if loc.startswith(redirect_uri):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
                if qs.get("error"):
                    raise RuntimeError(f"oauth error callback: {loc}")
                code = (qs.get("code") or [""])[0]
                if not code:
                    raise RuntimeError(f"callback without code: {loc}")
                break
            current = urllib.parse.urljoin(current, loc)
    if not code:
        raise RuntimeError("did not receive authorization code")
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
        }
    ).encode()
    req = urllib.request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    if not payload.get("access_token"):
        raise RuntimeError(f"token response missing access_token: {payload}")
    return Tokens(access_token=payload["access_token"], refresh_token=payload.get("refresh_token"), raw=payload)


def api(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict | list | str]:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, raw
