# Mech Royale bot. Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# Runs bot/main.py: POLICY selects the policy (wd = Warden).
# Plain mechbot (practice 4.13): set ENTRYPOINT to /bot/mb_bot.py.
FROM python:3.13.1-slim
WORKDIR /bot
# Practice test: Warden with the Guard switch on (cautious once a death we did not cause is seen).
ENV WD_P="g.guard=1"
COPY bot/ /bot/
ENTRYPOINT ["python3", "-u", "/bot/main.py"]
