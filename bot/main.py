"""Entry point. Swap the policy class here to try another policy."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mechfight as mf  # noqa: E402
from policy import BasePolicy  # noqa: E402

if __name__ == "__main__":
    mf.run(BasePolicy)
