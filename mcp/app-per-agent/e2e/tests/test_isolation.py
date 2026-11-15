"""Per-agent isolation via M2M MCP aliases + virtual-key grants.

m365_a (app-agent-a → user-a): read folder-x only.
m365_b (app-agent-b → user-b): send allowlist only.
No authorization_code / browser step in this suite.
"""

from __future__ import annotations

import json

import pytest

from harness import (
    call_denied,
    call_tool,
    graph_count,
    graph_log,
    login_log,
    mcp_url,
    mint_key,
    open_session,
    reset_graph_log,
    seed_servers,
    send_mail_arguments,
    tool_names,
)
from mcp_client import Client


class TestIsolation:
    @pytest.fixture(scope="class", autouse=True)
    def session(self, request):
        seed_servers()
        key_a = mint_key("agent-a-key", ["m365_a"])
        key_b = mint_key("agent-b-key", ["m365_b"])
        a = Client(key=key_a)
        b = Client(key=key_b)
        open_session(a, "m365_a")
        open_session(b, "m365_b")
        request.cls.agent_a = a
        request.cls.agent_b = b
        request.cls.key_a = key_a
        request.cls.key_b = key_b
        yield

    def test_01_key_a_tools_list_is_read_only(self):
        listed = tool_names(self.agent_a, "m365_a")
        prefix = "m365_a-"
        bare = [name[len(prefix):] if name.startswith(prefix) else name for name in listed]
        assert bare == ["list-mail-folder-messages"]

    def test_02_key_b_tools_list_is_send_only(self):
        listed = tool_names(self.agent_b, "m365_b")
        prefix = "m365_b-"
        bare = [name[len(prefix):] if name.startswith(prefix) else name for name in listed]
        assert bare == ["send-mail"]

    def test_03_key_a_read_uses_user_a_on_graph(self):
        reset_graph_log()
        result = call_tool(
            self.agent_a, "m365_a", "list-mail-folder-messages", {"mailFolderId": "folder-x"}
        )
        assert "rejected by connection policy" not in json.dumps(result)
        authorized = [item for item in graph_log() if item.get("authorized")]
        assert authorized, json.dumps(result)[:400]
        subjects = {item.get("sub") for item in authorized}
        assert "user-a" in subjects, subjects
        assert any(
            item.get("event") == "client_credentials" and item.get("client_id") == "app-agent-a"
            for item in login_log()
        ), login_log()[-5:]

    def test_04_key_a_cannot_use_m365_b(self):
        # Grant denial: key A has no object_permission for m365_b
        denied = Client(key=self.key_a).mcp(
            mcp_url("m365_b"),
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "cross-grant", "version": "1"},
            },
        )
        assert call_denied(denied) or denied.get("http_status", 200) in (401, 403)

    def test_05_key_b_can_send(self):
        reset_graph_log()
        result = call_tool(
            self.agent_b,
            "m365_b",
            "send-mail",
            send_mail_arguments("allowed@example.com", "agent-b", "ok"),
        )
        assert "rejected by connection policy" not in json.dumps(result)
        authorized = [item for item in graph_log() if item.get("authorized")]
        assert authorized, json.dumps(result)[:400]
        subjects = {item.get("sub") for item in authorized}
        assert "user-b" in subjects, subjects

    def test_06_key_b_cannot_read_via_m365_a(self):
        denied = Client(key=self.key_b).mcp(
            mcp_url("m365_a"),
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "cross-grant-b", "version": "1"},
            },
        )
        assert call_denied(denied) or denied.get("http_status", 200) in (401, 403)

    def test_07_key_a_cannot_send_on_own_alias(self):
        before = graph_count()
        denied = call_tool(
            self.agent_a,
            "m365_a",
            "send-mail",
            send_mail_arguments("allowed@example.com", "no", "no"),
        )
        assert call_denied(denied)
        assert graph_count() == before

    def test_08_no_authorization_code_in_login_log(self):
        events = {item.get("event") for item in login_log()}
        assert "authorization_code" not in events
        assert "client_credentials" in events
