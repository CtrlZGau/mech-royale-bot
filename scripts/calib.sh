#!/usr/bin/env bash
# Calibrate sim params against known practice scores. Usage: scripts/calib.sh SEEDS "k=v k=v" ["k=v ..."]
cd "$(dirname "$0")/.."
V=/tmp/claude-1000/-home-gb-mech-game/d62d4b17-f398-4693-ad15-3381ad4e0730/scratchpad/versions
SEEDS="$1"; shift
for cfg in "$@"; do
  P=""; for kv in $cfg; do P="$P --param $kv"; done
  line="[$cfg]"; err=0
  for spec in "mb|mb|bot/main.py|4.12" "mbe|mbe|bot/main.py|3.15" "v0|x|$V/v0-3.25/bot/main.py|3.25" "v1.2|v1|bot/main.py|2.98" "v1.1|x|$V/6fd8444/bot/main.py|1.77"; do
    IFS='|' read n pol path prac <<< "$spec"
    s=$(MECH_POLICY=$pol python3 sim/run.py --seeds "$SEEDS" --jobs 22 $P -- python3 -u $path 2>&1 | grep -m1 "^score" | awk '{print $2}')
    line="$line $n $s/$prac"
    err=$(python3 -c "print(round($err + abs($s - $prac), 2))")
  done
  echo "$line  | sum|err| = $err"
done
