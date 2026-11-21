# Mech Royale bot. Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# Runs bot/main.py: POLICY selects the policy (wd = Warden).
# Plain mechbot: set ENTRYPOINT to /bot/mb_bot.py.
FROM python:3.13.1-slim
WORKDIR /bot
# Practice test: Warden configured to mirror the strongest reference configuration we know of
# (parity engine rules, Guard on, no supply drops, aggressive hunt numbers, cautious first 20 s).
# Remove this ENV line for plain Warden.
ENV WD_P="g.parity=1,g.guard=1,g.drops=0,hunt.finish_r=20,hunt.keep_bonus=200,hunt.hp_weight=0.1,hunt.wounded_bonus=200,hunt.pref_laser=120,scout.loot_r=90,scout.laser_r=90,scout.lethal=0,guard.laser_first=1"
COPY bot/ /bot/
ENTRYPOINT ["python3", "-u", "/bot/main.py"]
