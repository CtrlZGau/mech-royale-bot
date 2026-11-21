# Mech Royale bot (Go). Build: docker build -t ctrlz-bot .
# Match sandbox: no network, read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
# The Python version of the same policy is in bot/ (python3 -u /bot/main.py).
FROM golang:1.23.4-bookworm AS build
WORKDIR /src
COPY gobot/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/warden .

FROM scratch
COPY --from=build /out/warden /bot/warden
# Practice test: give up a pocket escape whose walk to the takeoff stops making progress (remove for defaults).
ENV WD_P="g.esc_guard=1"
ENTRYPOINT ["/bot/warden"]
