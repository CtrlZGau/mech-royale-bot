"""Run the policy directly over the recorded check stream and summarise what it decided.
Not a simulation (observations don't react), but exercises every branch without stdin plumbing."""
import collections, gzip, json, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
import mechfight as mf
from policy_v0 import V0Policy as BasePolicy

msgs = [json.loads(l) for l in gzip.open(os.path.join(ROOT, "kit/check/stream.jsonl.gz"), "rt")]
hello = msgs[0]; hello["map"]["dir"] = os.path.join(ROOT, "kit/arena")
bot = BasePolicy(hello)
labels, fires, takes, worst = collections.Counter(), collections.Counter(), 0, 0.0
for m in msgs[1:]:
    if m.get("type") != "obs":
        continue
    mf.draw.clear(); t0 = time.perf_counter()
    a = bot.act(m); worst = max(worst, (time.perf_counter() - t0) * 1000)
    labels[(mf.draw.take() or {}).get("label", "?").split(" hp")[0].split(" ")[0]] += 1
    for i, f in enumerate(a.get("fire", [])): fires[i] += bool(f)
    takes += "take" in a
print("labels:", dict(labels)); print("fire per slot:", dict(fires), "takes:", takes, f"worst act {worst:.2f} ms")
