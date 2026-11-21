"""Entry point. Swap the policy class here to try another policy.

  V0Policy (policy_v0.py): v0 always-fight logic + laser-release and sticky-zone fixes  <- active
  BasePolicy (policy.py):  v1.2 "hunter" (retreat/heal/cover/dodge), practice 2.98
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mechfight as mf  # noqa: E402
from policy_v0 import V0Policy  # noqa: E402

if __name__ == "__main__":
    mf.run(V0Policy)
