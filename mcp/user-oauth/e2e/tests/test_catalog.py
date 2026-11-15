"""Catalog: every tool in the pinned Softeria image, on a lab-only URL."""

from __future__ import annotations

import json

import pytest

from harness import (
    call_tool,
    graph_log,
    mcp_url,
    open_session,
    reset_graph_log,
    seed_servers,
    upstream_tools,
    wait_ready,
)
from mcp_client import Client, graph_audience

TOOLS: list[dict] = []


def pytest_generate_tests(metafunc):
    if "catalog_tool" not in metafunc.fixturenames:
        return
    wait_ready()
    TOOLS[:] = upstream_tools()
    metafunc.parametrize("catalog_tool", TOOLS, ids=lambda tool: tool.get("name", ""))


def json_type(prop: dict) -> str | None:
    if not isinstance(prop, dict):
        return None
    kind = prop.get("type")
    if isinstance(kind, str) and kind != "null":
        return kind
    for key in ("anyOf", "allOf", "oneOf"):
        for option in prop.get(key) or []:
            found = json_type(option)
            if found:
                return found
    return None


def sample(name: str, schema: dict) -> dict:
    if name == "read-mail-text":
        return {"messageIds": ["fixture"]}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    keys = required or list(properties)[:4]
    arguments = {}
    for key in keys:
        prop = properties.get(key) or {}
        kind = json_type(prop)
        lowered = key.lower()
        if lowered == "target":
            arguments[key] = (
                "/me/messages/fixture/attachments"
                if name == "get-upload-url"
                else "/me/drive/items/fixture/content"
            )
        elif "mailfolder" in lowered or lowered == "id":
            arguments[key] = "folder-x"
        elif "email" in lowered or lowered in {"to", "recipient"}:
            arguments[key] = ["allowed@example.com"]
        elif "channel" in lowered:
            arguments[key] = "channel-1"
        elif "chat" in lowered:
            arguments[key] = "chat-1"
        elif kind in {"integer", "number"}:
            arguments[key] = 1
        elif kind == "boolean":
            arguments[key] = False
        elif kind == "array":
            arguments[key] = ["fixture"]
        elif kind == "object" or key == "body":
            arguments[key] = {
                "message": {
                    "subject": "fixture",
                    "body": {"contentType": "Text", "content": "fixture"},
                    "toRecipients": [{"emailAddress": {"address": "allowed@example.com"}}],
                }
            }
        else:
            arguments[key] = "fixture"
    if name == "list-mail-folder-messages":
        return {"mailFolderId": "folder-x"}
    return arguments


class TestCatalog:
    @pytest.fixture(scope="class", autouse=True)
    def session(self, request):
        seed_servers(include_catalog=True)
        client = Client()
        open_session(client, "catalog")
        request.cls.client = client
        request.cls.token = client.tokens[mcp_url("catalog")]["access_token"]

    def test_01_softeria_published_at_least_one_tool(self):
        assert len(TOOLS) > 0

    def test_02_catalog_client_token_is_not_a_graph_token(self):
        assert not graph_audience(self.token)

    def test_03_catalog_tool(self, catalog_tool):
        name = catalog_tool.get("name", "")
        reset_graph_log()
        result = call_tool(self.client, "catalog", name, sample(name, catalog_tool.get("inputSchema") or {}))
        logs = [item for item in graph_log() if item.get("authorized")]
        text = json.dumps(result)
        body = result.get("body") if isinstance(result.get("body"), dict) else {}
        inner = body.get("result") if isinstance(body.get("result"), dict) else {}
        completed = (
            result.get("http_status", 500) < 400
            and inner.get("isError") is not True
            and "not found" not in text
            and "Listing tools" not in text
        )
        assert "rejected by connection policy" not in text
        assert self.token not in text
        assert self.token not in json.dumps(logs)
        assert logs or completed, text[:240]
