"""Protocol server: runs one match with external bot subprocess(es) + in-process house squad.

    python3 -m sim.server --seed 3 -- python3 -u bot/main.py        # one match, prints result
    python3 -m sim.server --seed 3 --replay --validate -- python3 -u kit/python/example_bot.py

Lockstep (protocol §2): send obs, wait up to tick_budget_ms for the answer echoing the tick,
apply, simulate decide_every ticks. No answer: coast (keep move/boost/aim, drop one-shots), idle
after coast_s.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import select
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sim import house  # noqa: E402
from sim.engine import Match  # noqa: E402
from sim.geom import Map  # noqa: E402

KIT = os.path.join(ROOT, "kit")
ARENA = os.path.join(KIT, "arena")
_CFG = None
_MAP = None
_SCHEMA = None


def load_config():
    global _CFG
    if _CFG is None:
        with gzip.open(os.path.join(KIT, "check", "stream.jsonl.gz"), "rt") as f:
            _CFG = json.loads(f.readline())["config"]
    return _CFG


def load_map():
    global _MAP
    if _MAP is None:
        _MAP = Map(ARENA)
    return _MAP


def schema():
    global _SCHEMA
    if _SCHEMA is None:
        sys.path.insert(0, os.path.join(KIT, "check"))
        import check  # noqa: E402  (kit/check/check.py, read-only reuse)
        _SCHEMA = check.Schema(os.path.join(KIT, "mechfight.schema.json"))
    return _SCHEMA


class ExtBot:
    def __init__(self, cmd, cwd=None, stderr_path=None):
        self.err = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, cwd=cwd, bufsize=0)
        self.fd = self.p.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.buf = b""
        self.lines = []
        self.dead = False
        self.bad = 0
        self.late = 0
        self.answered = 0
        self.ms = []

    def send(self, obj):
        if self.dead:
            return
        try:
            self.p.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            self.dead = True

    def _pump(self, timeout):
        if self.dead and not self.buf:
            return
        try:
            r, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            self.dead = True
            return
        if not r:
            return
        try:
            chunk = os.read(self.fd, 1 << 20)
        except BlockingIOError:
            return
        except OSError:
            self.dead = True
            return
        if not chunk:
            self.dead = True
            return
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self.lines.append(line)

    def read_msg(self, deadline, want=None):
        """Next JSON message (dict) before deadline (time.monotonic), or None.
        want(msg) -> bool filters (non-matching messages are dropped)."""
        while True:
            while self.lines:
                raw = self.lines.pop(0)
                if len(raw) > 65536:
                    self.bad += 1
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    self.bad += 1
                    continue
                if not isinstance(msg, dict):
                    self.bad += 1
                    continue
                if want is None or want(msg):
                    return msg
            if self.dead:
                return None
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return None
            self._pump(1.0 if deadline is None else deadline - now)

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=2)
        except Exception:
            self.p.kill()
            try:
                self.p.wait(timeout=1)
            except Exception:
                pass
        if self.err not in (None, subprocess.DEVNULL):
            self.err.close()


CONT = ("move", "boost", "aim")


def run_match(seed, cmds, params=None, record=False, replay_dir=None, no_timeout=False, budget_ms=None,
              validate=False, full=False, stderr_dir=None, names=None, cwd=None, verbose=False, trace_dir=None):
    """Run one match. cmds: list of commands (one external bot each). Returns dict."""
    cfg = load_config()
    gmap = load_map()
    srng = random.Random(seed * 31 + 7)
    slots = srng.sample(range(cfg["match"]["mechs"]), len(cmds))
    M = Match(cfg, gmap, seed, ext_ids=slots, house_factory=lambda MM: house.setup(MM, params), record=record)
    if record:
        M.snapshot_initial()
    bud = (budget_ms if budget_ms is not None else cfg["bot"]["tick_budget_ms"]) / 1000.0
    hello_bud = cfg["bot"]["hello_budget_ms"] / 1000.0
    bots = {}
    probs = []
    for i, (slot, cmd) in enumerate(zip(slots, cmds)):
        errp = os.path.join(stderr_dir, f"bot-{seed}-{slot}.log") if stderr_dir else None
        b = ExtBot(cmd, cwd=cwd, stderr_path=errp)
        bots[slot] = b
        hello = {"type": "hello", "protocol": "mechfight/1", "slot": slot, "seed": srng.randrange(1 << 31),
                 "map": {"dir": ARENA, "files": ["v2/world.json", "v2/terrain.i16"]}, "config": cfg}
        b.send(hello)
    for slot, b in bots.items():
        r = b.read_msg(None if no_timeout else time.monotonic() + hello_bud, lambda m: m.get("type") == "ready")
        nm = (r or {}).get("name") or f"bot{slot}"
        M.mechs[slot].name = str(nm)[:32]
        if r is None:
            probs.append(f"slot {slot}: no ready")
    last_act = {s: {} for s in bots}
    last_ok_t = {s: 0.0 for s in bots}
    sch = schema() if validate else None
    trace = None
    if trace_dir:
        os.makedirs(trace_dir, exist_ok=True)
        trace = open(os.path.join(trace_dir, f"trace-{seed}.jsonl"), "w")
    t_wall = time.monotonic()
    while not M.over:
        live_ext = [s for s in bots if M.mechs[s].alive]
        if not live_ext and not full:
            break
        acts = {}
        # send all observations first (lockstep), then collect
        sent = {}
        for s in live_ext:
            o = M.obs_for(M.mechs[s])
            if sch is not None:
                errs = sch.errors(o, "Observation")
                if errs:
                    probs.append(f"obs tick {o['tick']}: {errs[:3]}")
            bots[s].send(o)
            sent[s] = (o["tick"], time.monotonic())
            if trace is not None:
                trace.write(json.dumps({"dir": "in", "msg": o}, separators=(",", ":")) + "\n")
        for s in live_ext:
            b = bots[s]
            tick, t0 = sent[s]
            dl = None if no_timeout else t0 + bud
            a = b.read_msg(dl, lambda m, tk=tick: m.get("tick") == tick)
            if a is not None:
                b.ms.append((time.monotonic() - t0) * 1000)
                b.answered += 1
                if sch is not None:
                    errs = sch.errors(a, "Action")
                    if errs:
                        probs.append(f"action tick {tick}: {errs[:3]}")
                last_act[s] = a
                last_ok_t[s] = M.t
                acts[s] = a
                if trace is not None:
                    trace.write(json.dumps({"dir": "out", "msg": a}, separators=(",", ":")) + "\n")
            else:
                b.late += 1
                if M.t - last_ok_t[s] <= cfg["bot"]["coast_s"]:
                    acts[s] = {k: last_act[s][k] for k in CONT if k in last_act[s]}
                else:
                    acts[s] = {"aim": last_act[s].get("aim")} if last_act[s].get("aim") else {}
        acts.update(M.squad.decide())
        M.decide(acts)
    if not M.over:
        M._finish()  # stopped early (our mech died): rank survivors by HP, as at the time limit
    # end messages
    for s, b in bots.items():
        m = M.mechs[s]
        if m.place is None:
            # still alive when we stopped (shouldn't happen) -> current rank
            m.place = M.n_alive()
        b.send({"type": "end", "place": m.place, "kills": m.kills})
        b.close()
        if trace is not None:
            trace.write(json.dumps({"dir": "in", "msg": {"type": "end", "place": m.place, "kills": m.kills}}) + "\n")
            trace.close()
    wall = time.monotonic() - t_wall
    res = M.result()
    out = {"seed": seed, "slots": slots, "wall_s": round(wall, 2), "t_end": round(M.t, 2), "problems": probs[:20],
           "result": res, "bots": {s: {"answered": b.answered, "late": b.late, "bad": b.bad,
                                       "p50_ms": round(sorted(b.ms)[len(b.ms) // 2], 2) if b.ms else None,
                                       "max_ms": round(max(b.ms), 2) if b.ms else None} for s, b in bots.items()}}
    if record and replay_dir:
        os.makedirs(replay_dir, exist_ok=True)
        path = os.path.join(replay_dir, f"replay-{seed}.jsonl")
        bots_hdr = ["human" if mm.team == "ext" else f"house:{mm.diff}" for mm in M.mechs]
        with open(path, "w") as f:
            f.write(json.dumps(M.replay_header(bots_hdr, [mm.name for mm in M.mechs]), separators=(",", ":")) + "\n")
            for fr in M.frames:
                f.write(json.dumps(fr, separators=(",", ":")) + "\n")
            if M.rev:
                f.write(json.dumps({"t": M.tick, "m": [], "n": [], "p": [], "z": [0, 0, 0], "ev": M.rev}, separators=(",", ":")) + "\n")
            f.write(json.dumps(res, separators=(",", ":")) + "\n")
        out["replay"] = path
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--no-timeout", action="store_true")
    ap.add_argument("--full", action="store_true", help="keep simulating after our mech dies")
    ap.add_argument("--trace", action="store_true", help="write logs/sim/trace-<seed>.jsonl (obs + actions)")
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    params = {}
    for kv in a.param:
        k, v = kv.split("=", 1)
        params[k] = float(v)
    r = run_match(a.seed, [cmd], params, record=a.replay, replay_dir=os.path.join(ROOT, "logs", "sim"),
                  no_timeout=a.no_timeout, validate=a.validate, full=a.full, stderr_dir=os.path.join(ROOT, "logs", "sim"),
                  trace_dir=os.path.join(ROOT, "logs", "sim") if a.trace else None)
    s = r["slots"][0]
    me = r["result"]["mechs"][s]
    print(json.dumps({k: v for k, v in r.items() if k != "result"}))
    print(json.dumps(me))


if __name__ == "__main__":
    main()
