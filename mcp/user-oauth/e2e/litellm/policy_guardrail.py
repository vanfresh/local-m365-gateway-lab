"""Opt-in default-deny connection policy for MCP tool calls.

Reads connection_policy.yaml on every call. Operators edit that file.
Only MCP names listed there are enforced. An unknown tool, a wrong
argument, or an unreadable file rejects the call before Microsoft Graph
is contacted. MCP servers not named in the file are left alone.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import yaml
from fastapi import HTTPException
from litellm.integrations.custom_guardrail import CustomGuardrail

EMAIL = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
META = {"to", "deny", "only"}
ALWAYS_DENY = {"graph-batch", "search-query"}
POLICY_PATH = os.environ.get("CONNECTION_POLICY", "/app/connection_policy.yaml")
AUDIT_PATH = os.environ.get("AUDIT_LOG", "/policy/audit.log")


def _load_policy() -> dict:
    with open(POLICY_PATH, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("connection policy must be a mapping")
    return data


def _emails(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            found.extend(_emails(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_emails(item))
    elif isinstance(value, str):
        found.extend(match.group(0).lower() for match in EMAIL.finditer(value))
    return found


def _walk_keys(value: Any, found: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            _walk_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_keys(item, found)


def _values_for(value: Any, names: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in names:
                found.append(item)
            found.extend(_values_for(item, names))
    elif isinstance(value, list):
        for item in value:
            found.extend(_values_for(item, names))
    return found


def _tool_name(data: dict, paths: list[str]) -> str:
    name = str(data.get("mcp_tool_name") or "")
    for path in paths:
        prefix = path + "-"
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _connection(data: dict, policy: dict) -> str | None:
    server = str(data.get("mcp_server_name") or "")
    if server in policy:
        return server
    name = str(data.get("mcp_tool_name") or "")
    matches = [path for path in policy if name.startswith(path + "-")]
    if len(matches) == 1:
        return matches[0]
    return None


def _audit(path: str, tool: str, decision: str) -> None:
    directory = os.path.dirname(AUDIT_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(AUDIT_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"connection": path, "tool": tool, "decision": decision}) + "\n")


def _reject(reason: str) -> None:
    raise HTTPException(status_code=403, detail=f"rejected by connection policy: {reason}")


def _value_allowed(found: list[Any], expected: Any) -> bool:
    if not found:
        return False
    allowed = expected if isinstance(expected, list) else [expected]
    allowed_text = {str(item) for item in allowed}
    return any(str(item) in allowed_text for item in found)


def enforce(data: dict) -> None:
    if not data.get("mcp_tool_name"):
        return
    try:
        policy = _load_policy()
    except Exception as exc:
        detail = str(exc).splitlines()[0][:160]
        if "://" in detail:
            detail = exc.__class__.__name__
        _reject(f"policy unavailable: {detail}")
        return
    paths = list(policy)
    tool = _tool_name(data, paths)
    arguments = data.get("mcp_arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            _reject("arguments must be an object")
    if not isinstance(arguments, dict):
        _reject("arguments must be an object")
    path = _connection(data, policy)
    # Opt-in default-deny: only MCP names listed in the YAML are enforced.
    # Other MCP servers on this gateway are left alone.
    if path is None:
        return
    entry = policy[path] or {}
    if not isinstance(entry, dict):
        _reject(f"invalid policy for {path}")
    if entry.get("allow_all") is True:
        _audit(path, tool, "allow")
        return
    if tool in ALWAYS_DENY:
        _audit(path, tool, "reject")
        _reject(f"{tool} is not allowed on {path}")
    rules = entry.get(tool)
    if not isinstance(rules, dict):
        _audit(path, tool, "reject")
        _reject(f"{tool} is not allowed on {path}")
    constraints = {key: value for key, value in rules.items() if key not in META}
    if rules.get("only") is True:
        allowed_keys = set(constraints)
        if set(arguments) - allowed_keys:
            _audit(path, tool, "reject")
            _reject("extra arguments are not allowed")
    denied = {str(item).lower() for item in (rules.get("deny") or [])}
    if denied:
        present: list[str] = []
        _walk_keys(arguments, present)
        if any(key.lower() in denied for key in present):
            _audit(path, tool, "reject")
            _reject("denied argument is present")
    if "to" in rules:
        allow = [str(item).lower() for item in (rules.get("to") or [])]
        found = _emails(arguments)
        if not found or any(address not in allow for address in found):
            _audit(path, tool, "reject")
            _reject("recipient is not allowed")
    for name, expected in constraints.items():
        found = _values_for(arguments, {name.lower()})
        if not _value_allowed(found, expected):
            _audit(path, tool, "reject")
            _reject(f"{name} is not allowed")
    _audit(path, tool, "allow")


class PolicyGuardrail(CustomGuardrail):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # tools/list also runs this hook, once per description. Only a tool call
        # is a request to Graph. Raising here during a list hides the tool.
        if str(call_type) == "call_mcp_tool":
            enforce(data)
        return data
