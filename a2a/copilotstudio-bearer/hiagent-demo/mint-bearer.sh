#!/bin/sh
# Hi Agent Create A2A "bearer" max 128 chars — print the short lab key.
set -eu
CLIENT="${1:-agent-a}"
case "$CLIENT" in
  agent-a|lab-agent-a) echo "lab-agent-a" ;;
  agent-b|lab-agent-b) echo "lab-agent-b" ;;
  *)
    echo "usage: $0 [agent-a|agent-b]" >&2
    exit 1
    ;;
esac
