"""Play our bot in a real match on the public play server (play.joinbuilders.org).

The browser client speaks mechfight/1 over a WebSocket (same hello/obs/end and action JSON as
scored matches), plus a small lobby layer: `lobby` messages, and `ready`/`loaded`/`start` from us.
This bridge creates a private room (so we don't drop a bot on human players), joins it, starts the
match (bots fill the empty seats), pipes hello/obs to the bot subprocess exactly as the scorer does,
and records every message to logs/live-<time>.jsonl for failure analysis.

    .venv/bin/python scripts/live.py                 # private room, our bot
    .venv/bin/python scripts/live.py --name test1 -- python3 -u kit/python/example_bot.py

Notes: the play server runs in real time (not lockstep like scored matches), and its fill bots
may not behave like the practice house-bot squad. Physics, map, weapons and vision are the real ones.
"""
import argparse
import asyncio
import json
import os
import secrets
import sys
import time
import urllib.request

import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = "play.joinbuilders.org"


def log(*a):
    print(f"[live {time.strftime('%H:%M:%S')}]", *a, file=sys.stderr, flush=True)


def new_private_room(sid):
    url = f"https://{HOST}/mm/code/new?sid={sid}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "mech-bot-bridge"}), timeout=8) as r:
        d = json.load(r)
    if "code" not in d:
        raise SystemExit(f"could not create private room: {d}")
    return d["code"], d["room"]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="ctrlz-bot")
    ap.add_argument("--code", help="join an existing private room code instead of making one")
    ap.add_argument("--room", type=int)
    ap.add_argument("--map-dir", default=os.path.join(ROOT, "kit", "arena"))
    ap.add_argument("--matches", type=int, default=1, help="matches to play before exiting")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    cmd = cmd or [sys.executable, "-u", os.path.join(ROOT, "bot", "main.py")]

    sid = secrets.token_hex(16)
    code, room = (a.code, a.room) if a.code else new_private_room(sid)
    if a.code and room is None:
        raise SystemExit("--code needs --room")
    log(f"private room {room} code {code}")
    log(f"watch in a browser: https://{HOST}/#code-{code}   (or https://{HOST}/?watch&code={code})")

    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    rec_path = os.path.join(ROOT, "logs", f"live-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    rec = open(rec_path, "w", buffering=1)
    log(f"recording to {rec_path}")

    url = f"wss://{HOST}/r/{room}/play?token=&sid={sid}&name={a.name}&code={code}"
    live_url = f"wss://{HOST}/r/{room}/live?token=&code={code}&sid={sid}"
    replay_path = rec_path.replace("live-", "replay-")
    replay_task = None

    async def record_replay():
        """Spectator stream = full god-view replay (mechreplay/1). Saved for the 3D viewer."""
        try:
            async with websockets.connect(live_url, max_size=None, user_agent_header="mech-bot-bridge") as lv:
                with open(replay_path, "w", buffering=1) as f:
                    async for raw in lv:
                        f.write(str(raw).rstrip("\n") + "\n")
        except Exception as e:
            log(f"replay stream ended: {e!r}")
    played = 0
    bot = None
    stats = {"obs": 0, "acts": 0, "lat": []}
    sent_at = {}

    async with websockets.connect(url, max_size=None, ping_interval=20,
                                  user_agent_header="mech-bot-bridge") as ws:
        async def pump_bot(proc):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    log("bot sent a non-JSON line:", line[:200])
                    continue
                rec.write(json.dumps({"dir": "out", "at": time.time(), "msg": msg}) + "\n")
                if msg.get("type") == "ready":
                    continue  # the lobby already knows our name
                t0 = sent_at.pop(msg.get("tick"), None)
                if t0 is not None:
                    stats["lat"].append((time.perf_counter() - t0) * 1000)
                stats["acts"] += 1
                await ws.send(json.dumps(msg, separators=(",", ":")))

        async def pipe_stderr(proc):
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                print("[bot]", line.decode(errors="replace").rstrip(), file=sys.stderr, flush=True)

        ready_sent = False
        start_sent_for = None
        async for raw in ws:
            for part in str(raw).split("\n"):
                if not part.strip():
                    continue
                msg = json.loads(part)
                rec.write(json.dumps({"dir": "in", "at": time.time(), "msg": msg}) + "\n")
                kind = msg.get("type")
                if kind == "lobby":
                    you = msg.get("you", {})
                    key = (msg.get("phase"), msg.get("match"), you.get("ready"), you.get("seated"))
                    if key != getattr(main, "_last_lobby", None):
                        main._last_lobby = key
                        log("lobby:", json.dumps({k: v for k, v in msg.items() if k != "standings"})[:700])
                    if not you.get("ready") and not ready_sent:
                        await ws.send(json.dumps({"type": "ready", "name": a.name}))
                        ready_sent = True
                        log("sent ready")
                    if msg.get("phase") != "running" and you.get("ready") and start_sent_for != msg.get("match"):
                        await ws.send(json.dumps({"type": "start"}))
                        start_sent_for = msg.get("match")
                        log(f"sent start (match {msg.get('match')})")
                elif kind == "hello":
                    msg["map"]["dir"] = a.map_dir
                    bot = await asyncio.create_subprocess_exec(
                        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, cwd=ROOT)
                    asyncio.ensure_future(pump_bot(bot))
                    asyncio.ensure_future(pipe_stderr(bot))
                    bot.stdin.write((json.dumps(msg) + "\n").encode())
                    await bot.stdin.drain()
                    await ws.send(json.dumps({"type": "loaded"}))
                    log(f"hello: slot {msg['slot']}; bot started; sent loaded")
                    if replay_task is None:
                        replay_task = asyncio.ensure_future(record_replay())
                        log(f"recording replay to {replay_path}")
                elif kind == "obs":
                    stats["obs"] += 1
                    if bot and bot.returncode is None:
                        sent_at[msg["tick"]] = time.perf_counter()
                        bot.stdin.write((part + "\n").encode())
                        await bot.stdin.drain()
                    if stats["obs"] % 100 == 0:
                        s = msg["self"]
                        log(f"t={msg['t']:.0f}s alive={msg['alive']} hp={s['hp']:.0f} pos={[round(v) for v in s['pos']]}")
                elif kind == "end":
                    lat = sorted(stats["lat"]) or [0]
                    log(f"END place={msg.get('place')} kills={msg.get('kills')}  obs={stats['obs']} "
                        f"acts={stats['acts']} bot ms p50={lat[len(lat)//2]:.1f} max={lat[-1]:.1f}")
                    if bot and bot.returncode is None:
                        bot.stdin.write((part + "\n").encode())
                        await bot.stdin.drain()
                        try:
                            await asyncio.wait_for(bot.wait(), 3)
                        except asyncio.TimeoutError:
                            bot.kill()
                    bot = None
                    played += 1
                elif kind == "results":
                    rows = msg.get("mechs", [])
                    log("results:", json.dumps(rows)[:1500])
                    if played >= a.matches:
                        rec.close()
                        await asyncio.sleep(6)  # let the spectator stream (delayed) catch up
                        if replay_task:
                            replay_task.cancel()
                        log(f"view it: python3 scripts/view.py {os.path.relpath(replay_path, ROOT)}")
                        return


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
