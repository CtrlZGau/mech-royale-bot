# Mech Royale bot. Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# Pinned base image: finalists are rebuilt, so the build must be reproducible.
FROM python:3.13.18-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /bot
COPY bot/ /bot/
ENTRYPOINT ["python3", "-u", "/bot/main.py"]
