#!/usr/bin/env bash
# Calibrate sim params against the practice results in logs/practice_scores.json (local, untracked). Usage: scripts/calib.sh SEEDS "k=v k=v" ["k=v ..."]
cd "$(dirname "$0")/.."
V=/tmp/claude-1000/-home-gb-mech-game/d62d4b17-f398-4693-ad15-3381ad4e0730/scratchpad/versions
SEEDS="$1"; shift
t() { python3 -c "import json;print(json.load(open('logs/practice_scores.json')).get('$1'))"; }
for cfg in "$@"; do
  P=""; for kv in $cfg; do P="$P --param $kv"; done
  line="[$cfg]"; err=0
  for spec in "mb|mb|bot/main.py|$(t mb)" "mbe|mbe|bot/main.py|$(t mbe)" "v0|x|$V/v0/bot/main.py|$(t v0)" "v1.2|v1|bot/main.py|$(t v1.2)" "v1.1|x|$V/6fd8444/bot/main.py|$(t v1.1)"; do
    IFS='|' read n pol path prac <<< "$spec"
    s=$(MECH_POLICY=$pol python3 sim/run.py --seeds "$SEEDS" --jobs 22 $P -- python3 -u $path 2>&1 | grep -m1 "^score" | awk '{print $2}')
    line="$line $n $s/$prac"
    err=$(python3 -c "print(round($err + abs($s - $prac), 2))")
  done
  echo "$line  | sum|err| = $err"
done
