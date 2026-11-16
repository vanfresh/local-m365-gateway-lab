#!/usr/bin/env python3
"""Seed Nango integration only (no Keycloak); write .nango-secret."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK_ROOT = Path(os.environ.get("SEED_WORK_ROOT", str(ROOT))).resolve()
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "a2a-bearer-e2e")
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", str(ROOT / "compose.yaml"))
NANGO = os.environ.get("NANGO", "http://127.0.0.1:3403").rstrip("/")
SECRET_FILE = WORK_ROOT / ".nango-secret"
PROVIDER_KEY = "copilot-a2a-lab"
IDP_CLIENT_ID = "keycloak-broker"
IDP_CLIENT_SECRET = "keycloak-broker-secret"


def http(method: str, url: str, payload=None, headers=None, timeout=30):
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"raw": raw[:800]}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:800]}
        return exc.code, body


def wait(url: str, seconds=240) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            status, _ = http("GET", url)
            if status != 0:
                return
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit(f"timeout waiting for {url}")


def nango_secret_from_db() -> str:
    out = subprocess.check_output(
        [
            "docker",
            "compose",
            "-p",
            COMPOSE_PROJECT,
            "-f",
            COMPOSE_FILE,
            "exec",
            "-T",
            "nango-db",
            "psql",
            "-U",
            "nango",
            "-d",
            "nango",
            "-tAc",
            "SELECT secret_key FROM _nango_environments WHERE name='dev' LIMIT 1;",
        ],
        cwd=str(WORK_ROOT),
        text=True,
    ).strip()
    if not out:
        raise SystemExit("no Nango dev secret_key in database")
    return out


def nango_signup_and_secret() -> str:
    secret = nango_secret_from_db()
    cached = SECRET_FILE.read_text().strip() if SECRET_FILE.exists() else ""
    if cached == secret:
        print("using existing .nango-secret")
    else:
        SECRET_FILE.write_text(secret + "\n", encoding="utf-8")
        print("refreshed .nango-secret from nango-db" if cached else "wrote .nango-secret from nango-db")
    status, body = http("GET", f"{NANGO}/integrations", headers={"Authorization": f"Bearer {secret}"})
    if status in (401, 403):
        raise SystemExit(f"nango secret rejected by {NANGO} ({status}): {body}")
    return secret


def ensure_nango_integration(secret: str) -> None:
    headers = {"Authorization": f"Bearer {secret}"}
    status, body = http("GET", f"{NANGO}/integrations", headers=headers)
    items = body.get("data") if isinstance(body, dict) else body
    found = False
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("unique_key") == PROVIDER_KEY:
            found = True
            break
    if not found:
        status, body = http(
            "POST",
            f"{NANGO}/integrations",
            {"provider": PROVIDER_KEY, "unique_key": PROVIDER_KEY},
            headers=headers,
        )
        if status >= 400:
            raise SystemExit(f"nango integration create failed: {status} {body}")
        print(f"created nango integration status={status}")
    status, body = http(
        "PATCH",
        f"{NANGO}/integrations/{PROVIDER_KEY}",
        {
            "credentials": {
                "type": "OAUTH2",
                "client_id": IDP_CLIENT_ID,
                "client_secret": IDP_CLIENT_SECRET,
                "scopes": "",
            }
        },
        headers=headers,
    )
    if status >= 400:
        raise SystemExit(f"nango integration credentials failed: {status} {body}")
    print(f"patched nango integration credentials status={status}")


def main() -> None:
    print("waiting for Nango…")
    wait(f"{NANGO}/")
    secret = nango_signup_and_secret()
    ensure_nango_integration(secret)

    env_path = WORK_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out = []
    seen_secret = False
    for line in lines:
        if line.startswith("NANGO_SECRET_KEY="):
            out.append(f"NANGO_SECRET_KEY={secret}")
            seen_secret = True
        else:
            out.append(line)
    if not seen_secret:
        out.append(f"NANGO_SECRET_KEY={secret}")
    if not any(l.startswith("NANGO_ENCRYPTION_KEY=") for l in out):
        out.insert(0, "NANGO_ENCRYPTION_KEY=bG9jYWwtbTM2NS1nYXRld2F5LW5hbmdvLWtleSEhISE=")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("ok")
    print(f"nango_provider={PROVIDER_KEY}")
    print("bearer_keys=lab-agent-a,lab-agent-b")


if __name__ == "__main__":
    main()
