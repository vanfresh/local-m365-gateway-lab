"""Auth failures and Graph failures."""

from __future__ import annotations

import json
import time
import urllib.parse

import pytest

from harness import (
    call_tool,
    control_graph,
    control_login,
    graph_log,
    mcp_url,
    open_session,
    reset_graph_log,
    seed_servers,
    send_mail_arguments,
)
from mcp_client import CALLBACK, Client, _pkce


class TestFailure:
    @pytest.fixture(scope="class", autouse=True)
    def session(self, request):
        seed_servers()
        control_login({"refresh": "ok", "reset_subjects": True, "expires_in": 3600})
        client = Client()
        open_session(client, "m365")
        request.cls.client = client
        yield
        control_graph({"clear": True})
        control_login({"refresh": "ok", "expires_in": 3600})

    def test_01_missing_token_is_rejected(self):
        missing = self.client.mcp(
            mcp_url("m365"),
            "tools/call",
            {"name": "list-mail-folder-messages", "arguments": {"mailFolderId": "folder-x"}},
            token="",
        )
        assert missing.get("http_status", 200) >= 400

    def test_02_garbage_token_is_rejected(self):
        garbage = self.client.mcp(
            mcp_url("m365"),
            "tools/call",
            {"name": "list-mail-folder-messages", "arguments": {"mailFolderId": "folder-x"}},
            token="not-a-token",
        )
        assert garbage.get("http_status", 200) >= 400

    def test_03_unknown_code_is_rejected(self):
        _verifier, challenge = _pkce()
        bad = self.client.request(
            "POST",
            self.client.tokens[mcp_url("m365")]["token_endpoint"],
            data=urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": "not-a-code",
                    "redirect_uri": CALLBACK,
                    "client_id": self.client.tokens[mcp_url("m365")]["client_id"],
                    "code_verifier": _verifier,
                }
            ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert bad.status >= 400
        assert challenge not in bad.text()

    def test_04_invalid_grant_on_microsoft_refresh_fails_the_tool_call(self):
        control_login({"expires_in": 30, "refresh": "ok"})
        open_session(self.client, "m365")
        time.sleep(2)
        control_login({"refresh": "invalid_grant"})
        held = self.client.tokens[mcp_url("m365")]["access_token"]
        reset_graph_log()
        failed = call_tool(self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-x"})
        text = json.dumps(failed)
        assert "hello-" not in text, text[:240]
        assert self.client.tokens[mcp_url("m365")]["access_token"] == held
        assert "refresh_token" not in text
        control_login({"refresh": "ok", "expires_in": 3600})
        open_session(self.client, "m365")

    def test_04b_dead_graph_refresh_sends_hi_agent_to_login(self):
        control_login({"expires_in": 30, "refresh": "ok"})
        open_session(self.client, "m365")
        time.sleep(2)
        control_login({"refresh": "invalid_grant"})
        reset_graph_log()
        failed = call_tool(self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-x"})
        headers = {str(key).lower(): value for key, value in (failed.get("headers") or {}).items()}
        text = json.dumps(failed)
        assert failed.get("http_status") == 401, text[:300]
        assert "www-authenticate" in headers, text[:300]
        assert "refresh_token" not in text
        assert "eyJ" not in text
        assert not any(item.get("authorized") for item in graph_log())
        control_login({"refresh": "ok", "expires_in": 3600})
        open_session(self.client, "m365")
        reset_graph_log()
        recovered = call_tool(
            self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-x"}
        )
        assert any(item.get("authorized") for item in graph_log()), json.dumps(recovered)[:240]

    @pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
    def test_05_graph_status_becomes_a_tool_error(self, status):
        reset_graph_log()
        times = 4 if status == 429 else 1
        control_graph({"status": status, "times": times, "body": {"error": {"code": str(status), "message": "forced"}}})
        result = call_tool(self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-x"})
        text = json.dumps(result)
        assert "hello-" not in text, text[:180]
        assert "eyJ" not in text

    def test_06_malformed_graph_body_becomes_a_tool_error(self):
        reset_graph_log()
        control_graph({"status": 200, "raw": "this is not json", "content_type": "application/json", "times": 1})
        malformed = call_tool(self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-x"})
        assert "hello-" not in json.dumps(malformed)

    def test_07_failed_send_reaches_graph_once(self):
        reset_graph_log()
        control_graph({"status": 500, "times": 1, "body": {"error": {"code": "500", "message": "forced"}}})
        send_failed = call_tool(
            self.client, "m365", "send-mail", send_mail_arguments("allowed@example.com", "once", "once")
        )
        posts = [item for item in graph_log() if item.get("method") == "POST"]
        assert len(posts) == 1
        assert "created-1" not in json.dumps(send_failed)

    def test_08_hung_send_is_a_single_graph_request(self):
        reset_graph_log()
        control_graph({"hang": True, "seconds": 8, "times": 1})
        hung = call_tool(self.client, "m365", "send-mail", send_mail_arguments("allowed@example.com", "hang", "hang"))
        posts = [item for item in graph_log() if item.get("method") == "POST"]
        assert len(posts) == 1
        assert "created-1" not in json.dumps(hung)
