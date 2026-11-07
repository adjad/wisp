#!/usr/bin/env python3
"""Reuse base answers r4 already sampled from the served model, so the GPU doesn't write them again.

    python reuse_r4.py --zip r4/ask-lora-data.zip --partial partial.jsonl --out OUT/finals.r4reuse.jsonl

A row is reused only when its id exists in r4's data, every user, tool and assistant text in its history appears in r4's
prompt, and r4's completion isn't the scripted target (so base wrote it). session_chat_r4 is left out: r4's targets there
are part of what lost base's voice. Lines use gen_finals.py's format with sample k=0, so `run_finals.sh` skips these ids
and choose_r5.py still applies every r5 check. Tools and the date aren't compared: they don't change what the answer says.
"""
from __future__ import annotations

import _paths  # noqa: F401

import argparse
import collections
import json
import zipfile

from gen import request_hash
from gen_finals import is_final, parse_output, read_jsonl

SKIP_FAMILIES = {"session_chat_r4"}
FORCED = "<|channel>thought\n"


def same_history(messages: list[dict], prompt: str) -> bool:
    for m in messages:
        c = m.get("content")
        if m.get("role") != "system" and isinstance(c, str) and c.strip() and c.strip() not in prompt:
            return False
    return True


def reused(row: dict, old: dict) -> dict | None:
    m = row["meta"]
    if m["family"] in SKIP_FAMILIES or not is_final(row):
        return None
    o = old.get(m["id"])
    if not o or not same_history(row["messages"][:-1], o["prompt"]):
        return None
    scripted = (row["messages"][-1].get("content") or "").strip()
    if scripted and scripted[:60] in o["completion"]:
        return None
    p = parse_output(o["completion"], o["prompt"].endswith(FORCED))
    if not (p.get("content") or "").strip() or p.get("tool_calls"):
        return None
    return {"id": m["id"], "family": m["family"], "k": 0, "model": "r4-served-base",
            "request_hash": request_hash(row["messages"][:-1], row.get("tools")), "finish_reason": "stop", **p}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--partial", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    z = zipfile.ZipFile(a.zip)
    old = {}
    for name in ("train.jsonl", "dev.jsonl"):
        for line in z.read(name).decode().splitlines():
            r = json.loads(line)
            old[r["id"]] = r
    n, fam = 0, collections.Counter()
    with open(a.out, "w") as f:
        for row in read_jsonl(a.partial):
            rec = reused(row, old)
            if rec:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
                fam[rec["family"]] += 1
    print(f"reused {n} answers: {dict(fam.most_common())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
