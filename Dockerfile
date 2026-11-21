# Mech Royale bot. Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# Runs bot/main.py: POLICY selects the policy (wd = Warden).
# Plain mechbot (practice 4.13): set ENTRYPOINT to /bot/mb_bot.py.
FROM python:3.13.1-slim
WORKDIR /bot
# Practice test: Warden with a more aggressive hunt profile: finish wounded targets from 20 m,
# stickier targeting, stronger wounded-target focus, laser at 120 m, speed-first upgrades,
# no long-range Scout looting. Remove this ENV line for plain Warden (5.31).
ENV WD_P="hunt.finish_r=20,hunt.keep_bonus=200,hunt.hp_weight=0.1,hunt.wounded_bonus=200,hunt.pref_laser=120,hunt.speed_ups=1,scout.loot_r=90,scout.laser_r=90"
COPY bot/ /bot/
ENTRYPOINT ["python3", "-u", "/bot/main.py"]
