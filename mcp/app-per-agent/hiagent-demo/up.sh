#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p logs

echo "==> Starting app-per-agent demo stack (without public URL yet)"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://127.0.0.1:4300}"
docker compose up -d --remove-orphans litellm-token-database microsoft-login-stand-in microsoft-graph-stand-in softeria-m365-mcp litellm-gateway caddy

echo "==> Waiting for LiteLLM"
i=0
while [ "$i" -lt 60 ]; do
  if curl -sf http://127.0.0.1:4300/health/liveliness >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if ! curl -sf http://127.0.0.1:4300/health/liveliness >/dev/null 2>&1; then
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
  if curl -sf http://127.0.0.1:4300/health/liveliness >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if ! curl -sf http://127.0.0.1:4300/health/liveliness >/dev/null 2>&1; then
  echo "LiteLLM did not become healthy after recreate" >&2
  docker compose logs litellm-gateway --tail 80 >&2
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Seeding m365_a / m365_b (M2M) + two virtual keys"
GATEWAY=http://127.0.0.1:4300 \
PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \
LOGIN_TOKEN_URL=http://microsoft-login-stand-in:8080/token \
SOFTERIA_INTERNAL=http://softeria-m365-mcp:3000 \
python seed.py

KEY_A=$(tr -d '\n' < .agent-a-virtual-key)
KEY_B=$(tr -d '\n' < .agent-b-virtual-key)

cat <<EOF

========================================================================
App-per-agent Hi Agent demo is up

LiteLLM UI (local):     http://127.0.0.1:4300/ui
  Master key (UI only): sk-local-master
  Confirm MCP servers:  m365_a, m365_b
  Each server: OAuth → Machine-to-Machine (client_credentials)

Public base (tunnel):   $PUBLIC_BASE_URL

Hi Agent MCP plugin URLs (Streamable HTTP) + Bearer virtual key:
  Agent A (read):  $PUBLIC_BASE_URL/m365_a/mcp
    Bearer: $KEY_A
  Agent B (send):  $PUBLIC_BASE_URL/m365_b/mcp
    Bearer: $KEY_B

LiteLLM holds Entra app secrets and refreshes Graph tokens.
Hi Agent only sees the virtual key — it does not hold app tokens.

Keys also written to:
  .agent-a-virtual-key
  .agent-b-virtual-key

Graph log:  $(pwd)/logs/graph.jsonl
Audit log:  $(pwd)/logs/audit.log

Next: open README.md → Integrate with Hi Agent.
Stop with: ./down.sh
========================================================================
EOF
