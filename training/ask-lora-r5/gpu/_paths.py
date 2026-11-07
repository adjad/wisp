"""Put the directories holding gen.py / choose.py (r4 distill) and render.py (r3 brev) on sys.path.

In the repo they sit at ../../ask-lora-r4/distill and ../../ask-lora-r3/brev. On the GPU box upload copies of
gen.py, choose.py and render.py next to these scripts (or point WISP_DISTILL_DIR / WISP_RENDER_DIR at them).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.environ.get("WISP_DISTILL_DIR"), os.environ.get("WISP_RENDER_DIR"),
           os.path.join(HERE, "..", "..", "ask-lora-r4", "distill"),
           os.path.join(HERE, "..", "..", "ask-lora-r3", "brev")):
    if _d and os.path.isdir(_d) and os.path.abspath(_d) not in sys.path:
        sys.path.append(os.path.abspath(_d))
