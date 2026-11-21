"""Robustness check: replay recorded observations through a policy with optional fields removed
or perturbed, and count exceptions. A crash in act() makes the wrapper coast WITHOUT firing.

    python3 scripts/fuzz.py policy_v0:V0Policy policy:BasePolicy
"""
import copy, glob, importlib, json, os, random, sys, traceback, collections
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
import mechfight as mf

OPTIONAL_ACTOR = ["hp", "max_hp", "cubes", "weapons", "charging"]


def mutate(o, rng, mode):
    o = copy.deepcopy(o)
    if mode == "strip":
        for m in o["mechs"]:
            for k in OPTIONAL_ACTOR:
                m.pop(k, None)
    elif mode == "random":
        for m in o["mechs"]:
            for k in OPTIONAL_ACTOR:
                if rng.random() < 0.3:
                    m.pop(k, None)
        if rng.random() < 0.05:
            o["drops"] = []
        if rng.random() < 0.05:
            o["self"]["weapons"] = [o["self"]["weapons"][0], None]
    return o


def main():
    specs = sys.argv[1:] or ["policy_v0:V0Policy", "policy:BasePolicy"]
    recs = sorted(glob.glob(os.path.join(ROOT, "logs", "live-*.jsonl")))
    streams = []
    for p in recs:
        msgs = []
        for l in open(p):
            if '"dir": "in"' in l:
                try:
                    msgs.append(json.loads(l)["msg"])
                except ValueError:
                    pass  # recording still being written
        hello = next((m for m in msgs if m.get("type") == "hello"), None)
        obs = [m for m in msgs if m.get("type") == "obs"]
        if hello and obs:
            hello["map"]["dir"] = os.path.join(ROOT, "kit", "arena")
            streams.append((hello, obs))
    print(f"{len(streams)} recordings, {sum(len(o) for _, o in streams)} observations")
    for spec in specs:
        mod, cls = spec.split(":")
        P = getattr(importlib.import_module(mod), cls)
        for mode in ("asis", "strip", "random"):
            errs = collections.Counter(); first = {}
            n = 0
            for hello, obs in streams:
                rng = random.Random(1)
                bot = P(copy.deepcopy(hello))
                for o in obs:
                    mf.draw.clear(); n += 1
                    try:
                        bot.act(mutate(o, rng, mode))
                    except Exception as e:
                        key = f"{type(e).__name__}: {e}"[:120]
                        errs[key] += 1
                        first.setdefault(key, traceback.format_exc().strip().splitlines()[-3])
            print(f"{spec:24s} {mode:6s} exceptions {sum(errs.values())}/{n}")
            for k, v in errs.most_common(5):
                print(f"    {v:6d}  {k}\n            at {first[k].strip()}")


if __name__ == "__main__":
    main()
