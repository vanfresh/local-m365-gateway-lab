#!/usr/bin/env python3
"""Seed Keycloak realm/clients/IdP and Nango integration; write .nango-secret."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK_ROOT = Path(os.environ.get("SEED_WORK_ROOT", str(ROOT))).resolve()
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "rest-nango-e2e")
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", str(ROOT / "compose.yaml"))
KEYCLOAK = os.environ.get("KEYCLOAK", "http://127.0.0.1:8085").rstrip("/")
NANGO = os.environ.get("NANGO", "http://127.0.0.1:3003").rstrip("/")
LOGIN = os.environ.get("LOGIN", "http://microsoft-login-stand-in:8080").rstrip("/")
LOGIN_PUBLIC = os.environ.get("LOGIN_PUBLIC", "http://127.0.0.1:8481").rstrip("/")
REALM = "hiagent-lab"
ADMIN_USER = "admin"
ADMIN_PASS = "admin"
CLIENT_A_SECRET = "secret-a"
CLIENT_B_SECRET = "secret-b"
IDP_CLIENT_ID = "keycloak-broker"
IDP_CLIENT_SECRET = "keycloak-broker-secret"
SECRET_FILE = WORK_ROOT / ".nango-secret"
PROVIDER_KEY = "microsoft-graph-lab"


def http(method: str, url: str, payload=None, headers=None, form=None, timeout=30):
    data = None
    hdrs = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
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


def wait(url: str, ok=lambda s, b: s < 500, seconds=180) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            status, body = http("GET", url)
            if ok(status, body):
                return
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit(f"timeout waiting for {url}")


def keycloak_admin_token() -> str:
    status, body = http(
        "POST",
        f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": ADMIN_USER,
            "password": ADMIN_PASS,
        },
    )
    if status >= 400 or not body.get("access_token"):
        raise SystemExit(f"keycloak admin token failed: {status} {body}")
    return body["access_token"]


def ensure_realm(token: str) -> None:
    status, realms = http("GET", f"{KEYCLOAK}/admin/realms", headers={"Authorization": f"Bearer {token}"})
    names = [r.get("realm") for r in realms] if isinstance(realms, list) else []
    if REALM in names:
        print(f"realm {REALM} exists")
        return
    status, body = http(
        "POST",
        f"{KEYCLOAK}/admin/realms",
        {
            "realm": REALM,
            "enabled": True,
            "sslRequired": "none",
            "registrationAllowed": False,
            "loginWithEmailAllowed": True,
            "duplicateEmailsAllowed": False,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    if status not in (201, 204, 409) and status >= 400:
        raise SystemExit(f"create realm failed: {status} {body}")
    print(f"created realm {REALM}")


def ensure_client(token: str, client_id: str, secret: str) -> None:
    status, clients = http(
        "GET",
        f"{KEYCLOAK}/admin/realms/{REALM}/clients?clientId={urllib.parse.quote(client_id)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    existing = clients[0] if isinstance(clients, list) and clients else None
    payload = {
        "clientId": client_id,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "secret": secret,
        "directAccessGrantsEnabled": True,
        "standardFlowEnabled": True,
        "redirectUris": ["*"],
        "webOrigins": ["*"],
        # PKCE optional — Hi Agent may send it; e2e works without.
        "attributes": {},
    }
    if existing:
        cid = existing["id"]
        status, body = http(
            "PUT",
            f"{KEYCLOAK}/admin/realms/{REALM}/clients/{cid}",
            {**existing, **payload, "id": cid},
            headers={"Authorization": f"Bearer {token}"},
        )
        # set secret
        http(
            "POST",
            f"{KEYCLOAK}/admin/realms/{REALM}/clients/{cid}/client-secret",
            headers={"Authorization": f"Bearer {token}"},
        )
        # Keycloak regenerates on POST; use client-secret PUT via register
        http(
            "PUT",
            f"{KEYCLOAK}/admin/realms/{REALM}/clients/{cid}",
            {**existing, **payload, "id": cid, "secret": secret},
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"updated client {client_id}")
        return
    status, body = http(
        "POST",
        f"{KEYCLOAK}/admin/realms/{REALM}/clients",
        payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    if status not in (201, 204) and status >= 400:
        raise SystemExit(f"create client {client_id} failed: {status} {body}")
    print(f"created client {client_id}")


def ensure_idp(token: str) -> None:
    status, idps = http(
        "GET",
        f"{KEYCLOAK}/admin/realms/{REALM}/identity-provider/instances",
        headers={"Authorization": f"Bearer {token}"},
    )
    aliases = [i.get("alias") for i in idps] if isinstance(idps, list) else []
    # Browser hits LOGIN_PUBLIC; Keycloak server hits LOGIN (docker DNS).
    config = {
        "alias": "microsoft-standin",
        "displayName": "Microsoft Stand-in",
        "providerId": "oidc",
        "enabled": True,
        "trustEmail": True,
        "storeToken": True,
        "linkOnly": False,
        "firstBrokerLoginFlowAlias": "first broker login",
        "config": {
            "authorizationUrl": f"{LOGIN_PUBLIC}/authorize",
            "tokenUrl": f"{LOGIN}/token",
            "userInfoUrl": f"{LOGIN}/userinfo",
            "logoutUrl": "",
            "issuer": LOGIN_PUBLIC,
            "clientId": IDP_CLIENT_ID,
            "clientSecret": IDP_CLIENT_SECRET,
            "clientAuthMethod": "client_secret_post",
            "syncMode": "IMPORT",
            "defaultScope": "openid offline_access Mail.Read Mail.Send",
            "validateSignature": "false",
            "useJwksUrl": "false",
            "pkceEnabled": "false",
            "backchannelSupported": "false",
            "loginHint": "true",
        },
    }
    if "microsoft-standin" in aliases:
        status, body = http(
            "PUT",
            f"{KEYCLOAK}/admin/realms/{REALM}/identity-provider/instances/microsoft-standin",
            config,
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"updated idp microsoft-standin status={status}")
        return
    status, body = http(
        "POST",
        f"{KEYCLOAK}/admin/realms/{REALM}/identity-provider/instances",
        config,
        headers={"Authorization": f"Bearer {token}"},
    )
    if status not in (201, 204) and status >= 400:
        raise SystemExit(f"create idp failed: {status} {body}")
    print("created idp microsoft-standin")


def ensure_broker_read_token(token: str) -> None:
    """Allow users (via default roles) to call /broker/{alias}/token."""
    status, clients = http(
        "GET",
        f"{KEYCLOAK}/admin/realms/{REALM}/clients?clientId=broker",
        headers={"Authorization": f"Bearer {token}"},
    )
    if not isinstance(clients, list) or not clients:
        print("broker client missing — skip read-token")
        return
    broker_id = clients[0]["id"]
    status, role = http(
        "GET",
        f"{KEYCLOAK}/admin/realms/{REALM}/clients/{broker_id}/roles/read-token",
        headers={"Authorization": f"Bearer {token}"},
    )
    if status >= 400 or not isinstance(role, dict):
        print(f"read-token role missing: {status}")
        return
    default_name = f"default-roles-{REALM}"
    status, _ = http(
        "POST",
        f"{KEYCLOAK}/admin/realms/{REALM}/roles/{urllib.parse.quote(default_name)}/composites",
        [role],
        headers={"Authorization": f"Bearer {token}"},
    )
    print(f"added broker.read-token to {default_name} status={status}")
    # Assign to existing users too
    status, users = http(
        "GET",
        f"{KEYCLOAK}/admin/realms/{REALM}/users?max=100",
        headers={"Authorization": f"Bearer {token}"},
    )
    if isinstance(users, list):
        for u in users:
            st, _ = http(
                "POST",
                f"{KEYCLOAK}/admin/realms/{REALM}/users/{u['id']}/role-mappings/clients/{broker_id}",
                [role],
                headers={"Authorization": f"Bearer {token}"},
            )
            print(f"assigned read-token to {u.get('username')} status={st}")


def ensure_browser_idp_redirect(token: str) -> None:
    """Disable review-profile / VERIFY_PROFILE so automated OAuth can finish."""
    status, executions = http(
        "GET",
        f"{KEYCLOAK}/admin/realms/{REALM}/authentication/flows/first%20broker%20login/executions",
        headers={"Authorization": f"Bearer {token}"},
    )
    if status >= 400 or not isinstance(executions, list):
        print(f"first broker login executions skipped: {status}")
    else:
        found = False
        for ex in executions:
            display = (ex.get("displayName") or "") + " " + (ex.get("providerId") or "")
            if "review" in display.lower() or ex.get("providerId") == "idp-review-profile":
                payload = {**ex, "requirement": "DISABLED"}
                st, _ = http(
                    "PUT",
                    f"{KEYCLOAK}/admin/realms/{REALM}/authentication/flows/first%20broker%20login/executions",
                    payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                print(f"disabled review profile execution status={st}")
                found = True
        if not found:
            print("no review-profile execution found (ok)")

    status, actions = http(
        "GET",
        f"{KEYCLOAK}/admin/realms/{REALM}/authentication/required-actions",
        headers={"Authorization": f"Bearer {token}"},
    )
    if isinstance(actions, list):
        for action in actions:
            if action.get("alias") in ("VERIFY_PROFILE", "TERMS_AND_CONDITIONS", "UPDATE_PASSWORD", "CONFIGURE_TOTP"):
                action["enabled"] = False
                action["defaultAction"] = False
                st, _ = http(
                    "PUT",
                    f"{KEYCLOAK}/admin/realms/{REALM}/authentication/required-actions/{action['alias']}",
                    action,
                    headers={"Authorization": f"Bearer {token}"},
                )
                print(f"disabled required action {action['alias']} status={st}")

    # Clear required actions on existing federated users
    status, users = http(
        "GET",
        f"{KEYCLOAK}/admin/realms/{REALM}/users?max=100",
        headers={"Authorization": f"Bearer {token}"},
    )
    if isinstance(users, list):
        for u in users:
            if u.get("requiredActions"):
                u["requiredActions"] = []
                st, _ = http(
                    "PUT",
                    f"{KEYCLOAK}/admin/realms/{REALM}/users/{u['id']}",
                    u,
                    headers={"Authorization": f"Bearer {token}"},
                )
                print(f"cleared requiredActions for {u.get('username')} status={st}")


def nango_secret_from_db() -> str:
    """Self-hosted Nango seeds a default 'dev' environment secret in Postgres."""
    import subprocess

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
    """Prefer the secret for the *currently running* Nango DB (file can go stale after recreate)."""
    secret = nango_secret_from_db()
    cached = SECRET_FILE.read_text().strip() if SECRET_FILE.exists() else ""
    if cached == secret:
        print("using existing .nango-secret")
    else:
        SECRET_FILE.write_text(secret + "\n", encoding="utf-8")
        if cached:
            print("refreshed .nango-secret from nango-db (was stale)")
        else:
            print("wrote .nango-secret from nango-db")
    # Sanity-check against API so a wrong NANGO host fails early
    status, body = http("GET", f"{NANGO}/integrations", headers={"Authorization": f"Bearer {secret}"})
    if status in (401, 403):
        raise SystemExit(
            f"nango secret from DB rejected by {NANGO} ({status}): {body}. "
            "Check NANGO URL / COMPOSE_PROJECT match the running stack."
        )
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
    print("waiting for Keycloak…")
    wait(f"{KEYCLOAK}/realms/master", ok=lambda s, b: s == 200)
    print("waiting for Nango…")
    wait(f"{NANGO}/", ok=lambda s, b: True, seconds=240)  # UI HTML is fine

    token = keycloak_admin_token()
    ensure_realm(token)
    ensure_client(token, "agent-a", CLIENT_A_SECRET)
    ensure_client(token, "agent-b", CLIENT_B_SECRET)
    ensure_idp(token)
    ensure_broker_read_token(token)
    ensure_browser_idp_redirect(token)

    secret = nango_signup_and_secret()
    ensure_nango_integration(secret)

    # Write env snippet for compose recreate
    env_path = WORK_ROOT / ".env"
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
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
        out.insert(0, "NANGO_ENCRYPTION_KEY=labNangoEncryptionKey32chars!!")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    print("ok")
    print(f"keycloak_realm={REALM}")
    print(f"client_agent_a=agent-a secret={CLIENT_A_SECRET}")
    print(f"client_agent_b=agent-b secret={CLIENT_B_SECRET}")
    print(f"auth_url={KEYCLOAK}/realms/{REALM}/protocol/openid-connect/auth")
    print(f"token_url={KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token")
    print(f"kc_idp_hint=microsoft-standin")
    print(f"nango_secret_set=true")


if __name__ == "__main__":
    main()
