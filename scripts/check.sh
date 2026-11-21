#!/usr/bin/env bash
# Conformance check. Usage: scripts/check.sh [local|docker|all]  (default: local)
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:-local}"
if [[ "$mode" == local || "$mode" == all ]]; then
  echo "== local python check =="
  python3 kit/check/check.py -- python3 -u bot/main.py
fi
if [[ "$mode" == docker || "$mode" == all ]]; then
  echo "== docker sandbox check (what the scorer runs) =="
  docker build -t ctrlz-bot .
  python3 kit/check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 \
    --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
    -v "$PWD/kit/arena:/arena:ro" ctrlz-bot
fi
