# mech-royale-bot

A bot for the Mech Royale `mechfight/1` battle royale (16 mechs, OSU campus map).

- `Dockerfile`: submission image (top level, as the scorer expects); copies `bot/` only.
- `bot/`: the bot (`main.py` entry, `policy.py` base policy, `util.py` math, `world.py` map).
- `kit/`: the official bot kit (protocol, schema, conformance check, map). Unmodified.

## Test

```sh
scripts/check.sh local    # conformance check with local python
scripts/check.sh docker   # build + check in the match sandbox (needs Docker)
python3 scripts/smoke.py  # run the policy over the recorded stream, print what it did
```
