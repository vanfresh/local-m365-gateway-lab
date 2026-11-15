"""Per-agent tool isolation via separate MCP URLs.

Hi Agent registers one plugin per agent permission set in this lab model.
agent_a may only read mail in folder-x. agent_b may only send mail to the allowlist.
"""

from __future__ import annotations

import json

import pytest

from harness import (
    call_denied,
    call_tool,
    control_login,
    graph_count,
    graph_log,
    mcp_url,
    open_session,
    reset_graph_log,
    seed_servers,
    send_mail_arguments,
    tool_names,
)
from mcp_client import Client


class TestAgents:
    @pytest.fixture(scope="class", autouse=True)
    def session(self, request):
        seed_servers()
        control_login({"reset_subjects": True, "refresh": "ok", "expires_in": 3600})
        a = Client()
        b = Client()
        open_session(a, "agent_a")
        open_session(b, "agent_b")
        request.cls.agent_a = a
        request.cls.agent_b = b
        request.cls.token_a = a.tokens[mcp_url("agent_a")]["access_token"]
        request.cls.token_b = b.tokens[mcp_url("agent_b")]["access_token"]
        yield
        control_login({"expires_in": 3600, "refresh": "ok"})

    def test_01_agent_a_tools_list_is_read_only(self):
        listed = tool_names(self.agent_a, "agent_a")
        prefix = "agent_a-"
        bare = [name[len(prefix):] if name.startswith(prefix) else name for name in listed]
        assert bare == ["list-mail-folder-messages"]

    def test_02_agent_b_tools_list_is_send_only(self):
        listed = tool_names(self.agent_b, "agent_b")
        prefix = "agent_b-"
        bare = [name[len(prefix):] if name.startswith(prefix) else name for name in listed]
        assert bare == ["send-mail"]

    def test_03_agent_a_can_read_folder_x(self):
        reset_graph_log()
        result = call_tool(
            self.agent_a, "agent_a", "list-mail-folder-messages", {"mailFolderId": "folder-x"}
        )
        assert any(item.get("authorized") for item in graph_log()), json.dumps(result)[:240]
        assert "rejected by connection policy" not in json.dumps(result)

    def test_04_agent_a_cannot_send_mail(self):
        before = graph_count()
        denied = call_tool(
            self.agent_a,
            "agent_a",
            "send-mail",
            send_mail_arguments("allowed@example.com", "no", "no"),
        )
        assert call_denied(denied)
        assert graph_count() == before

    def test_05_agent_b_can_send_mail(self):
        reset_graph_log()
        result = call_tool(
            self.agent_b,
            "agent_b",
            "send-mail",
            send_mail_arguments("allowed@example.com", "agent-b", "ok"),
        )
        assert any(item.get("authorized") for item in graph_log()), json.dumps(result)[:240]
        assert "rejected by connection policy" not in json.dumps(result)

    def test_06_agent_b_cannot_read_mail(self):
        before = graph_count()
        denied = call_tool(
            self.agent_b, "agent_b", "list-mail-folder-messages", {"mailFolderId": "folder-x"}
        )
        assert call_denied(denied)
        assert graph_count() == before

    def test_07_agent_a_token_is_rejected_on_agent_b(self):
        replay = self.agent_a.mcp(
            mcp_url("agent_b"),
            "tools/call",
            {
                "name": "send-mail",
                "arguments": send_mail_arguments("allowed@example.com", "replay", "no"),
            },
            token=self.token_a,
        )
        assert call_denied(replay)

    def test_08_agent_b_token_is_rejected_on_agent_a(self):
        replay = self.agent_b.mcp(
            mcp_url("agent_a"),
            "tools/call",
            {"name": "list-mail-folder-messages", "arguments": {"mailFolderId": "folder-x"}},
            token=self.token_b,
        )
        assert call_denied(replay)
