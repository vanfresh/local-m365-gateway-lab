"""MCP OAuth client used by the lab suites.

Follows discovery, authorization code, PKCE, and refresh. Tokens are kept per
MCP URL, which is how Hi Agent stores a connection.
"""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

CALLBACK = "http://127.0.0.1:8765/callback"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _claims(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (json.JSONDecodeError, ValueError):
        return None


@dataclass
class HttpResult:
    status: int
    headers: dict
    body: bytes

    def json(self):
        if not self.body:
            return {}
        return json.loads(self.body.decode())

    def text(self) -> str:
        return self.body.decode(errors="replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self) -> None:
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            _NoRedirect(),
        )
        self.tokens: dict[str, dict] = {}
        self.sessions: dict[str, str] = {}
        self.user_key: str | None = None

    def _user_key(self) -> str:
        if self.user_key:
            return self.user_key
        issued = self.request(
            "POST",
            _origin(os.environ["GATEWAY"]) + "/key/generate",
            data=json.dumps({"user_id": "default_user_id", "key_alias": "lab-session-" + secrets.token_hex(4)}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ['MASTER_KEY']}",
            },
        )
        if issued.status >= 400:
            raise RuntimeError(f"could not create session key {issued.status}: {issued.text()[:400]}")
        self.user_key = issued.json().get("key") or issued.json().get("token")
        if not self.user_key:
            raise RuntimeError(f"session key response had no key: {issued.text()[:400]}")
        return self.user_key

    def request(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 20) -> HttpResult:
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return HttpResult(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read())
        except urllib.error.HTTPError as exc:
            return HttpResult(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())

    def follow(self, url: str, headers: dict | None = None, timeout: int = 20) -> HttpResult:
        current = url
        for _ in range(15):
            if current.startswith(CALLBACK):
                return HttpResult(302, {"location": current}, b"")
            result = self.request("GET", current, headers=headers, timeout=timeout)
            if "connect_flow=" in current and result.status == 200:
                return HttpResult(200, {"location": current}, result.body)
            if result.status == 200 and b'name="username"' in result.body:
                posted = self.request(
                    "POST",
                    urllib.parse.urljoin(current, "/login"),
                    data=urllib.parse.urlencode(
                        {"username": "admin", "password": os.environ["MASTER_KEY"]}
                    ).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=timeout,
                )
                location = posted.headers.get("location")
                if posted.status in (301, 302, 303, 307, 308) and location:
                    current = urllib.parse.urljoin(current, location)
                    continue
                raise RuntimeError(f"gateway login failed {posted.status}: {posted.text()[:400]}")
            location = result.headers.get("location")
            if result.status in (301, 302, 303, 307, 308) and location:
                current = urllib.parse.urljoin(current, location)
                continue
            return result
        raise RuntimeError(f"too many redirects from {url}")

    def login(self, mcp_url: str) -> dict:
        resource = mcp_url
        discovery = self._discover(mcp_url)
        registration = self.request(
            "POST",
            discovery["registration_endpoint"],
            data=json.dumps(
                {
                    "client_name": "lab-suite",
                    "redirect_uris": [CALLBACK],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                }
            ).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if registration.status >= 400:
            raise RuntimeError(f"registration failed {registration.status}: {registration.text()[:500]}")
        client = registration.json()
        verifier, challenge = _pkce()
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": CALLBACK,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": resource,
                "state": secrets.token_urlsafe(12),
                "scope": " ".join(discovery.get("scopes") or []),
            }
        )
        landed = self.follow(discovery["authorization_endpoint"] + "?" + query)
        if "connect_flow=" in (landed.headers.get("Location") or "") or "connect_flow=" in landed.text():
            pass
        location = landed.headers.get("location") or ""
        if "connect_flow=" in location or (landed.status == 200 and "connect_flow" in landed.text()):
            flow_url = location if "connect_flow=" in location else ""
            if not flow_url:
                raise RuntimeError("connect flow did not redirect")
            final = self._finish_connect(flow_url)
        else:
            final = location if location.startswith(CALLBACK) else ""
        if not final:
            raise RuntimeError(f"login did not return to callback: {landed.status} {landed.text()[:800]}")
        code = urllib.parse.parse_qs(urllib.parse.urlparse(final).query).get("code", [""])[0]
        if not code:
            raise RuntimeError("callback did not include a code")
        token_body = urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CALLBACK,
                "client_id": client["client_id"],
                "code_verifier": verifier,
                "resource": resource,
            }
        ).encode()
        issued = self.request(
            "POST",
            discovery["token_endpoint"],
            data=token_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        if issued.status >= 400:
            raise RuntimeError(f"token exchange failed {issued.status}: {issued.text()[:500]}")
        payload = issued.json()
        payload["client_id"] = client["client_id"]
        payload["token_endpoint"] = discovery["token_endpoint"]
        payload["resource"] = resource
        self.tokens[mcp_url] = payload
        return payload

    def _finish_connect(self, connect_url: str) -> str:
        flow = urllib.parse.parse_qs(urllib.parse.urlparse(connect_url).query).get("connect_flow", [""])[0]
        described = self.request("GET", f"{_origin(connect_url)}/authorize/flow?flow={urllib.parse.quote(flow)}")
        if described.status >= 400:
            raise RuntimeError(f"connect flow failed {described.status}: {described.text()[:500]}")
        info = described.json()
        access, refresh, expires_in = _microsoft_tokens(os.environ["LOGIN"])
        scopes = info.get("requested_scopes") or ["openid", "offline_access", "Mail.Read", "Mail.Send"]
        bearer = self._user_key()
        stored = self.request(
            "POST",
            f"{_origin(connect_url)}/v1/mcp/server/{info['server_id']}/oauth-user-credential",
            data=json.dumps(
                {
                    "access_token": access,
                    "refresh_token": refresh,
                    "expires_in": expires_in,
                    "scopes": scopes,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {bearer}",
            },
        )
        if stored.status >= 400:
            raise RuntimeError(f"could not store Microsoft token {stored.status}: {stored.text()[:500]}")
        finished = self.request(
            "POST",
            f"{_origin(connect_url)}/authorize/complete",
            data=urllib.parse.urlencode(
                {"flow": flow, "decision": "approve", "delivery": "redirect"}
            ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        location = finished.headers.get("location") or ""
        if not location:
            raise RuntimeError(f"connect approval failed {finished.status}: {finished.text()[:500]}")
        if location.startswith(CALLBACK):
            return location
        landed = self.follow(urllib.parse.urljoin(connect_url, location))
        return landed.headers.get("location") or ""

    def refresh(self, mcp_url: str) -> dict:
        current = self.tokens[mcp_url]
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": current["refresh_token"],
                "client_id": current["client_id"],
                "resource": current["resource"],
            }
        ).encode()
        issued = self.request(
            "POST",
            current["token_endpoint"],
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        if issued.status >= 400:
            raise RuntimeError(f"refresh failed {issued.status}: {issued.text()[:500]}")
        payload = issued.json()
        current["access_token"] = payload["access_token"]
        if payload.get("refresh_token"):
            current["refresh_token"] = payload["refresh_token"]
        return current

    def mcp(self, mcp_url: str, method: str, params: dict | None = None, token: str | None = None, rpc_id: int = 1) -> dict:
        access = token if token is not None else self.tokens[mcp_url]["access_token"]
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if method != "notifications/initialized":
            message["id"] = rpc_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {access}",
            "MCP-Protocol-Version": "2025-03-26",
        }
        if self.sessions.get(mcp_url):
            headers["mcp-session-id"] = self.sessions[mcp_url]
        result = self.request(
            "POST",
            mcp_url,
            data=json.dumps(message).encode(),
            headers=headers,
            timeout=40,
        )
        if result.status >= 400:
            return {
                "http_status": result.status,
                "headers": result.headers,
                "error": result.text()[:1500],
            }
        session = result.headers.get("mcp-session-id")
        if session:
            self.sessions[mcp_url] = session
        return {
            "http_status": result.status,
            "headers": result.headers,
            "body": _parse_mcp(result.body),
        }

    def _discover(self, mcp_url: str) -> dict:
        probe = self.request(
            "POST",
            mcp_url,
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "lab-suite", "version": "1.0"},
                    },
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        if probe.status != 401:
            raise RuntimeError(f"expected 401 from {mcp_url}, got {probe.status}: {probe.text()[:500]}")
        header = probe.headers.get("WWW-Authenticate") or probe.headers.get("Www-Authenticate") or ""
        metadata_url = _quoted(header, "resource_metadata")
        parsed = urllib.parse.urlparse(mcp_url)
        if not metadata_url:
            metadata_url = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{parsed.path}"
        metadata = self.request("GET", metadata_url, headers={"Accept": "application/json"})
        if metadata.status >= 400:
            raise RuntimeError(f"resource metadata failed {metadata.status}: {metadata.text()[:500]}")
        document = metadata.json()
        issuer = (document.get("authorization_servers") or [None])[0]
        if not issuer:
            raise RuntimeError(f"resource metadata has no authorization server: {document}")
        issuer = issuer.rstrip("/")
        as_url = f"{issuer}/.well-known/oauth-authorization-server"
        path = urllib.parse.urlparse(issuer).path
        if path and path != "/":
            as_url = f"{parsed.scheme}://{urllib.parse.urlparse(issuer).netloc}/.well-known/oauth-authorization-server{path}"
        server = self.request("GET", as_url, headers={"Accept": "application/json"})
        if server.status >= 400:
            server = self.request(
                "GET",
                issuer + "/.well-known/oauth-authorization-server",
                headers={"Accept": "application/json"},
            )
        if server.status >= 400:
            raise RuntimeError(f"authorization server metadata failed {server.status}: {server.text()[:500]}")
        info = server.json()
        return {
            "authorization_endpoint": info["authorization_endpoint"],
            "token_endpoint": info["token_endpoint"],
            "registration_endpoint": info.get("registration_endpoint") or issuer + "/register",
            "scopes": document.get("scopes_supported") or info.get("scopes_supported") or [],
        }


def _origin(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _microsoft_tokens(login: str) -> tuple[str, str, int]:
    verifier, challenge = _pkce()
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": "litellm-gateway",
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "microsoft",
        }
    )
    class _Stop(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise RuntimeError(newurl)

    opener = urllib.request.build_opener(_Stop)
    try:
        opener.open(login.rstrip("/") + "/authorize?" + query, timeout=10)
        raise RuntimeError("login stand-in did not redirect")
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "")
    except RuntimeError as exc:
        location = str(exc)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [""])[0]
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": "litellm-gateway",
            "code_verifier": verifier,
        }
    ).encode()
    issued = urllib.request.Request(
        login.rstrip("/") + "/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(issued, timeout=10) as response:
        payload = json.loads(response.read().decode())
    return payload["access_token"], payload["refresh_token"], int(payload.get("expires_in") or 3600)


def _quoted(header: str, name: str) -> str:
    marker = name + '="'
    start = header.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = header.find('"', start)
    return header[start:end] if end > start else ""


def _parse_mcp(body: bytes) -> dict:
    text = body.decode(errors="replace").strip()
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    raise RuntimeError(f"unrecognized MCP response: {text[:500]}")


def graph_audience(token: str) -> bool:
    decoded = _claims(token)
    if not decoded:
        return False
    audience = decoded.get("aud")
    if isinstance(audience, list):
        return "https://graph.microsoft.com" in audience
    return audience == "https://graph.microsoft.com"
