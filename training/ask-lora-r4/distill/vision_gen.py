#!/usr/bin/env python3
"""Self-distillation for the vision rows: the BASE model's own answer to each row of ../vision/vision_rows.jsonl, sent
the way production sends it.

- Picture and scanned-document answers (kinds ``image``, ``document``) are ``service/vision.py`` ``answer``: max_tokens
  400.
- Text documents (``text_document``) are ``service/documents.py``: the row's ``meta.max_tokens`` (600).
- Picture readings (``picture_read``) are ``vision.read_picture``: max_tokens 700.
- Every call has thinking OFF and carries ``X-oMLX-Cache-Store: off``.
- The row's ``{"type": "image", "path": ...}`` parts become ``image_url`` data URLs, in the row's order (images first,
  then the text, as ``vision._messages`` builds them).
- Sample 0 is production exactly: temperature 0. A greedy rerun would only repeat it, so top-up samples (k >= 1) use
  the chat profile (temperature 1.0, top_p 0.95, top_k 64) with thinking still off. Each record says which it used.

There is no thought to keep: production never thinks on these calls.

Output is one JSON line per (id, k), like gen.py: ``content`` is the raw reply, and ``request_hash`` covers the
messages and the image bytes. The run resumes by skipping pairs that are already done. Run it through ``run.sh``
(``GEN=vision_gen.py``).

    vision_gen.py --rows ../vision/vision_rows.jsonl --k 1 --out raw/x/vision.jsonl [--ids FILE] [--kinds image,...]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen import MODEL, NEVER, http, one_resident  # noqa: E402

GREEDY = {"temperature": 0}
TOPUP = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}
MAX_TOKENS = {"image": 400, "document": 400, "picture_read": 700}      # vision._MAX_TOKENS, READ_MAX_TOKENS
TEXT_DOC_MAX = 600                                                      # documents.py, when the row does not say
NO_CACHE_STORE = {"X-oMLX-Cache-Store": "off"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
KINDS = ("image", "document", "text_document", "picture_read")


def max_tokens(row: dict) -> int:
    if row["kind"] == "text_document":
        return int((row.get("meta") or {}).get("max_tokens") or TEXT_DOC_MAX)
    return MAX_TOKENS[row["kind"]]


def image_bytes(base: str, row: dict) -> list[bytes]:
    return [open(os.path.join(base, p), "rb").read() for p in row.get("images") or []]


def vision_hash(row: dict, images: list[bytes]) -> str:
    blob = json.dumps({"kind": row["kind"], "messages": row["messages"], "max_tokens": max_tokens(row)},
                      sort_keys=True, ensure_ascii=False).encode()
    h = hashlib.sha256(blob)
    for b in images:
        h.update(hashlib.sha256(b).digest())
    return h.hexdigest()[:16]


def wire(row: dict, images: list[bytes]) -> list[dict]:
    """The row's messages with each image placeholder as a data URL (the images list is in placeholder order)."""
    paths, out, n = list(row.get("images") or []), [], 0
    for m in row["messages"]:
        c = m.get("content")
        if isinstance(c, list):
            parts = []
            for p in c:
                if p.get("type") != "image":
                    parts.append(p)
                    continue
                if n >= len(paths) or p.get("path") not in (None, paths[n]):
                    raise ValueError(f"{row['id']}: image part {p.get('path')!r} does not match images[{n}]")
                mime = MIME.get(os.path.splitext(paths[n])[1].lower(), "image/jpeg")
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{mime};base64," + base64.b64encode(images[n]).decode()}})
                n += 1
            m = {**m, "content": parts}
        out.append(m)
    if n != len(paths):
        raise ValueError(f"{row['id']}: {n} image parts for {len(paths)} images")
    return out


def sample(row: dict, images: list[bytes], k: int) -> dict:
    prof = GREEDY if k == 0 else TOPUP
    body = {"model": MODEL, "messages": wire(row, images), "stream": False, "max_tokens": max_tokens(row), **prof,
            "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.monotonic()
    resp = http("/v1/chat/completions", body, headers=NO_CACHE_STORE)
    dt = time.monotonic() - t0
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    return {"content": msg.get("content") or "", "reasoning": msg.get("reasoning_content", msg.get("reasoning")),
            "tool_calls": msg.get("tool_calls") or [], "finish_reason": ch.get("finish_reason"),
            "usage": resp.get("usage"), "seconds": round(dt, 2), "no_choices": not resp.get("choices"),
            "sampling": {**prof, "max_tokens": max_tokens(row), "enable_thinking": False}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True, help="vision_rows.jsonl (image paths are relative to its folder)")
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--kinds", default=",".join(KINDS))
    ap.add_argument("--ids", help="file of row ids to sample (top-up rounds)")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    if any(x in MODEL.lower() for x in NEVER):
        sys.exit(f"refusing to load {MODEL}")
    base = os.path.dirname(os.path.abspath(a.rows))
    kinds = set(a.kinds.split(","))
    rows = [r for r in map(json.loads, open(a.rows)) if r["kind"] in kinds]
    if a.ids:
        want = {x.strip() for x in open(a.ids) if x.strip()}
        rows = [r for r in rows if r["id"] in want]
    if a.limit:
        rows = rows[:a.limit]
    done = set()
    if os.path.exists(a.out):
        for line in open(a.out):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not d.get("error"):
                done.add((d["id"], d["k"]))
    todo = [(k, r) for k in range(a.k) for r in rows if (r["id"], k) not in done]
    print(f"{len(rows)} rows x {a.k} = {len(rows) * a.k}; {len(done)} done, {len(todo)} to go", flush=True)
    one_resident(MODEL)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    t_start, errors = time.monotonic(), 0
    with open(a.out, "a") as f:
        for n, (k, r) in enumerate(todo, 1):
            imgs = image_bytes(base, r)
            rec = {"id": r["id"], "family": "vision_" + r["kind"], "k": k, "model": MODEL,
                   "request_hash": vision_hash(r, imgs)}
            try:
                rec |= sample(r, imgs, k)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                rec["error"] = repr(exc)[:300]
                errors += 1
                if errors >= 5:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    sys.exit(f"stopping after {errors} errors: {rec['error']}")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if n % 10 == 0 or n == len(todo):
                el = time.monotonic() - t_start
                print(f"{n}/{len(todo)}  {el / 60:.1f} min  eta {el / n * (len(todo) - n) / 60:.0f} min  "
                      f"last {rec.get('seconds')}s finish={rec.get('finish_reason')}", flush=True)


if __name__ == "__main__":
    main()
