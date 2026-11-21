# Mech Royale bot (Go). Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# The Python version of the same policy is in bot/ (python3 -u /bot/main.py).
FROM golang:1.23.4-bookworm AS build
WORKDIR /src
COPY gobot/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/warden .

FROM scratch
COPY --from=build /out/warden /bot/warden
# Hunt tuning: early laser release at 70 % of a lethal partial charge, target HP weight 0.18.
# Fights: boost while fighting (energy > 40, not while charging a laser) and while sidestepping (energy > 25).
# Free-for-all: look for lasers from 240 m in the first 20 s, fight laser duels from 120 m in Guard, and in the
# first 60 s back off from a laser carrier while we have no laser.
ENV WD_P="g.lethal_k=0.7,hunt.hp_weight=0.18,g.fight_boost=40,g.dodge_boost=25,scout.laser_r=240,guard.pref_laser=120,g.early_gunned_s=60"
ENTRYPOINT ["/bot/warden"]
