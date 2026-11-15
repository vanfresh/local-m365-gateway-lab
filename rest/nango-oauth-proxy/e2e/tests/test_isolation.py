"""REST + Keycloak OAuth + Nango isolation tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from oauth_client import api, login

GATEWAY = os.environ.get("GATEWAY", "http://127.0.0.1:4400").rstrip("/")
KEYCLOAK = os.environ.get("KEYCLOAK", "http://127.0.0.1:8085").rstrip("/")
GRAPH_LOG = Path(os.environ.get("GRAPH_LOG_FILE", Path(__file__).resolve().parents[1] / "logs" / "graph.jsonl"))
LOGIN_LOG = Path(os.environ.get("LOGIN_LOG_FILE", Path(__file__).resolve().parents[1] / "logs" / "login.jsonl"))


def graph_log() -> list[dict]:
    if not GRAPH_LOG.exists():
        return []
    return [json.loads(line) for line in GRAPH_LOG.read_text().splitlines() if line.strip()]


def login_log() -> list[dict]:
    if not LOGIN_LOG.exists():
        return []
    return [json.loads(line) for line in LOGIN_LOG.read_text().splitlines() if line.strip()]


def reset_graph():
    import urllib.request

    req = urllib.request.Request(
        "http://127.0.0.1:8482/_control",
        data=json.dumps({"reset_log": True, "clear": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


class TestIsolation:
    @pytest.fixture(scope="class", autouse=True)
    def tokens(self, request):
        import urllib.parse
        import urllib.request

        # Wipe federated users so login_hint maps cleanly to stand-in subjects
        data = urllib.parse.urlencode(
            {"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": "admin"}
        ).encode()
        tok = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
                    data=data,
                    method="POST",
                ),
                timeout=15,
            ).read()
        )["access_token"]
        users = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{KEYCLOAK}/admin/realms/hiagent-lab/users?max=100",
                    headers={"Authorization": f"Bearer {tok}"},
                ),
                timeout=15,
            ).read()
        )
        for u in users:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{KEYCLOAK}/admin/realms/hiagent-lab/users/{u['id']}",
                    headers={"Authorization": f"Bearer {tok}"},
                    method="DELETE",
                ),
                timeout=15,
            )

        req = urllib.request.Request(
            "http://127.0.0.1:8481/_control",
            data=json.dumps({"reset_subjects": True, "reset_log": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        a = login(KEYCLOAK, "hiagent-lab", "agent-a", "secret-a", login_hint="user-a")
        b = login(KEYCLOAK, "hiagent-lab", "agent-b", "secret-b", login_hint="user-b")
        request.cls.token_a = a.access_token
        request.cls.token_b = b.access_token

    def test_01_agent_a_can_read_folder_x(self):
        reset_graph()
        status, body = api(
            "GET",
            f"{GATEWAY}/v1.0/me/mailFolders/folder-x/messages",
            self.token_a,
        )
        assert status == 200, body
        authorized = [i for i in graph_log() if i.get("authorized")]
        assert authorized, body
        assert any(i.get("sub") == "user-a" for i in authorized), authorized

    def test_02_agent_a_cannot_send(self):
        before = len(graph_log())
        status, body = api(
            "POST",
            f"{GATEWAY}/v1.0/me/sendMail",
            self.token_a,
            {
                "message": {
                    "subject": "no",
                    "body": {"contentType": "Text", "content": "no"},
                    "toRecipients": [{"emailAddress": {"address": "allowed@example.com"}}],
                },
                "saveToSentItems": False,
            },
        )
        assert status == 403, body
        assert len(graph_log()) == before

    def test_03_agent_b_can_send(self):
        reset_graph()
        status, body = api(
            "POST",
            f"{GATEWAY}/v1.0/me/sendMail",
            self.token_b,
            {
                "message": {
                    "subject": "agent-b",
                    "body": {"contentType": "Text", "content": "ok"},
                    "toRecipients": [{"emailAddress": {"address": "allowed@example.com"}}],
                },
                "saveToSentItems": False,
            },
        )
        assert status == 200, body
        authorized = [i for i in graph_log() if i.get("authorized")]
        assert authorized, body
        assert any(i.get("sub") == "user-b" for i in authorized), authorized

    def test_04_agent_b_cannot_read(self):
        before = len(graph_log())
        status, body = api(
            "GET",
            f"{GATEWAY}/v1.0/me/mailFolders/folder-x/messages",
            self.token_b,
        )
        assert status == 403, body
        assert len(graph_log()) == before

    def test_05_missing_token_401(self):
        import urllib.request
        import urllib.error

        req = urllib.request.Request(f"{GATEWAY}/v1.0/me/mailFolders/folder-x/messages", method="GET")
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected 401"
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

    def test_06_login_log_has_authorization_code(self):
        events = {i.get("event") for i in login_log()}
        assert "authorization_code" in events or "authorize" in events, login_log()[-10:]
