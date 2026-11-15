#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p logs
docker compose up -d --remove-orphans
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt
pytest tests
