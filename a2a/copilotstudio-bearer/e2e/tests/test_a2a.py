"""A2A bearer + Nango SSE gateway tests (no Keycloak)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from http_client import api, sse

GATEWAY = os.environ.get("GATEWAY", "http://127.0.0.1:4800").rstrip("/")
A2A_LOG = Path(os.environ.get("A2A_LOG_FILE", Path(__file__).resolve().parents[1] / "logs" / "a2a.jsonl"))
STREAM = f"{GATEWAY}/a2a/v1/message:stream"
TOKEN_A = "lab-agent-a"
TOKEN_B = "lab-agent-b"


def a2a_log() -> list[dict]:
    if not A2A_LOG.exists():
        return []
    return [json.loads(line) for line in A2A_LOG.read_text().splitlines() if line.strip()]


def reset_a2a():
    import urllib.request

    req = urllib.request.Request(
        "http://127.0.0.1:8882/_control",
        data=json.dumps({"reset_log": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


def stream_body(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/stream",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


class TestA2A:
    def test_01_agent_card(self):
        import urllib.request

        with urllib.request.urlopen(f"{GATEWAY}/.well-known/agent-card.json", timeout=10) as resp:
            card = json.loads(resp.read().decode())
        assert "message:stream" in card.get("url", "")
        schemes = card.get("securitySchemes") or {}
        assert "oauth2" not in schemes
        assert "bearer" in schemes

    def test_01b_sse_url_get_without_bearer_returns_card(self):
        import urllib.request

        with urllib.request.urlopen(STREAM, timeout=10) as resp:
            card = json.loads(resp.read().decode())
        assert "message:stream" in card.get("url", "")

    def test_02_agent_a_sse_stream(self):
        reset_a2a()
        status, ctype, raw = sse("POST", STREAM, TOKEN_A, stream_body("ping from a"))
        assert status == 200, raw
        assert "text/event-stream" in ctype, ctype
        assert "user-a" in raw
        assert "completed" in raw
        authorized = [i for i in a2a_log() if i.get("authorized")]
        assert any(i.get("sub") == "user-a" for i in authorized), authorized

    def test_03_agent_b_sse_stream(self):
        reset_a2a()
        status, ctype, raw = sse("POST", STREAM, TOKEN_B, stream_body("ping from b"))
        assert status == 200, raw
        assert "user-b" in raw
        authorized = [i for i in a2a_log() if i.get("authorized")]
        assert any(i.get("sub") == "user-b" for i in authorized), authorized

    def test_04_agent_b_denied_blocked_skill(self):
        before = len(a2a_log())
        status, body = api("POST", STREAM, TOKEN_B, stream_body("please use blocked-skill now"))
        assert status == 403, body
        assert len(a2a_log()) == before

    def test_05_missing_token_401(self):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            STREAM,
            data=json.dumps(stream_body("x")).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected 401"
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

    def test_06_lab_issue_mints_idp_token(self):
        import urllib.request

        req = urllib.request.Request(
            "http://127.0.0.1:8881/_lab/issue",
            data=json.dumps({"sub": "user-a"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        assert body.get("access_token")
        assert "MCS.InvokeAsA2A" in (body.get("scope") or "")
