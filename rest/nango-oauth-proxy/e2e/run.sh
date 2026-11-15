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

echo "==> Starting stack"
docker compose --env-file .env up -d --remove-orphans

echo "==> Waiting for Keycloak"
i=0
while [ "$i" -lt 90 ]; do
  if curl -sf http://127.0.0.1:8085/realms/master >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if ! curl -sf http://127.0.0.1:8085/realms/master >/dev/null 2>&1; then
  echo "Keycloak not ready" >&2
  docker compose logs keycloak --tail 80 >&2
  exit 1
fi

echo "==> Waiting for Nango"
i=0
while [ "$i" -lt 90 ]; do
  if curl -sf http://127.0.0.1:3003/ >/dev/null 2>&1 || curl -sf http://127.0.0.1:3003/health >/dev/null 2>&1; then
    break
  fi
  # Nango may 404 on / — any HTTP response means up
  if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3003/ | grep -qE '^[0-9]+$'; then
    code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3003/)
    if [ "$code" != "000" ]; then
      break
    fi
  fi
  i=$((i + 1))
  sleep 2
done

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Seeding Keycloak + Nango"
LOGIN=http://microsoft-login-stand-in:8080 \
LOGIN_PUBLIC=http://127.0.0.1:8481 \
KEYCLOAK=http://127.0.0.1:8085 \
NANGO=http://127.0.0.1:3003 \
python seed.py

echo "==> Recreating policy-proxy with NANGO_SECRET_KEY"
docker compose --env-file .env up -d --force-recreate policy-proxy

i=0
while [ "$i" -lt 60 ]; do
  if curl -sf http://127.0.0.1:4400/health >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done

echo "==> Pytest"
pytest tests
