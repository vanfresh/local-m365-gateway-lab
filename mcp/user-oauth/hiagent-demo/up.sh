#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p logs

echo "==> Starting demo stack (without public URL yet)"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://127.0.0.1:4100}"
docker compose up -d --remove-orphans litellm-token-database microsoft-login-stand-in microsoft-graph-stand-in softeria-m365-mcp litellm-gateway caddy

echo "==> Waiting for LiteLLM"
i=0
while [ "$i" -lt 60 ]; do
  if curl -sf http://127.0.0.1:4100/health/liveliness >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if ! curl -sf http://127.0.0.1:4100/health/liveliness >/dev/null 2>&1; then
  echo "LiteLLM did not become healthy" >&2
  docker compose logs litellm-gateway --tail 80 >&2
  exit 1
fi

echo "==> Starting Cloudflare quick tunnel"
docker compose up -d cloudflared

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

echo "==> Recreating LiteLLM with PROXY_BASE_URL=$PUBLIC_BASE_URL"
docker compose up -d --force-recreate litellm-gateway
i=0
while [ "$i" -lt 60 ]; do
  if curl -sf http://127.0.0.1:4100/health/liveliness >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if ! curl -sf http://127.0.0.1:4100/health/liveliness >/dev/null 2>&1; then
  echo "LiteLLM did not become healthy after recreate" >&2
  docker compose logs litellm-gateway --tail 80 >&2
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Seeding m365, agent_a, agent_b (OAuth-ready; no token priming)"
GATEWAY=http://127.0.0.1:4100 \
PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \
LOGIN_AUTHORIZE_URL="$PUBLIC_BASE_URL/login/authorize" \
LOGIN_TOKEN_URL=http://microsoft-login-stand-in:8080/token \
SOFTERIA_INTERNAL=http://softeria-m365-mcp:3000 \
python seed.py

cat <<EOF

========================================================================
User-OAuth Hi Agent demo is up

LiteLLM UI (local):     http://127.0.0.1:4100/ui
  Master key (UI only): sk-local-master
  Confirm MCP servers:  m365, agent_a, agent_b

Public base (tunnel):   $PUBLIC_BASE_URL

Hi Agent MCP plugin URLs (Streamable HTTP):
  m365 (full ceiling):  $PUBLIC_BASE_URL/m365/mcp
  agent_a (read only):  $PUBLIC_BASE_URL/agent_a/mcp
  agent_b (send only):  $PUBLIC_BASE_URL/agent_b/mcp

Intended auth: MCP OAuth (Authorization Code) — not Bearer virtual key.
  auth_url:           $PUBLIC_BASE_URL/login/authorize
  access_token_url:   $PUBLIC_BASE_URL/login/token
  refresh_token_url:  $PUBLIC_BASE_URL/login/token
  client_id:          litellm-gateway
  client_secret:      litellm-gateway-secret
  scopes:             openid offline_access Mail.Read Mail.Send Sites.Read.All
  auth_content_type:  application/x-www-form-urlencoded
  redirect_url:       leave Hi Agent's callback (e.g. …/product/llm/info/oauth)

Today's Integrate MCP UI may only offer Bearer/Basic/API Key (no OAuth).
  Until OAuth appears there, prove the flow with: cd ../e2e && ./run.sh
  Do not treat Bearer priming as the product design for this lab.

Login stand-in (browser via tunnel):
  $PUBLIC_BASE_URL/login/authorize

Graph log:  $(pwd)/logs/graph.jsonl
Audit log:  $(pwd)/logs/audit.log

Next: open README.md → Integrate with Hi Agent.
Stop with: ./down.sh
========================================================================
EOF
