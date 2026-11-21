# Mech Royale bot. Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# Runs V3 = mechbot @120f1ff (bot/mb_bot.py, unchanged) + flags in bot/mb_v3.py DEFAULT_FLAGS.
FROM python:3.13.1-slim
WORKDIR /bot
COPY bot/ /bot/
ENTRYPOINT ["python3", "-u", "/bot/main.py"]
