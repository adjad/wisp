#!/usr/bin/env python3
"""Keep test questions out of an r5 build, and say exactly which rows that removes. Run on the Mac, after build.py and before
base writes anything, so no node time goes to a row that can't stay.

    seal_r5.py --build BUILD [--private-dir ~/.moe/evals/private-real-use] [--audit-dir AUDIT_OUTPUTS] [--keep-own-wording]

The user's rule of 2026-11-07: "Never turn sealed evaluation prompts, answers, or failure transcripts into training examples.
Report known overlaps separately from unseen evaluation." A turn is removed when its question is the same as, or close to
(leak.py's 0.7 trigram bar), a prompt of:

- ``public``    the eval questions, the chat eval and this checkout's toolbench, every split, including ``heldout_sources``.
                build.py checks only the pinned toolbench and exempts leak_ok rows; this has no exemption.
- ``private``   the real-use test sets, read in memory only. Their prompts stay out of every output: the manifest holds ids.
- ``audit``     the prompts of the audit's diagnostic transcripts.

Earlier user turns count too when they have 4+ words, because a seeded history is part of what a row trains on. ("thanks"
is not a test question.) The whole turn goes, every step, so no row keeps a sibling. ``--keep-own-wording`` is the one
exception, and only the user can ask for it: debug_log_r5 rows whose only overlap is with ``private`` stay and are listed apart.

Writes ``overlap_manifest.json`` and refreshes ``stats.json`` beside the build.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINING = HERE.parent.parent
sys.path.insert(0, str(TRAINING / "ask-lora-r2" / "builder"))
sys.path.insert(0, str(HERE))
os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import r5_stats  # noqa: E402

HISTORY_MIN_WORDS = 4
PAIRS = (("all.jsonl", "meta.jsonl"), ("train.jsonl", "train.meta.jsonl"), ("valid.jsonl", "valid.meta.jsonl"))


def private_prompts(root: str) -> list[dict]:
    import leak
    import yaml
    out = []
    for path in sorted([os.path.join(root, "cases.yaml"), *glob.glob(os.path.join(root, "*", "cases.yaml"))]):
        if not os.path.isfile(path):
            continue
        tag = "real-use" if os.path.dirname(path) == root.rstrip("/") else os.path.basename(os.path.dirname(path))
        for c in yaml.safe_load(open(path)) or []:
            if not isinstance(c, dict):
                continue
            texts = [c.get("prompt")] + [t.get("user") for t in c.get("history") or [] if isinstance(t, dict)]
            out += [leak._entry(f"private:{tag}:{c.get('id')}", str(t)) for t in texts if t]
    return out


def audit_prompts(root: str) -> list[dict]:
    import leak
    out = []
    for name in ("paired_behavior_review.json", "multi_source_20_trajectories.json"):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        data = json.load(open(path))
        rows = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
        for i, r in enumerate(rows):
            u = r.get("user") or r.get("prompt") or r.get("question")
            if isinstance(u, str) and u.strip():
                out.append(leak._entry(f"audit:{name[:6]}:{i}", u))
    return out


def extra_public() -> list[dict]:
    """The r4 chat eval: leak.load_chat reads the older copy."""
    import leak
    import yaml
    path = TRAINING / "ask-lora-r4" / "chateval" / "prompts_r4.yaml"
    if not path.is_file():
        return []
    out = []
    for u in yaml.safe_load(open(path)).get("units") or []:
        texts = [u["prompt"]] if "prompt" in u else list(u.get("prompts") or u.get("turns") or [])
        out += [leak._entry(f"chat4:{u['id']}#{i}", str(t)) for i, t in enumerate(texts)]
    return out


class Index:
    def __init__(self, private_dir=None, audit_dir=None):
        import leak
        self.leak = leak
        self.sets = {"public": leak.LeakIndex(), "public_chat4": leak.LeakIndex(extra_public())}
        self.sizes = {"public": len(self.sets["public"].questions), "public_chat4": len(self.sets["public_chat4"].questions)}
        priv = private_prompts(private_dir) if private_dir else []
        if priv:
            self.sets["private"] = leak.LeakIndex(priv)
        aud = audit_prompts(audit_dir) if audit_dir else []
        if aud:
            self.sets["audit"] = leak.LeakIndex(aud)
        self.sizes |= {"private": len(priv), "audit": len(aud)}
        self.memo = {}

    def hits(self, text: str) -> dict:
        """{set name: (reference id, similarity)} for a user turn."""
        if text in self.memo:
            return self.memo[text]
        out = {}
        for name, idx in self.sets.items():
            hit, score, q = idx.leaks(text)
            if hit:
                out[name.replace("_chat4", "")] = (q["id"], round(score, 3))
        self.memo[text] = out
        return out


def user_turns(ex: dict) -> list[str]:
    return [m["content"] for m in ex["messages"] if m.get("role") == "user" and isinstance(m.get("content"), str)]


def classify(rows, index: Index) -> dict:
    """turn id -> {family, rows, current: {set: ref}, history: {set: ref}}. The current question is the last user turn."""
    turns = {}
    for ex, meta in rows:
        tid = meta["id"].rsplit("-s", 1)[0]
        t = turns.setdefault(tid, {"family": meta["family"], "rows": 0, "ids": [], "current": {}, "history": {}, "qkind": meta.get("qkind")})
        t["rows"] += 1
        t["ids"].append(meta["id"])
        users = user_turns(ex)
        if not users:
            continue
        t["current"] |= index.hits(users[-1])
        for u in users[:-1]:
            if len(u.split()) >= HISTORY_MIN_WORDS:
                t["history"] |= index.hits(u)
    return turns


def removal(turn: dict, keep_own_wording: bool) -> bool:
    if not turn["current"] and not turn["history"]:
        return False
    if keep_own_wording and turn["family"] == "debug_log_r5":
        return bool(set(turn["current"]) - {"private"} or set(turn["history"]) - {"private"})
    return True


def remove_ids(build: Path, ids: set) -> None:
    for ex_name, meta_name in PAIRS:
        kept_ex, kept_meta = [], []
        with open(build / ex_name) as fe, open(build / meta_name) as fm:
            for a, b in zip(fe, fm, strict=True):
                if json.loads(b)["id"] not in ids:
                    kept_ex.append(a)
                    kept_meta.append(b)
        for name, lines in ((ex_name, kept_ex), (meta_name, kept_meta)):
            tmp = build / (name + ".tmp")
            tmp.write_text("".join(lines))
            os.replace(tmp, build / name)
    part = build / "partial.jsonl"
    if part.is_file():
        tmp = build / "partial.jsonl.tmp"
        with open(part) as f, open(tmp, "w") as out:
            for line in f:
                if json.loads(line)["meta"]["id"] not in ids:
                    out.write(line)
        os.replace(tmp, part)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", required=True)
    ap.add_argument("--private-dir")
    ap.add_argument("--audit-dir")
    ap.add_argument("--keep-own-wording", action="store_true")
    a = ap.parse_args(argv)
    if os.environ.get("WISP_REPO"):
        sys.exit("seal_r5: unset WISP_REPO, so the check sees this checkout's toolbench and not the pin's")
    build = Path(a.build)
    with open(build / "all.jsonl") as fe, open(build / "meta.jsonl") as fm:
        rows = [(json.loads(x), json.loads(y)) for x, y in zip(fe, fm, strict=True)]
    index = Index(a.private_dir, a.audit_dir)
    turns = classify(rows, index)
    removed, kept_known = {}, {}
    for tid, t in turns.items():
        if not (t["current"] or t["history"]):
            continue
        (removed if removal(t, a.keep_own_wording) else kept_known)[tid] = t
    ids = {i for t in removed.values() for i in t["ids"]}
    remove_ids(build, ids)

    def table(group: dict) -> dict:
        fam = defaultdict(Counter)
        for t in group.values():
            f = fam[t["family"]]
            f["turns"] += 1
            f["rows"] += t["rows"]
            for where in ("current", "history"):
                for name in t[where]:
                    f[f"{where}_{name}_rows"] += t["rows"]
        return {k: dict(v) for k, v in sorted(fam.items())}

    manifest = {"rule": "no question of a test set in training: same or >=0.7 trigram similar, no exemption",
                "keep_own_wording": a.keep_own_wording, "index_sizes": index.sizes,
                "rows_before": len(rows), "rows_removed": len(ids), "turns_removed": len(removed),
                "by_family": table(removed), "kept_known_overlaps_by_family": table(kept_known),
                "removed": [{"turn": k, "family": t["family"], "rows": t["rows"], "qkind": t["qkind"],
                             "current": t["current"], "history": t["history"]} for k, t in sorted(removed.items())],
                "kept_known_overlaps": [{"turn": k, "family": t["family"], "rows": t["rows"], "current": t["current"],
                                         "history": t["history"]} for k, t in sorted(kept_known.items())]}
    (build / "overlap_manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    r5_stats.refresh(build, status="sealed_source_build")
    print(json.dumps({k: manifest[k] for k in ("rows_before", "rows_removed", "turns_removed", "index_sizes", "by_family")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
