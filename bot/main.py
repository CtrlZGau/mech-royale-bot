"""Entry point. POLICY picks what runs in the submitted image; MECH_POLICY env overrides it locally.

  mb   mechbot TimedBot (mb_bot.py, vendored Specz04/mechbot@120f1ff, practice 4.10)  <- default
  v2   V2Policy  (policy_v2.py): evade - stay in zone, max distance from everyone, fight only if engaged
  v0   V0Policy  (policy_v0.py): v0 always-fight logic + fixes + experiment flags (v1.3)
  v1   BasePolicy (policy.py):   v1.2 "hunter" (practice 2.98)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mechfight as mf  # noqa: E402

POLICY = "mb"


def policy_class(name=None):
    name = (name or os.environ.get("MECH_POLICY") or POLICY).lower()
    if name == "mb":
        from mb_bot import TimedBot as P
    elif name == "v0":
        from policy_v0 import V0Policy as P
    elif name == "v1":
        from policy import BasePolicy as P
    else:
        from policy_v2 import V2Policy as P
    return P


if __name__ == "__main__":
    mf.run(policy_class())
