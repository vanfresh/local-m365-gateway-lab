"""Minimal HTTP helpers for bearer lab tests."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def api(method: str, url: str, token: str | None = None, payload: dict | None = None) -> tuple[int, dict | list | str]:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, raw


def sse(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, str, str]:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = resp.headers.get("Content-Type") or ""
            return resp.status, ctype, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        ctype = exc.headers.get("Content-Type") or ""
        return exc.code, ctype, exc.read().decode(errors="replace")
