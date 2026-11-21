# Mech Royale bot. Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# Runs V3 = mechbot @120f1ff (bot/mb_bot.py, unchanged) + DEFAULT_FLAGS in bot/mb_v3.py.
# Plain mechbot (practice 4.13): set ENTRYPOINT to /bot/mb_bot.py.
FROM python:3.13.1-slim
WORKDIR /bot
COPY bot/ /bot/
ENTRYPOINT ["python3", "-u", "/bot/main.py"]
