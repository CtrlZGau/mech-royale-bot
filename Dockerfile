# Mech Royale bot. Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# Runs bot/main.py: POLICY selects the policy (wd = Warden).
# Plain mechbot (practice 4.13): set ENTRYPOINT to /bot/mb_bot.py.
FROM python:3.13.1-slim
WORKDIR /bot
# Evolved Warden settings (sim evolution #1, training 6.29 on seeds 1-48): shorter back-off bursts,
# count as losing below 39% HP, wounded bonus 95, Scout loot radius 172 m.
ENV WD_P="g.back_s=0.702,g.lose_low_hp=0.388,hunt.wounded_bonus=94.676,scout.loot_r=171.852"
COPY bot/ /bot/
ENTRYPOINT ["python3", "-u", "/bot/main.py"]
