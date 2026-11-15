"""Bearer virtual-key MCP client for the M2M lab.

No browser OAuth. Hi Agent auth is the LiteLLM virtual key; LiteLLM fetches
Graph tokens via client_credentials against the login stand-in.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field


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


@dataclass
class Client:
    key: str
    sessions: dict = field(default_factory=dict)

    def request(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 40) -> HttpResult:
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResult(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read())
        except urllib.error.HTTPError as exc:
            return HttpResult(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())

    def mcp(self, mcp_url: str, method: str, params: dict | None = None, rpc_id: int = 1) -> dict:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if method != "notifications/initialized":
            message["id"] = rpc_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.key}",
            "MCP-Protocol-Version": "2025-03-26",
        }
        if self.sessions.get(mcp_url):
            headers["mcp-session-id"] = self.sessions[mcp_url]
        result = self.request(
            "POST",
            mcp_url,
            data=json.dumps(message).encode(),
            headers=headers,
            timeout=60,
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
