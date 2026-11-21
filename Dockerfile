# Mech Royale bot (Go). Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# The Python version of the same policy is in bot/ (python3 -u /bot/main.py).
FROM golang:1.23.4-bookworm AS build
WORKDIR /src
COPY gobot/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/warden .

FROM scratch
COPY --from=build /out/warden /bot/warden
# Hunt tuning: early laser release at 70 % of a lethal partial charge, target HP weight 0.18; give up a pocket
# escape whose walk to the takeoff stops making progress (remove for defaults).
# Cubes valued at 100 (default 75) when choosing loot.
ENV WD_P="g.lethal_k=0.7,hunt.hp_weight=0.18,g.esc_guard=1,g.cube_v=100"
ENTRYPOINT ["/bot/warden"]
