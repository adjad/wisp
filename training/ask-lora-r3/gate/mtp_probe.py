#!/usr/bin/env python3
"""MTP probe: the same 12 prompts per model, production sampling (no temperature sent, so oMLX's per-model profile
applies), thinking off, max_tokens 256, streamed to time decode. Acceptance comes from oMLX's own per-request
"MTP[n] ... accept=a/b ... tok/cycle=x" log lines, read from ~/.omlx/logs/server.log for this model's time window
(one model resident at a time).

    mtp_probe.py --models A,B --out FILE.json
"""
import argparse
import os
import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = os.environ.get("OMLX_BASE", "http://127.0.0.1:8000")
LOG = Path.home() / ".omlx/logs/server.log"
PROMPTS = [
    "Write a 250-word story about a lighthouse keeper who finds a message in a bottle.",
    "Explain how a refrigerator works to a curious twelve-year-old, in about 250 words.",
    "Write a friendly 200-word email inviting neighbours to a weekend street cleanup.",
    "Describe a perfect autumn Saturday in a small town, in about 250 words.",
    "Give step-by-step instructions for making a simple tomato pasta sauce, in prose, about 250 words.",
    "Summarise the pros and cons of working from home in about 250 words.",
    "Write a 250-word diary entry from someone on their first day at a new job.",
    "Explain why the sky is blue and why sunsets are red, in about 250 words.",
    "Write a 200-word product description for a handmade ceramic mug.",
    "Describe how to plan a weekly grocery budget, in about 250 words.",
    "Write a short motivational talk for a team that just missed a deadline, about 250 words.",
    "Explain what compound interest is with a simple example, in about 250 words.",
]


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read())


def post(path, body=b""):
    req = urllib.request.Request(BASE + path, body, method="POST", headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=300)


def stream(model, prompt):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 256, "stream": True,
            "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.monotonic(); first = None; usage = None; n = 0
    with post("/v1/chat/completions", json.dumps(body).encode()) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            ev = json.loads(line[5:])
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                if (ch.get("delta") or {}).get("content"):
                    n += 1
                    if first is None:
                        first = time.monotonic()
    end = time.monotonic()
    toks = (usage or {}).get("completion_tokens") or n
    return {"ttft": round((first or end) - t0, 3), "decode_s": round(end - (first or end), 3), "tokens": toks,
            "decode_tok_s": round((toks - 1) / max(1e-6, end - (first or end)), 1) if first else None}


def log_mtp(since, until):
    pat = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*MTP\[\d+\].*?tokens=(\d+) cycles=(\d+) tok/cycle=([\d.]+) accept=(\d+)/(\d+)")
    out = []
    for line in LOG.read_text(errors="replace").splitlines():
        m = pat.search(line)
        if m:
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            if since <= t <= until:
                out.append({"tokens": int(m.group(2)), "cycles": int(m.group(3)), "tok_per_cycle": float(m.group(4)),
                            "accepted": int(m.group(5)), "proposed": int(m.group(6))})
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--models", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    res = {}
    for model in a.models.split(","):
        for other, m in {m["id"]: m for m in get("/v1/models/status").get("models", [])}.items():
            if other != model and m.get("loaded"):
                post(f"/admin/api/models/{other}/unload").read(); print("unloaded", other)
        stream(model, "Say hello.")  # load + warm
        since = time.time()
        runs = [stream(model, p) for p in PROMPTS]
        time.sleep(2)
        mtp = log_mtp(since - 1, time.time() + 1)
        dec = sorted(r["decode_tok_s"] for r in runs if r["decode_tok_s"])
        acc = sum(x["accepted"] for x in mtp); prop = sum(x["proposed"] for x in mtp)
        res[model] = {"runs": runs, "decode_tok_s_p50": dec[len(dec) // 2], "decode_tok_s_p10": dec[len(dec) // 10],
                      "mean_tokens": sum(r["tokens"] for r in runs) / len(runs),
                      "mtp_log_lines": len(mtp), "mtp_accept_rate": round(acc / prop, 3) if prop else None,
                      "mtp_tok_per_cycle": round(sum(x["tokens"] for x in mtp) / max(1, sum(x["cycles"] for x in mtp)), 2) if mtp else None}
        print(model, {k: v for k, v in res[model].items() if k != "runs"})
    Path(a.out).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
