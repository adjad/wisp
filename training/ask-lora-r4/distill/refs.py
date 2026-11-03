#!/usr/bin/env python3
"""The recall facts of session_chat_r4, for choose.py's session checks.

The builder keeps each scenario's recall detail (``ref``), the never-mentioned topic (``absent``) and the
whitelisted general-knowledge words (``ok``) in ``scen_r4_session_chat.QS``, not in meta. Scenario k of the family
is ``QS[k]`` (the family builds every question in order and fails the build on any dry-run failure), so id
``session_chat_r4-{k:03d}-s0`` maps to ``QS[k]``. Each mapping is checked: the partial row's scripted final must be
``QS[k]["final"]``, and its notes must name the same kind.

    refs.py --partial _build/sc/partial.jsonl --out refs.json
    refs.py --registry-out tools.json     # every live tool name (validate.py's final:leak list), for choose.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BUILDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ask-lora-r2", "builder")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--partial")
    ap.add_argument("--out")
    ap.add_argument("--registry-out")
    a = ap.parse_args(argv)
    sys.path.insert(0, os.path.abspath(BUILDER))
    os.environ.setdefault("ASK_LORA_ROUND", "4")
    if a.registry_out:
        import boot  # noqa: F401,E402  (fresh WISP_HOME, repo on sys.path)
        import service.tools  # noqa: F401,E402
        from service import skills  # noqa: E402
        skills.load()
        from service.tools.registry import REGISTRY  # noqa: E402
        with open(a.registry_out, "w") as f:
            json.dump(sorted(REGISTRY), f, indent=0)
            f.write("\n")
        print(f"{len(REGISTRY)} tool names -> {a.registry_out}")
    if not a.partial:
        return
    import scen  # noqa: E402,F401  (first, as build.py does: scen's family imports reach scen_r4_session_chat)
    import scen_r4_session_chat as S  # noqa: E402  (loads the service through harness; no model, no network)
    rows = [json.loads(l) for l in open(a.partial) if l.strip()]
    rows = [r for r in rows if r["meta"]["family"] == S.FAMILY]
    if len(rows) != len(S.QS):
        sys.exit(f"{len(rows)} {S.FAMILY} rows but {len(S.QS)} questions: the k -> QS[k] mapping does not hold")
    out, bad = {}, []
    for r in rows:
        mid = r["meta"]["id"]
        k = int(mid.split("-")[1])
        q = S.QS[k]
        if r["messages"][-1]["content"] != q["final"] or f"({q['kind']})" not in r["meta"]["notes"]:
            bad.append(mid)
            continue
        out[mid] = {"kind": q["kind"], "ref": list(q["ref"]), "absent": q["absent"], "ok": list(q["ok"])}
    if bad:
        sys.exit(f"scripted final or kind differs from QS[k] for {len(bad)} ids: {bad[:5]}")
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"{len(out)} refs -> {a.out}")


if __name__ == "__main__":
    main()
