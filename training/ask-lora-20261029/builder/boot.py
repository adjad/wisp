"""Import FIRST: fresh WISP_HOME before any `service` import, repo on sys.path."""
import os
import sys
import tempfile

# builder/ -> ask-lora-20261029/ -> training/ -> repo root
REPO = os.environ.get("WISP_REPO") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_home = tempfile.mkdtemp(prefix="wisp-home-")
os.environ["WISP_HOME"] = _home
os.environ["MOE_DIR"] = _home
assert os.environ["WISP_HOME"] and os.path.isdir(_home)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
