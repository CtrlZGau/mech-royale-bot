#!/usr/bin/env bash
# Paired A/B of mechbot variants vs plain mechbot 4.10 in the local sim.
# Usage: scripts/ab.sh SEEDS SPEC [SPEC ...]   SPEC = "flag,flag" or "flag,flag|name=val,name=val"
#   e.g. scripts/ab.sh 1-144 focus "focus,npcsafe|hp_w=0.12,stick_s=8"
set -u
cd "$(dirname "$0")/.."
SEEDS="$1"; shift
P="--param omniscient=0 --param search_s=15 --param h_per_min=0.5"
for SPEC in "$@"; do
  F="${SPEC%%|*}"; MP=""; [[ "$SPEC" == *"|"* ]] && MP="${SPEC#*|}"
  out=$(python3 sim/run.py --seeds "$SEEDS" --jobs 22 $P \
        --ab "env MECH_POLICY=mb python3 -u bot/main.py" \
        --ab "env MECH_POLICY=v3 MB_FLAGS=$F MB_P=$MP python3 -u bot/main.py" 2>&1)
  echo "== [$SPEC]"
  echo "$out" | grep -E "score|A - B" | head -4
done
