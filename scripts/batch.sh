#!/usr/bin/env bash
# Play N live matches in private rooms, J at a time. Usage: scripts/batch.sh [N=10] [J=3] [tag=batch]
set -u
cd "$(dirname "$0")/.."
N="${1:-10}"; J="${2:-3}"; TAG="${3:-batch}"
mkdir -p logs/runs
run() { timeout 900 .venv/bin/python scripts/live.py --name ctrlz-test > "logs/runs/$TAG-$1.txt" 2>&1; echo "match $1 done: $(grep -m1 -o 'END place=[0-9]* kills=[0-9]*' "logs/runs/$TAG-$1.txt" || echo 'NO RESULT')"; }
export -f run; export TAG
seq 1 "$N" | xargs -P "$J" -I{} bash -c 'sleep $(( ({} - 1) % '"$J"' * 3 )); run {}'
