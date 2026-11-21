# Mech Royale bot. Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# Runs mechbot @120f1ff exactly as its own Dockerfile does (same base image, bot file as entrypoint).
# Practice 4.13 in this repo. bot/main.py selects other policies for local tests only (MECH_POLICY env).
FROM python:3.13.1-slim
WORKDIR /bot
COPY bot/ /bot/
ENTRYPOINT ["python3", "-u", "/bot/mb_bot.py"]
