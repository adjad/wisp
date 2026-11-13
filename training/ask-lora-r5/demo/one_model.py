"""One model resident at a time (user, 2026-11-12: "dont keep both models loaded at once").

    one_model.py KEEP [--base-url URL]     unload every loaded model except KEEP ("none" keeps nothing)

Only for the engine a check window started; never run against the user's oMLX outside one.
Prints what was loaded before and after, with the engine's model memory."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

OMLX = "http://127.0.0.1:8000"


def status(base_url: str = OMLX) -> dict:
    with urllib.request.urlopen(base_url + "/v1/models/status", timeout=30) as r:
        return json.loads(r.read())


def loaded(st: dict) -> list[str]:
    return [m["id"] for m in st.get("models", []) if m.get("loaded") or m.get("is_loading")]


def only(keep: str | None, base_url: str = OMLX, log=print) -> list[str]:
    """Unload every loaded model but `keep`; returns what is loaded afterwards."""
    before = status(base_url)
    for mid in loaded(before):
        if mid != keep:
            req = urllib.request.Request(f"{base_url}/admin/api/models/{mid}/unload", b"", method="POST")
            urllib.request.urlopen(req, timeout=120).read()
            log(f"one_model: unloaded {mid}")
    after = status(base_url)
    log(f"one_model: loaded before={loaded(before)} after={loaded(after)} "
        f"model_memory={after.get('current_model_memory', 0) / 1e9:.2f} GB")
    return loaded(after)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("keep")
    ap.add_argument("--base-url", default=OMLX)
    a = ap.parse_args(argv)
    keep = None if a.keep == "none" else a.keep
    left = only(keep, a.base_url)
    return 0 if all(m == keep for m in left) else 3


if __name__ == "__main__":
    sys.exit(main())
