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
docker compose --env-file .env up -d --remove-orphans

echo "==> Waiting for Nango"
i=0
while [ "$i" -lt 90 ]; do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3403/ || true)
  if [ "$code" != "000" ] && [ -n "$code" ]; then
    break
  fi
  i=$((i + 1))
  sleep 2
done

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Seeding Nango"
NANGO=http://127.0.0.1:3403 python seed.py

echo "==> Recreating policy-proxy with NANGO_SECRET_KEY"
docker compose --env-file .env up -d --force-recreate policy-proxy

i=0
while [ "$i" -lt 60 ]; do
  if curl -sf http://127.0.0.1:4800/health >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done

echo "==> Pytest"
pytest tests
