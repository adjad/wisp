#!/usr/bin/env python3
"""Self-distillation for the r4 chat families: sample the BASE model's own replies to the exact production requests.

Each row of a builder ``partial.jsonl`` ({meta, messages, tools}) is replayed as Wisp sends it: ``messages[:-1]``,
``tools``, ``meta.tool_choice`` and ``meta.chat_template_kwargs`` (enable_thinking true), with tool-call arguments as
JSON strings on the wire. The scripted target (``messages[-1]``) is never sent. Sampling is the production profile:
temperature 1.0, top_p 0.95, top_k 64, max_tokens 3000.

Output is one JSON line per (id, sample): content, reasoning, tool calls, finish reason, usage and a hash of the
request, so ``choose.py``/``splice.py`` can check a build row still asks the same thing. Order is sample-major (sample 0
for every prompt, then sample 1, ...), so a stopped run already covers every prompt once. A rerun skips the
(id, sample) pairs already in ``--out``. ``--ids`` with a larger ``--k`` tops up only the ids that have no passing
sample yet (``topup.sh``).

Run it through ``run.sh``: that takes the heavy lock and restores oMLX's settings on exit. This script only talks to
the one model it is given and unloads any other loaded model first (one model resident).

    gen.py --partial $TMPDIR/r4/partial.jsonl --families chat_voice_r4,chat_direct_r4 --k 2 --out raw/chat.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("OMLX_BASE", "http://127.0.0.1:8000")
MODEL = "gemma-4-E4B-it-oQ4e-mtp"
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "max_tokens": 3000}
# Models that must never be loaded for this (feedback: no Ling, no embedder).
NEVER = ("ling", "embed", "rerank")
BASE_ORG = "Jundot"          # the untuned base's folder in oMLX's model dir; tuned models (r3, ...) carry other ids
OMLX_SETTINGS = os.path.expanduser("~/.omlx/settings.json")


def check_base(model: str = MODEL) -> str:
    """The folder `model` loads from, or exit: it must be the one folder of that name under oMLX's model dirs, and it
    must sit in Jundot/. Only the model-dir keys of oMLX's settings are read."""
    try:
        cfg = json.load(open(OMLX_SETTINGS)).get("model") or {}
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read oMLX's model dirs: {exc!r}")
    roots = cfg.get("model_dirs") or [cfg.get("model_dir")]
    hits = []
    for root in filter(None, roots):
        for sub in [root] + [os.path.join(root, d) for d in sorted(os.listdir(root))
                             if os.path.isdir(os.path.join(root, d))]:
            cand = os.path.join(sub, model)
            if os.path.isdir(cand):
                hits.append(os.path.realpath(cand))
    hits = sorted(set(hits))
    if len(hits) != 1 or os.path.basename(os.path.dirname(hits[0])) != BASE_ORG:
        sys.exit(f"{model} must be exactly one folder under {BASE_ORG}/ in oMLX's model dirs; found {hits}")
    return hits[0]


def wire_messages(messages: list[dict]) -> list[dict]:
    """The request as Wisp's loop sends it: tool-call arguments as JSON strings."""
    out = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):
            m["tool_calls"] = [{**tc, "function": {**tc["function"], "arguments": (
                tc["function"]["arguments"] if isinstance(tc["function"]["arguments"], str)
                else json.dumps(tc["function"]["arguments"], ensure_ascii=False))}} for tc in m["tool_calls"]]
        out.append(m)
    return out


def request_hash(messages: list[dict], tools: list | None) -> str:
    """A hash of the prompt side that ignores the arguments' wire form (string vs object), so a row of the written
    all.jsonl (objects) and of partial.jsonl (strings) hash the same."""
    norm = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):
            m["tool_calls"] = [{**tc, "function": {**tc["function"], "arguments": (
                json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str)
                else tc["function"]["arguments"])}} for tc in m["tool_calls"]]
        norm.append(m)
    blob = json.dumps({"messages": norm, "tools": tools or []}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def http(path: str, body: dict | None = None, timeout: float = 600, headers: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def one_resident(model: str) -> None:
    """Unload every other model; exit unless the server's own path for `model` is in Jundot/ (the untuned base)."""
    models = (http("/v1/models/status") or {}).get("models", [])
    path = next((m.get("model_path") for m in models if m.get("id") == model), None)
    if not path or f"/{BASE_ORG}/" not in path:
        sys.exit(f"oMLX serves {model} from {path!r}, not from {BASE_ORG}/")
    for m in models:
        if m.get("loaded") and m["id"] != model:
            http(f"/admin/api/models/{m['id']}/unload", {})
            print("unloaded", m["id"], flush=True)


def split_thought(content: str | None, reasoning: str | None) -> tuple[str, str | None]:
    """oMLX normally returns the thought as reasoning_content; fall back to Gemma's raw channel markers."""
    content = content or ""
    if reasoning is None and "<channel|>" in content:
        head, _, tail = content.partition("<channel|>")
        reasoning = head.replace("<|channel>thought", "", 1).strip("\n")
        content = tail
    return content.strip(), (reasoning.strip() if isinstance(reasoning, str) else None)


def sample(row: dict) -> dict:
    meta = row["meta"]
    body = {"model": MODEL, "messages": wire_messages(row["messages"][:-1]), "stream": False, **SAMPLING,
            "chat_template_kwargs": meta.get("chat_template_kwargs") or {"enable_thinking": True}}
    if row.get("tools"):
        body["tools"] = row["tools"]
        if meta.get("tool_choice") is not None:
            body["tool_choice"] = meta["tool_choice"]
    t0 = time.monotonic()
    resp = http("/v1/chat/completions", body)
    dt = time.monotonic() - t0
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content, reasoning = split_thought(msg.get("content"), msg.get("reasoning_content", msg.get("reasoning")))
    return {"content": content, "reasoning": reasoning, "tool_calls": msg.get("tool_calls") or [],
            "finish_reason": ch.get("finish_reason"), "usage": resp.get("usage"), "seconds": round(dt, 2),
            "no_choices": not resp.get("choices")}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--partial", required=True)
    ap.add_argument("--families", required=True, help="comma-separated meta.family values")
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="first N prompts only (smoke test)")
    ap.add_argument("--ids", help="file of meta ids (one per line) to sample; others are skipped (top-up rounds)")
    a = ap.parse_args(argv)
    if any(x in MODEL.lower() for x in NEVER):
        sys.exit(f"refusing to load {MODEL}")
    fams = set(a.families.split(","))
    rows = [r for r in map(json.loads, open(a.partial)) if r["meta"]["family"] in fams]
    if a.ids:
        want = {x.strip() for x in open(a.ids) if x.strip()}
        rows = [r for r in rows if r["meta"]["id"] in want]
    if a.limit:
        rows = rows[:a.limit]
    done = set()
    if os.path.exists(a.out):
        for line in open(a.out):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue          # a line cut off by a kill
            if not d.get("error"):
                done.add((d["id"], d["k"]))
    todo = [(k, r) for k in range(a.k) for r in rows if (r["meta"]["id"], k) not in done]
    print(f"{len(rows)} prompts x {a.k} = {len(rows) * a.k}; {len(done)} done, {len(todo)} to go", flush=True)
    print(f"model {MODEL} -> {check_base()}", flush=True)
    one_resident(MODEL)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    t_start, errors = time.monotonic(), 0
    with open(a.out, "a") as f:
        for n, (k, r) in enumerate(todo, 1):
            m = r["meta"]
            rec = {"id": m["id"], "family": m["family"], "k": k, "model": MODEL,
                   "request_hash": request_hash(r["messages"][:-1], r.get("tools"))}
            try:
                rec |= sample(r)
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
                      f"last {rec.get('seconds')}s {(rec.get('usage') or {}).get('completion_tokens')} tok "
                      f"finish={rec.get('finish_reason')}", flush=True)


if __name__ == "__main__":
    main()
