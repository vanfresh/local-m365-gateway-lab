#!/bin/sh
set -eu
cd "$(dirname "$0")"
docker compose down --remove-orphans
rm -f .tunnel-url
echo "hiagent-demo stopped"
