#!/usr/bin/env python3
"""The last leakage check on a finished r5 build (train and valid). Read-only on the build; writes only under --out.

    leak_check_r5.py --build DIR --out DIR [--private-dir ~/.moe/evals/private-real-use] [--audit-dir DIR]
                     [--private-results FILE ...]

1. Question overlap. Every user turn of every row (the current one, and history turns of 4+ words, seal_r5's rule) is compared,
   by leak.py's 0.7 character-trigram Jaccard, with: ``public`` (eval questions + this checkout's toolbench, every split, bound
   across worlds, with history turns), ``chat4`` (the r4 chat eval), ``private`` (every cases.yaml under --private-dir, at any
   depth, prompt + history user turns) and ``audit`` (the audit prompts). Policy: any hit removes the whole turn, no exemption
   (``id.rsplit('-s', 1)[0]``), so the report lists the turns that would go.
2. Private-results denylist. Toolbench results.jsonl files from real-use runs (--private-results, or every
   ``<private-dir>/**/runs/*/results.jsonl``) become validate.py's ``{text, results}`` rows, in a temp dir under --out that is
   deleted at once. validate.private_denylist reads them; every example of the build is then checked as validate.py does
   (names, emails, subject/title phrases). The report keeps counts, row ids and categories, never a string.

The report holds row ids, case ids, scores and counts only. Exit 1 on any overlap hit, any email or phrase hit, or a name found
nowhere synthetic; 4 if every remaining hit is a name also found, as a whole word, in the toolbench world or the committed builder
code (counted and listed, never excused: the owner decides); 2 if nothing hit but a check was INCOMPLETE; else 0.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINING = HERE.parent.parent
sys.path.insert(0, str(TRAINING / "ask-lora-r2" / "builder"))
sys.path.insert(0, str(HERE))
os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import seal_r5  # noqa: E402

SPLITS = (("train.jsonl", "train.meta.jsonl"), ("valid.jsonl", "valid.meta.jsonl"))
HISTORY_MIN_WORDS = seal_r5.HISTORY_MIN_WORDS
SOURCES = ("public", "chat4", "private", "audit")


def turn_of(row_id: str) -> str:
    return row_id.rsplit("-s", 1)[0]


# ------------------------------------------------------------------ inputs
def message_text(m: dict) -> str | None:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):     # picture turns: the text parts
        parts = [p.get("text") for p in c if isinstance(p, dict) and isinstance(p.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


def user_turns(ex: dict) -> list[str]:
    return [t for m in ex.get("messages", []) if m.get("role") == "user" and (t := message_text(m)) is not None]


def read_build(build: Path) -> list[tuple[str, dict, dict]]:
    rows = []
    for ex_name, meta_name in SPLITS:
        ex_path, meta_path = build / ex_name, build / meta_name
        if not ex_path.is_file() or not meta_path.is_file():
            raise SystemExit(f"leak_check_r5: {ex_path.name} / {meta_path.name} missing in {build}")
        with open(ex_path) as fe, open(meta_path) as fm:
            for a, b in zip(fe, fm, strict=True):
                rows.append((ex_name.split(".")[0], json.loads(a), json.loads(b)))
    return rows


def private_cases_files(root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "**", "cases.yaml"), recursive=True))


def private_entries(root: str) -> list[dict]:
    """seal_r5's private prompts, plus any cases.yaml deeper than one folder (seal_r5 reads the root and one level)."""
    import leak
    import yaml
    out = list(seal_r5.private_prompts(root))
    seen = {(e["id"], e["fold"]) for e in out}
    for path in private_cases_files(root):
        tag = "real-use" if os.path.dirname(path) == root.rstrip("/") else os.path.relpath(os.path.dirname(path), root)
        for c in yaml.safe_load(open(path)) or []:
            if not isinstance(c, dict):
                continue
            texts = [c.get("prompt")] + [t.get("user") for t in c.get("history") or [] if isinstance(t, dict)]
            for t in texts:
                e = leak._entry(f"private:{tag}:{c.get('id')}", str(t)) if t else None
                if e and (e["id"], e["fold"]) not in seen:
                    seen.add((e["id"], e["fold"]))
                    out.append(e)
    return out


def find_audit_dir() -> str | None:
    """Where the audit's diagnostic files live (the Codex outputs folder the r5 request came with)."""
    names = ("paired_behavior_review.json", "multi_source_20_trajectories.json")
    hits = [p for p in glob.glob(os.path.expanduser("~/Documents/Codex/*/*/outputs"))
            if any(os.path.isfile(os.path.join(p, n)) for n in names)]
    return max(hits, key=os.path.getmtime) if hits else None


def build_index(private_dir: str | None, audit_dir: str | None):
    """seal_r5.Index for public + chat4 + audit; its private set is replaced by the recursive one."""
    import leak
    index = seal_r5.Index(None, audit_dir)
    priv = private_entries(private_dir) if private_dir and os.path.isdir(private_dir) else []
    if priv:
        index.sets["private"] = leak.LeakIndex(priv)
    index.sizes["private"] = len(priv)
    return index


# ------------------------------------------------------------------ question overlap
def check_overlap(rows, index) -> dict:
    """{source: [[row_id, turn_index, case_id, score], ...]}. turn_index counts the row's user turns from 0."""
    hits = {s: [] for s in SOURCES}
    for _split, ex, meta in rows:
        users = user_turns(ex)
        for i, text in enumerate(users):
            if i < len(users) - 1 and len(text.split()) < HISTORY_MIN_WORDS:
                continue
            for src, (case_id, score) in index.hits(text).items():
                hits.setdefault(src, []).append([meta["id"], i, case_id, score])
    return hits


def removal_plan(rows, hits: dict) -> dict:
    prefixes = {turn_of(h[0]) for src in hits.values() for h in src}
    by_turn: dict[str, list[str]] = {}
    for _s, _ex, meta in rows:
        if turn_of(meta["id"]) in prefixes:
            by_turn.setdefault(turn_of(meta["id"]), []).append(meta["id"])
    return {"turns": len(by_turn), "rows": sum(len(v) for v in by_turn.values()),
            "removed_turns": [{"turn": t, "rows": ids} for t, ids in sorted(by_turn.items())]}


# ------------------------------------------------------------------ private-results denylist
def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _strings(v)


def results_row(r: dict) -> dict:
    """One toolbench result as validate.private_denylist's row: ``text`` is the final answer, ``results`` every other string the
    run produced that real data can sit in: the message draft, the error, each step's text, tool errors, call arguments, the
    effects it caused and what it asked. (The runner stores a step's calls and errors, not the raw tool output.)"""
    results = []
    for k in ("message_draft", "error"):
        if r.get(k):
            results += list(_strings(r[k]))
    for s in r.get("steps") or []:
        if not isinstance(s, dict):
            continue
        results += list(_strings(s.get("content")))
        results += list(_strings(s.get("tool_errors")))
        for c in s.get("calls") or []:
            if isinstance(c, dict):
                results += list(_strings(c.get("args")))
    for k in ("effects", "asked"):
        results += list(_strings(r.get(k)))
    return {"text": r.get("final") if isinstance(r.get("final"), str) else "", "results": [x for x in results if x]}


def results_files(private_dir: str | None, explicit: list[str] | None) -> list[str]:
    if explicit:
        return [p for p in explicit if os.path.isfile(p)]
    if not private_dir:
        return []
    return sorted(glob.glob(os.path.join(private_dir, "**", "runs", "*", "results.jsonl"), recursive=True))


def convert_results(paths: list[str]) -> tuple[list[dict], int]:
    rows, bad = [], 0
    for p in paths:
        for line in open(p):
            if not line.strip():
                continue
            try:
                rows.append(results_row(json.loads(line)))
            except (ValueError, AttributeError):
                bad += 1
    return rows, bad


def load_denylist(rows: list[dict], out_dir: Path):
    """validate.private_denylist over in-memory rows. It wants a path, so a 0600 file lives in a temp dir under --out for the call."""
    import validate
    tmp = Path(tempfile.mkdtemp(prefix=".denylist-", dir=out_dir))
    try:
        os.chmod(tmp, 0o700)
        path = tmp / "private-results.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return validate.private_denylist(str(path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def denylist_hits(build_rows, deny, wl=None, synthetic_text: str = "") -> list[dict]:
    """validate.py's privacy:real_name / real_email / real_phrase, per message so the role is known. Ids and categories only."""
    import re

    import leak
    import validate
    # The kit's persona (Sam Rivera, Maya) is the toolbench world's, so the real runs name it too; validate.py's own fact check
    # exempts it the same way (leak._KIT_PERSONA). Without this every row's system prompt would hit.
    persona = {w for name in leak._KIT_PERSONA for w in name.split()}
    wl = (validate.PUBLIC_WORDS | persona) if wl is None else wl
    tokens, emails, phrases = deny
    synthetic_words = set(re.findall(r"[a-z][a-z'’-]+", synthetic_text.lower()))
    out = []
    for _split, ex, meta in build_rows:
        found = Counter()
        names, synthetic = set(), set()
        for m in ex.get("messages", []):
            blob = json.dumps(m, ensure_ascii=False)
            low = blob.lower()
            role = m.get("role", "?")
            for tok in set(re.findall(r"\b[A-Z][a-z][A-Za-z'’-]{1,}\b", blob)):
                base = re.split(r"['’]", tok)[0]
                if base in tokens and base.lower() not in wl:
                    found[(role, "real_name")] += 1
                    names.add(base)
                    if base.lower() in synthetic_words:
                        synthetic.add(base)
            found.update({(role, "real_email"): 1 for e in emails if e in low})
            found.update({(role, "real_phrase"): 1 for p in phrases if p in low})
        if found:
            out.append({"row": meta["id"], "hits": sorted({f"{r}:{c}" for r, c in found}), "name_tokens": len(names),
                        "name_tokens_in_synthetic_world": len(synthetic),
                        "other_categories": any(c != "real_name" for _r, c in found)})
    return out


def synthetic_world_text() -> str:
    """Lower-cased text of the toolbench world (fact values, this checkout's toolbench files) and of the committed builders that
    invent the training world. A hit token found here as a whole word is a synthetic name the real runs also use; the report counts
    such tokens apart, it never excuses the row."""
    import leak
    parts = [" ".join(leak.fact_values())]
    root = Path(leak.REPO) / "scripts" / "toolbench"
    for p in root.rglob("*"):
        if p.suffix in (".yaml", ".py") and p.is_file():
            parts.append(p.read_text(errors="ignore"))
    for pattern in ("ask-lora-r2/builder/*.py", "ask-lora-r3/**/*.py", "ask-lora-r4/**/*.py", "ask-lora-r5/**/*.py"):
        for p in TRAINING.glob(pattern):
            if p.is_file() and "/data/" not in str(p) and not p.name.startswith("test_"):
                parts.append(p.read_text(errors="ignore"))
    return " ".join(parts).lower()


# ------------------------------------------------------------------ main
def run(build: Path, out: Path, private_dir=None, audit_dir=None, private_results=None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rows = read_build(build)
    audit_dir = audit_dir or find_audit_dir()
    index = build_index(private_dir, audit_dir)
    hits = check_overlap(rows, index)
    incomplete = []
    if index.sizes.get("private", 0) == 0:
        incomplete.append("private cases: none found")
    if index.sizes.get("audit", 0) == 0:
        incomplete.append("audit prompts: none found")
    sources = {s: {"hits": len(hits.get(s, [])), "rows_hit": len({h[0] for h in hits.get(s, [])}),
                   "list": hits.get(s, []), "reference_prompts": index.sizes.get(s if s != "chat4" else "public_chat4", 0)}
               for s in SOURCES}
    plan = removal_plan(rows, hits)

    files = results_files(private_dir, private_results)
    conv, bad_lines = convert_results(files)
    denylist = {"files": len(files), "result_rows": len(conv), "unreadable_lines": bad_lines,
                "names": 0, "emails": 0, "phrases": 0, "rows_hit": 0, "hits": []}
    if not conv:
        incomplete.append("private-results denylist: no usable results.jsonl rows")
    else:
        deny = load_denylist(conv, out)
        denylist.update(names=len(deny[0]), emails=len(deny[1]), phrases=len(deny[2]))
        dh = denylist_hits(rows, deny, synthetic_text=synthetic_world_text())
        denylist.update(rows_hit=len(dh), hits=dh,
                        rows_hit_only_synthetic_world_names=sum(1 for h in dh if h["name_tokens"] == h["name_tokens_in_synthetic_world"]
                                                                and not h["other_categories"]))
    report = {"rule": "any hit removes the whole turn (all steps), no exemption; trigram Jaccard >= 0.7",
              "rows_checked": len(rows), "threshold": index.leak.THRESHOLD,
              "audit_dir_found": bool(audit_dir), "sources": sources, "would_remove": plan, "denylist": denylist,
              "incomplete": incomplete, "complete": not incomplete}
    (out / "leak_report.json").write_text(json.dumps(report, indent=1) + "\n")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--private-dir", default=os.path.expanduser("~/.moe/evals/private-real-use"))
    ap.add_argument("--audit-dir")
    ap.add_argument("--private-results", nargs="*")
    a = ap.parse_args(argv)
    if os.environ.get("WISP_REPO"):
        sys.exit("leak_check_r5: unset WISP_REPO, so the check sees this checkout's toolbench and not the pin's")
    r = run(Path(a.build), Path(a.out), a.private_dir, a.audit_dir, a.private_results)
    summary = {"rows_checked": r["rows_checked"], "hits": {s: v["hits"] for s, v in r["sources"].items()},
               "would_remove_turns": r["would_remove"]["turns"], "would_remove_rows": r["would_remove"]["rows"],
               "denylist": {k: v for k, v in r["denylist"].items() if k != "hits"}, "incomplete": r["incomplete"]}
    print(json.dumps(summary))
    d = r["denylist"]
    if any(v["hits"] for v in r["sources"].values()) or d["rows_hit"] > d.get("rows_hit_only_synthetic_world_names", 0):
        return 1
    if d["rows_hit"]:
        return 4
    return 0 if r["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
