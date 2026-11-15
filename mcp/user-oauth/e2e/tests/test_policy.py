"""Policy: token boundary, default deny, folders, send, and two users.

Cases run in definition order. Later cases reuse the login opened by an earlier one.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from harness import (
    call_denied,
    call_tool,
    channel_arguments,
    chat_arguments,
    control_login,
    graph_count,
    graph_log,
    load_policy,
    login_log,
    mcp_url,
    open_session,
    reset_graph_log,
    seed_servers,
    send_mail_arguments,
    tool_names,
    upstream_tools,
    wait_ready,
    write_policy,
    SOFTERIA,
)
from mcp_client import Client, graph_audience

M365_TOOLS = {
    "list-mail-folder-messages",
    "send-mail",
    "send-channel-message",
    "send-chat-message",
    "list-sharepoint-site-items",
}


def pytest_generate_tests(metafunc):
    if "blocked_tool" not in metafunc.fixturenames:
        return
    wait_ready()
    names = [tool["name"] for tool in upstream_tools() if tool["name"] not in M365_TOOLS]
    metafunc.parametrize("blocked_tool", names)


class TestPolicy:
    @pytest.fixture(scope="class", autouse=True)
    def session(self, request):
        seed_servers(include_catalog=True)
        control_login({"reset_subjects": True, "refresh": "ok", "expires_in": 3600})
        client = Client()
        open_session(client, "m365")
        request.cls.client = client
        request.cls.token = client.tokens[mcp_url("m365")]
        yield
        control_login({"expires_in": 3600, "refresh": "ok"})

    def test_01_client_token_is_not_a_graph_token(self):
        assert not graph_audience(self.token["access_token"])

    def test_02_folder_x_read_reaches_graph(self):
        reset_graph_log()
        self.__class__.allowed = call_tool(
            self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-x"}
        )
        self.__class__.read_logs = graph_log()
        assert len(self.read_logs) == 1 and self.read_logs[0].get("authorized") is True

    def test_03_graph_bearer_differs_from_the_client_token(self):
        assert self.token["access_token"] not in json.dumps(self.read_logs)

    def test_04_graph_result_is_for_user_a(self):
        assert "hello-user-a" in json.dumps(self.allowed)

    def test_05_wrong_folder_is_rejected_before_graph(self):
        before = graph_count()
        denied = call_tool(self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-z"})
        assert call_denied(denied)
        assert graph_count() == before

    def test_06_missing_folder_is_rejected_before_graph(self):
        before = graph_count()
        missing = call_tool(self.client, "m365", "list-mail-folder-messages", {})
        assert call_denied(missing)
        assert graph_count() == before

    def test_07_extra_argument_is_rejected_before_graph(self):
        before = graph_count()
        extra = call_tool(
            self.client,
            "m365",
            "list-mail-folder-messages",
            {"mailFolderId": "folder-x", "search": "secret"},
        )
        assert call_denied(extra)
        assert graph_count() == before

    def test_08_tools_list_shows_only_m365_allowed_tools(self):
        listed = tool_names(self.client, "m365")
        prefix = "m365-"
        bare = [name[len(prefix):] if name.startswith(prefix) else name for name in listed]
        assert sorted(bare) == sorted(M365_TOOLS)

    def test_09_other_catalog_tool_is_rejected(self, blocked_tool):
        before = graph_count()
        result = call_tool(self.client, "m365", blocked_tool, {"mailFolderId": "folder-x"})
        assert call_denied(result)
        assert graph_count() == before

    def test_10_client_refresh_does_not_call_microsoft(self):
        control_login({"reset_log": True})
        stored = self.token["access_token"]
        self.client.refresh(mcp_url("m365"))
        assert self.client.tokens[mcp_url("m365")]["access_token"] != stored
        grants = [item.get("grant_type") for item in login_log() if item.get("event") == "token"]
        assert "refresh_token" not in grants and "authorization_code" not in grants

    def test_11_m365_token_is_rejected_on_catalog(self):
        replay = self.client.mcp(
            mcp_url("catalog"),
            "tools/call",
            {"name": "list-mail-folder-messages", "arguments": {"mailFolderId": "folder-x"}},
            token=self.token["access_token"],
        )
        assert call_denied(replay)

    def test_12_second_login_is_user_b(self):
        open_session(self.client, "m365")
        reset_graph_log()
        second = call_tool(self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-y"})
        assert "hello-user-b" in json.dumps(second) and graph_log()
        assert "hello-user-a" not in json.dumps(second)

    def test_13_allowed_recipient_reaches_graph(self):
        reset_graph_log()
        sent = call_tool(self.client, "m365", "send-mail", send_mail_arguments("allowed@example.com", "status", "ok"))
        send_logs = [item for item in graph_log() if item.get("authorized")]
        assert send_logs and "allowed@example.com" in json.dumps(send_logs)
        assert "rejected by connection policy" not in json.dumps(sent)

    def test_14_other_recipient_is_rejected_before_graph(self):
        before = graph_count()
        blocked = call_tool(self.client, "m365", "send-mail", send_mail_arguments("other@example.com", "no", "no"))
        assert call_denied(blocked)
        assert graph_count() == before

    def test_15_cc_is_rejected_before_graph(self):
        before = graph_count()
        copied = call_tool(
            self.client,
            "m365",
            "send-mail",
            send_mail_arguments("allowed@example.com", "no", "no", cc="other@example.com"),
        )
        assert call_denied(copied)
        assert graph_count() == before

    def test_16_graph_batch_is_rejected_before_graph(self):
        before = graph_count()
        denied = call_tool(self.client, "m365", "graph-batch", {"requests": []})
        assert call_denied(denied)
        assert graph_count() == before

    def test_17_allowed_channel_reaches_graph(self):
        reset_graph_log()
        call_tool(self.client, "m365", "send-channel-message", channel_arguments("channel-1", "hello"))
        assert any(item.get("authorized") and "channel-1" in item.get("path", "") for item in graph_log())

    def test_18_wrong_channel_is_rejected_before_graph(self):
        before = graph_count()
        wrong = call_tool(self.client, "m365", "send-channel-message", channel_arguments("channel-2", "no"))
        assert graph_count() == before and call_denied(wrong)

    def test_19_allowed_chat_reaches_graph(self):
        chat = call_tool(self.client, "m365", "send-chat-message", chat_arguments("chat-1", "hello"))
        assert "rejected by connection policy" not in json.dumps(chat)
        assert any(item.get("authorized") and "chat-1" in item.get("path", "") for item in graph_log())

    def test_20_wrong_chat_is_rejected_before_graph(self):
        before = graph_count()
        wrong = call_tool(self.client, "m365", "send-chat-message", chat_arguments("chat-2", "no"))
        assert graph_count() == before and call_denied(wrong)

    def test_21_direct_softeria_call_with_the_client_token_is_rejected(self):
        raw = Client()
        direct = raw.request(
            "POST",
            SOFTERIA + "/mcp",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "list-mail-folder-messages", "arguments": {"mailFolderId": "folder-x"}},
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer " + self.token["access_token"],
            },
        )
        text = direct.text()
        assert direct.status >= 400 or "InvalidAuthenticationToken" in text or "audience rejected" in text

    def test_22_empty_policy_tools_are_rejected_before_graph(self):
        original = load_policy()
        updated = dict(original)
        updated["m365"] = {}
        write_policy(updated)
        try:
            before = graph_count()
            removed = call_tool(
                self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-x"}
            )
            assert call_denied(removed) and graph_count() == before, json.dumps(removed)[:240]
        finally:
            write_policy(original)

    def test_22b_mcp_not_in_policy_file_is_not_guarded(self):
        original = load_policy()
        updated = dict(original)
        updated.pop("m365", None)
        write_policy(updated)
        try:
            reset_graph_log()
            allowed = call_tool(
                self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-x"}
            )
            assert any(item.get("authorized") for item in graph_log()), json.dumps(allowed)[:240]
            assert "rejected by connection policy" not in json.dumps(allowed)
        finally:
            write_policy(original)

    def test_23_policy_file_allowlist_change_applies_without_a_restart(self):
        original = load_policy()
        updated = dict(original)
        m365 = dict(updated.get("m365") or {})
        send_mail = dict(m365.get("send-mail") or {})
        send_mail["to"] = ["allowed@example.com", "added@example.com"]
        m365["send-mail"] = send_mail
        updated["m365"] = m365
        write_policy(updated)
        try:
            reset_graph_log()
            added = call_tool(
                self.client, "m365", "send-mail", send_mail_arguments("added@example.com", "new", "ok")
            )
            assert "added@example.com" in json.dumps(graph_log())
            assert "rejected by connection policy" not in json.dumps(added)
        finally:
            write_policy(original)

    def test_24_sharepoint_site_a_reaches_graph(self):
        reset_graph_log()
        result = call_tool(
            self.client, "m365", "list-sharepoint-site-items", {"siteId": "site-a"}
        )
        assert any(item.get("authorized") for item in graph_log()), json.dumps(result)[:240]
        assert "rejected by connection policy" not in json.dumps(result)

    def test_25_wrong_sharepoint_site_is_rejected_before_graph(self):
        before = graph_count()
        wrong = call_tool(
            self.client, "m365", "list-sharepoint-site-items", {"siteId": "site-b"}
        )
        assert call_denied(wrong)
        assert graph_count() == before

    def test_26_unnamed_sharepoint_tool_is_rejected_before_graph(self):
        before = graph_count()
        denied = call_tool(
            self.client, "m365", "create-sharepoint-list", {"siteId": "site-a"}
        )
        assert call_denied(denied)
        assert graph_count() == before

    def test_27_client_credentials_grant_is_rejected(self):
        credentials = self.client.request(
            "POST",
            self.token["token_endpoint"],
            data=b"grant_type=client_credentials&client_id=litellm-gateway&client_secret=litellm-gateway-secret",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert credentials.status >= 400

    def test_28_expired_graph_token_is_refreshed_by_litellm(self):
        control_login({"expires_in": 30, "refresh": "ok", "reset_log": True})
        open_session(self.client, "m365")
        time.sleep(2)
        before = len([item for item in login_log() if item.get("grant_type") == "refresh_token"])
        reset_graph_log()
        held = self.client.tokens[mcp_url("m365")]["access_token"]
        refreshed = call_tool(self.client, "m365", "list-mail-folder-messages", {"mailFolderId": "folder-x"})
        after = len([item for item in login_log() if item.get("grant_type") == "refresh_token"])
        assert after > before, f"refresh grants {before}->{after}"
        assert any(item.get("authorized") for item in graph_log())
        assert "hello-" in json.dumps(refreshed)
        assert self.client.tokens[mcp_url("m365")]["access_token"] == held
        control_login({"expires_in": 3600, "refresh": "ok"})

    def test_29_audit_log_names_the_connection_and_tool_only(self):
        audit_path = os.environ["AUDIT_LOG"]
        audit = open(audit_path, encoding="utf-8").read() if os.path.exists(audit_path) else ""
        assert "m365" in audit and "list-mail-folder-messages" in audit
        assert "hello-user-a" not in audit
