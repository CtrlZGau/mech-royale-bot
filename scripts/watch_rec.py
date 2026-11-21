"""Record the public watch feeds: every broadcast match (god view, mechreplay/1) to logs/watch/<match>.jsonl and
showdown standings changes to logs/watch/state.jsonl. One read-only connection per feed; reconnects with backoff.
    .venv/bin/python scripts/watch_rec.py
"""
import asyncio
import json
import os
import time

import websockets

BASE = "wss://watch.joinbuilders.org"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "watch")
HDR = {"Origin": "https://watch.joinbuilders.org", "User-Agent": "Mozilla/5.0"}


async def live():
    f = None
    while True:
        try:
            async with websockets.connect(BASE + "/live", max_size=2 ** 26, open_timeout=20, additional_headers=HDR) as ws:
                chan = {}
                async for msg in ws:
                    text = msg if isinstance(msg, str) else msg.decode()
                    for line in text.splitlines():
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get("type") == "channel":
                            chan = d
                            continue
                        if d.get("type") == "header":
                            if f:
                                f.close()
                            name = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-seed{d.get('seed')}.jsonl"
                            f = open(os.path.join(OUT, name), "a", buffering=1)
                            f.write(json.dumps({"type": "channel", **chan}) + "\n")
                        if f:
                            f.write(line + "\n")
        except Exception as e:  # network hiccup: wait and reconnect
            print("live:", type(e).__name__, e, flush=True)
            await asyncio.sleep(15)


async def state():
    last = None
    while True:
        try:
            async with websockets.connect(BASE + "/state", max_size=2 ** 26, open_timeout=20, additional_headers=HDR) as ws:
                d = json.loads(await asyncio.wait_for(ws.recv(), 30))
            snap = {"t": time.time(), "airing": (d.get("stage") or {}).get("airing"), "showdown": d.get("showdown")}
            key = json.dumps([snap["airing"], snap["showdown"]], sort_keys=True)
            if key != last:
                last = key
                with open(os.path.join(OUT, "state.jsonl"), "a") as g:
                    g.write(json.dumps(snap) + "\n")
        except Exception as e:
            print("state:", type(e).__name__, e, flush=True)
        await asyncio.sleep(60)


async def main():
    os.makedirs(OUT, exist_ok=True)
    await asyncio.gather(live(), state())


if __name__ == "__main__":
    asyncio.run(main())
