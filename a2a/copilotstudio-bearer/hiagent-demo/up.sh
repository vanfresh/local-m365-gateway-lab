#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p logs

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
  else
    printf 'NANGO_ENCRYPTION_KEY=%s\nNANGO_SECRET_KEY=\n' "$(openssl rand -base64 32)" > .env
  fi
fi

echo "==> Starting stack"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://127.0.0.1:4900}"
docker compose --env-file .env up -d --remove-orphans \
  nango-db nango-server \
  microsoft-login-stand-in copilot-a2a-stand-in \
  policy-proxy caddy

echo "==> Waiting for Nango"
i=0
while [ "$i" -lt 90 ]; do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3503/ || true)
  if [ "$code" != "000" ] && [ -n "$code" ]; then
    break
  fi
  i=$((i + 1))
  sleep 2
done

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

tmp=$(mktemp)
grep -vE '^(PUBLIC_BASE_URL)=' .env > "$tmp" || true
{
  cat "$tmp"
  echo "PUBLIC_BASE_URL=$PUBLIC_BASE_URL"
} > .env
rm -f "$tmp"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Seeding Nango"
NANGO=http://127.0.0.1:3503 \
SEED_WORK_ROOT="$(pwd)" \
COMPOSE_PROJECT=a2a-bearer-hiagent-demo \
COMPOSE_FILE="$(pwd)/compose.yaml" \
python ../e2e/seed.py

echo "==> Recreating policy-proxy with NANGO_SECRET_KEY + public base"
docker compose --env-file .env up -d --force-recreate policy-proxy caddy

i=0
while [ "$i" -lt 60 ]; do
  if curl -sf http://127.0.0.1:4900/health >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if ! curl -sf http://127.0.0.1:4900/health >/dev/null 2>&1; then
  echo "policy-proxy not healthy" >&2
  docker compose logs policy-proxy --tail 80 >&2
  exit 1
fi

SSE_URL="$PUBLIC_BASE_URL/a2a/v1/message:stream"

cat <<EOF | tee .hiagent-banner

========================================================================
A2A Copilot Bearer lab — Hi Agent demo is up

PUBLIC (tunnel):   $PUBLIC_BASE_URL
Local gateway:     http://127.0.0.1:4900
Local Nango:       http://127.0.0.1:3503
Caddy (local):     http://127.0.0.1:8490
Re-print later:    cat $(pwd)/.hiagent-banner

------------------------------------------------------------------------
Hi Agent: Create A2A service
------------------------------------------------------------------------
  SSE URL:   $SSE_URL
  bearer:    lab-agent-a
             (agent-b → lab-agent-b; max 128 chars — NOT a JWT)
  ./mint-bearer.sh

  No Keycloak / no OAuth in Hi Agent. Gateway mints Copilot tokens into Nango.

  Agent card: $PUBLIC_BASE_URL/.well-known/agent-card.json

------------------------------------------------------------------------
Secrets (gitignored; examples committed)
------------------------------------------------------------------------
  .env                          ← from .env.example (seed fills NANGO_SECRET_KEY)
  .nango-secret                 ← from Nango DB (see .nango-secret.example)
  .nango-encryption-key         ← optional mirror of NANGO_ENCRYPTION_KEY

========================================================================

EOF
