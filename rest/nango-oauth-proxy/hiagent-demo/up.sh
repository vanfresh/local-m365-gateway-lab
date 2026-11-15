#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p logs

# Lab .env from committed example (generic keys; fine for public lab)
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
  else
    printf 'NANGO_ENCRYPTION_KEY=%s\nNANGO_SECRET_KEY=\n' "$(openssl rand -base64 32)" > .env
  fi
fi

echo "==> Starting stack (local Keycloak hostname)"
# Host ports offset from e2e (8085/3003/8481/8482/4400)
export KEYCLOAK_HOSTNAME_URL="${KEYCLOAK_HOSTNAME_URL:-http://127.0.0.1:8185}"
export LOGIN_ISSUER="${LOGIN_ISSUER:-http://127.0.0.1:8581}"
export KEYCLOAK_PUBLIC_URL="${KEYCLOAK_PUBLIC_URL:-http://host.docker.internal:8185}"
docker compose --env-file .env up -d --remove-orphans \
  keycloak-db keycloak nango-db nango-server \
  microsoft-login-stand-in microsoft-graph-stand-in \
  policy-proxy caddy

echo "==> Waiting for Keycloak"
i=0
while [ "$i" -lt 90 ]; do
  if curl -sf http://127.0.0.1:8185/realms/master >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if ! curl -sf http://127.0.0.1:8185/realms/master >/dev/null 2>&1; then
  echo "Keycloak not ready" >&2
  docker compose logs keycloak --tail 80 >&2
  exit 1
fi

echo "==> Starting Cloudflare quick tunnel"
docker compose --env-file .env up -d cloudflared

echo "==> Waiting for trycloudflare.com URL"
TUNNEL_URL=""
i=0
while [ "$i" -lt 60 ]; do
  TUNNEL_URL=$(docker compose logs cloudflared 2>/dev/null | sed -n 's/.*\(https:\/\/[-a-z0-9]*\.trycloudflare\.com\).*/\1/p' | tail -n 1 || true)
  if [ -n "$TUNNEL_URL" ]; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if [ -z "$TUNNEL_URL" ]; then
  echo "Could not find Cloudflare tunnel URL in cloudflared logs" >&2
  docker compose logs cloudflared --tail 80 >&2
  exit 1
fi

echo "$TUNNEL_URL" > .tunnel-url
export PUBLIC_BASE_URL="$TUNNEL_URL"
echo "==> Public URL: $PUBLIC_BASE_URL"

echo "==> Recreating Keycloak / login / policy-proxy with public URLs"
export KEYCLOAK_HOSTNAME_URL="$PUBLIC_BASE_URL"
export LOGIN_ISSUER="$PUBLIC_BASE_URL/login"
# JWT iss is the tunnel; policy-proxy must call the same issuer host
export KEYCLOAK_PUBLIC_URL="$PUBLIC_BASE_URL"
# Persist into .env so later compose recreates do not fall back to 127.0.0.1
# (that caused Cookie not found — broker redirected off the tunnel host).
tmp=$(mktemp)
grep -vE '^(KEYCLOAK_HOSTNAME_URL|LOGIN_ISSUER|KEYCLOAK_PUBLIC_URL)=' .env > "$tmp" || true
{
  cat "$tmp"
  echo "KEYCLOAK_HOSTNAME_URL=$PUBLIC_BASE_URL"
  echo "LOGIN_ISSUER=$PUBLIC_BASE_URL/login"
  echo "KEYCLOAK_PUBLIC_URL=$PUBLIC_BASE_URL"
} > .env
rm -f "$tmp"
docker compose --env-file .env up -d --force-recreate keycloak microsoft-login-stand-in caddy policy-proxy

i=0
while [ "$i" -lt 90 ]; do
  if curl -sf http://127.0.0.1:8185/realms/master >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if ! curl -sf http://127.0.0.1:8185/realms/master >/dev/null 2>&1; then
  echo "Keycloak not ready after recreate" >&2
  docker compose logs keycloak --tail 80 >&2
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Seeding Keycloak realm + Nango integration"
# Admin API via host port; IdP authorize/issuer via tunnel paths
LOGIN=http://microsoft-login-stand-in:8080 \
LOGIN_PUBLIC="$PUBLIC_BASE_URL/login" \
KEYCLOAK=http://127.0.0.1:8185 \
NANGO=http://127.0.0.1:3103 \
SEED_WORK_ROOT="$(pwd)" \
COMPOSE_PROJECT=rest-nango-hiagent-demo \
COMPOSE_FILE="$(pwd)/compose.yaml" \
python ../e2e/seed.py

echo "==> Recreating policy-proxy with NANGO_SECRET_KEY + public Keycloak"
docker compose --env-file .env up -d --force-recreate policy-proxy

i=0
while [ "$i" -lt 60 ]; do
  if curl -sf http://127.0.0.1:4500/health >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if ! curl -sf http://127.0.0.1:4500/health >/dev/null 2>&1; then
  echo "policy-proxy not healthy" >&2
  docker compose logs policy-proxy --tail 80 >&2
  exit 1
fi

SECRET_A=secret-a
SECRET_B=secret-b

# Full copy-paste guide always lands in the terminal (and .hiagent-banner for re-print).
cat <<EOF | tee .hiagent-banner

========================================================================
REST Nango OAuth proxy — Hi Agent demo is up

PUBLIC (tunnel):   $PUBLIC_BASE_URL
Local proxy:       http://127.0.0.1:4500
Local Keycloak:    http://127.0.0.1:8185
Local Nango:       http://127.0.0.1:3103
Caddy (local):     http://127.0.0.1:8090
Re-print later:    cat $(pwd)/.hiagent-banner

------------------------------------------------------------------------
URLS (these are NOT the same)
------------------------------------------------------------------------
  Plugin base (Create-tool grey prefix):  $PUBLIC_BASE_URL
  OpenAPI import (plugin setup only):     $PUBLIC_BASE_URL/openapi.json
  Token shim (access_token_url):          $PUBLIC_BASE_URL/oauth/token
  Full tool URL = plugin base + tool path
    e.g. $PUBLIC_BASE_URL/v1.0/me/mailFolders/{mailFolderId}/messages
  Do NOT put /openapi.json in the tool path.
  Do NOT use Keycloak .../openid-connect/token as access_token_url.

  No "full" / unrestricted plugin — only Agent A (read) + Agent B (send).

------------------------------------------------------------------------
A) CREATE PLUGIN — once per agent (not Integrate MCP)
------------------------------------------------------------------------
  Auth mode / UI grant_type:  Authorization Code   ← NORMAL (picks the flow)
  Plugin / server URL:        $PUBLIC_BASE_URL
  Import OpenAPI:             $PUBLIC_BASE_URL/openapi.json

  Paste every URL with NO trailing space
  (…/auth%20 → Keycloak "Page not found").

  --- Agent A (copy each line exactly) ---
  auth_url:
  $PUBLIC_BASE_URL/realms/hiagent-lab/protocol/openid-connect/auth

  access_token_url:   ← MUST be the shim (not Keycloak /token)
  $PUBLIC_BASE_URL/oauth/token

  refresh_token_url:
  $PUBLIC_BASE_URL/oauth/token

  client_id:
  agent-a

  client_secret:
  $SECRET_A

  redirect_url:
  (leave Hi Agent default callback)

  scope:   ← paste exactly this one word (not Mail.Read / Mail.Send)
  openid

  auth_content_type (if the form shows it):
  application/x-www-form-urlencoded

  --- Agent B ---
  auth_url:          same as Agent A auth_url
  access_token_url:  $PUBLIC_BASE_URL/oauth/token
  refresh_token_url: $PUBLIC_BASE_URL/oauth/token
  client_id:         agent-b
  client_secret:     $SECRET_B
  redirect_url:      Hi Agent default callback
  scope:             openid
  auth_content_type: application/x-www-form-urlencoded
  UI grant_type:     Authorization Code

  Optional on auth_url only: ?kc_idp_hint=microsoft-standin  (no space before ?)

  Save → click authorize / HereAuthorize → browser login
  (Keycloak may go straight to Microsoft stand-in). No Nango Connect.
  Do NOT paste Nango secret / Microsoft app id / Graph tokens into Hi Agent.

------------------------------------------------------------------------
B) CREATE TOOL — Agent A plugin (read)
------------------------------------------------------------------------
  Step 1 Basic configuration
    Name*:            listMailFolderMessages
    Description*:     List messages in a mail folder (lab Graph stand-in).
    Tool path*:       /v1.0/me/mailFolders/folder-x/messages
                      ↑ use LITERAL folder-x — Hi Agent does NOT substitute {mailFolderId}
                      (path .../{mailFolderId}/... becomes %7BmailFolderId%7D → 403)
    Request method*:  GET
    Streaming output: UNCHECKED  ← NOT SSE; plain application/json
    Timeout:          1 Minute (ok)

  Step 2 Input parameters
    None required if tool path already contains folder-x.
    (If you kept {mailFolderId} in the path, add mailFolderId=folder-x in body/query;
     the lab proxy will rewrite it — but prefer the literal path above.)

  Step 3 Output parameters (plain JSON, not SSE) — MUST match Graph envelope
    Root type: object
    Required root field: value  (array of message objects)
    Do NOT mark id/subject/from as required at the ROOT — they live under value[0].
    Wrong schema → "Error at /id: property id is missing" even when HTTP 200 is fine.

    Example 200 (this is what Graph/lab returns):
    {
      "value": [
        {
          "id": "msg-user-a",
          "subject": "hello-user-a",
          "parentFolderId": "folder-x",
          "from": { "emailAddress": { "address": "user-a@example.com" } }
        }
      ]
    }

    If the UI wants nested fields, declare something like:
      value          | array  | required
        [] item      | object
          id         | string
          subject    | string
          parentFolderId | string
          from       | object
            emailAddress | object
              address  | string
    Or set output to raw/unvalidated JSON if the product allows it.

  Step 4 Debug → subject hello-user-a (inside value[0])
         If "oauth2 authorization required" → authorize the plugin first (section A).
         If policy_denied ...%7BmailFolderId%7D → change tool path to literal folder-x.
         If outputSchema validation failed ... /id missing → fix Step 3 (root is value[], not a message).

------------------------------------------------------------------------
C) CREATE TOOL — Agent B plugin (send)
------------------------------------------------------------------------
  Step 1 Basic configuration
    Name*:            sendMail
    Description*:     Send mail via Graph stand-in (allowlisted recipients).
    Tool path*:       /v1.0/me/sendMail
    Request method*:  POST
    Streaming output: UNCHECKED  ← NOT SSE
    Timeout:          1 Minute (ok)

  Step 2 Input (JSON body)
    {
      "message": {
        "subject": "agent-b",
        "body": { "contentType": "Text", "content": "ok" },
        "toRecipients": [
          { "emailAddress": { "address": "allowed@example.com" } }
        ]
      },
      "saveToSentItems": false
    }

  Step 3 Output example 200:
    { "id": "created-1", "subject": "hello-user-b", "parentFolderId": "" }

  Step 4 Debug send to allowed@example.com → success; read should 403

------------------------------------------------------------------------
D) ATTACH + TEST PROMPTS
------------------------------------------------------------------------
  Attach Agent A plugin → Agent A; Agent B plugin → Agent B.
  Agent A: list folder messages mailFolderId=folder-x → hello-user-a
  Agent B: send mail to allowed@example.com → success; refuse read

------------------------------------------------------------------------
E) TROUBLESHOOTING (seen in this lab)
------------------------------------------------------------------------
  Page not found on authorize
    → trailing space in auth_url (auth%20). Re-paste auth_url with no space.

  Authorization failed / Missing form parameter: grant_type
    → UI "grant_type=Authorization Code" is fine/normal.
    → access_token_url is still Keycloak .../openid-connect/token.
    → Set access_token_url + refresh_token_url to:
      $PUBLIC_BASE_URL/oauth/token
    → That shim injects grant_type and form-posts to Keycloak.

  Cookie not found
    → Keycloak redirected to 127.0.0.1 instead of the tunnel (hostname drift).
    → Re-run ./up.sh (persists KEYCLOAK_HOSTNAME_URL in .env).
    → Retry authorize in a fresh browser tab.

  Debug: oauth2 authorization required
    → Finish plugin OAuth (HereAuthorize) before debugging the tool.

  policy_denied .../mailFolders/%7BmailFolderId%7D/...
    → Hi Agent did not substitute {mailFolderId}; it called the literal braces.
    → Set tool path to: /v1.0/me/mailFolders/folder-x/messages

  outputSchema validation failed: Error at "/id": property "id" is missing
    → HTTP/Graph succeeded; your tool OUTPUT schema is wrong.
    → Response root is { "value": [ { "id": ..., "subject": ... } ] }
    → Require "value" at root; put id/subject under each array item — not at root.

  scope
    → use exactly: openid
    → do NOT put Mail.Read / Mail.Send on the Hi Agent→Keycloak plugin.

Graph log:  $(pwd)/logs/graph.jsonl
Audit log:  $(pwd)/logs/audit.log
Stop:       ./down.sh
Guide:      ../README.md
========================================================================
EOF
