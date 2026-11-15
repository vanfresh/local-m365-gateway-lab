#!/bin/sh
set -eu
cd "$(dirname "$0")"
docker compose --env-file .env down --remove-orphans 2>/dev/null || docker compose down --remove-orphans
rm -f .tunnel-url
echo "rest/nango-oauth-proxy hiagent-demo stopped"
