#!/usr/bin/env bash
# Run mechbot variants in the sim with traces into logs/simcombat/<name>/, then compare combat efficiency.
# Usage: scripts/combat_ab.sh SEEDS "name|flags|MB_P" ...   (first spec = baseline)
set -u
cd "$(dirname "$0")/.."
SEEDS="$1"; shift
P="--param omniscient=0 --param search_s=15 --param h_per_min=0.5"
dirs=()
for SPEC in "$@"; do
  IFS='|' read -r NAME F MP <<< "$SPEC"
  out="logs/simcombat/$NAME"; rm -rf "$out"; mkdir -p "$out"
  rm -f logs/sim/trace-*.jsonl
  env MECH_POLICY=v3 MB_FLAGS="$F" MB_P="$MP" python3 sim/run.py --seeds "$SEEDS" --jobs 22 $P --trace \
      -- python3 -u bot/main.py 2>&1 | grep -m1 "^score" | sed "s/^/[$NAME] /"
  mv logs/sim/trace-*.jsonl "$out"/
  dirs+=("$out")
done
python3 scripts/dps.py "${dirs[@]}"
