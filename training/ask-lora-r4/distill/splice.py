#!/usr/bin/env python3
"""Splice distilled chat replies into a round-4 build dir.

``chosen.jsonl`` (one line per distilled scenario: id, family, k, request_hash, content, reasoning) is the output of the
choose step over ``gen.py``'s samples. For every build row whose meta id is in it, the LAST message (the scripted
assistant reply) is replaced by the model's own reply, and its meta is updated: ``distilled`` true, ``think`` and
``thought`` from the model's own reasoning, ``distill_k`` the sample index. Everything else is left alone.

The six files of the build (all/train/valid .jsonl and their .meta.jsonl, line-aligned) are spliced the same way, so one
id carries one content everywhere. Before anything is written, every chosen row is checked: its ``request_hash`` must
equal ``request_hash(messages[:-1], tools)`` of the build row (so the reply still answers the exact request it was
sampled for), and the row's last message must be a plain assistant message (no tool calls). Any failure aborts with
the ids listed and writes nothing. Rows that are not touched keep their original bytes. Files are written atomically,
and a rerun with the same input gives the same files. ``partial.jsonl`` and ``stats.json`` are not changed (the
partial keeps the scripted targets); ``distill.json`` is written beside the spliced files with the counts, the missing
ids and the sha256 of each chosen file.

    splice.py --build $TMPDIR/r4 --chosen chosen.jsonl [--chosen more.jsonl ...] [--out $TMPDIR/r4-distilled]

Without ``--out`` the build dir is modified in place; with it, the build dir is copied there first and never touched.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen import request_hash  # noqa: E402  (same normalisation as the sampler: object/string tool args hash alike)

PAIRS = (("all.jsonl", "meta.jsonl"), ("train.jsonl", "train.meta.jsonl"), ("valid.jsonl", "valid.meta.jsonl"))
FILES = tuple(n for p in PAIRS for n in p)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8", newline="") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


def write_atomic(path: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".splice-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_chosen(paths: list[str]) -> tuple[dict, list[dict], list[str]]:
    """id -> chosen record, the files used with their hashes, and problems (bad lines, conflicting duplicates)."""
    chosen, used, errors = {}, [], []
    for p in paths:
        n = 0
        for i, line in enumerate(read_lines(p), 1):
            try:
                c = json.loads(line)
                cid, content = c["id"], c["content"]
                c["request_hash"], c["k"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                errors.append(f"{p}:{i}: unreadable chosen row ({exc!r})")
                continue
            if not isinstance(content, str) or not content.strip():
                errors.append(f"{p}:{i}: {cid}: empty or non-string content")
                continue
            if c.get("tool_calls"):
                errors.append(f"{p}:{i}: {cid}: chosen row carries tool calls, not a text reply")
                continue
            old = chosen.get(cid)
            if old and (old["content"], old.get("reasoning"), old["request_hash"], old["k"]) != (
                    content, c.get("reasoning"), c["request_hash"], c["k"]):
                errors.append(f"{p}:{i}: {cid}: conflicts with an earlier chosen row for the same id")
                continue
            chosen[cid] = c
            n += 1
        used.append({"path": os.path.abspath(p), "sha256": sha256(p), "rows": n})
    return chosen, used, errors


def splice_pair(build: str, ex_name: str, meta_name: str, chosen: dict, errors: list[str]):
    """Return (example lines, meta lines, ids spliced, think count); problems go to ``errors``."""
    ex_lines, meta_lines = read_lines(os.path.join(build, ex_name)), read_lines(os.path.join(build, meta_name))
    if len(ex_lines) != len(meta_lines):
        errors.append(f"{ex_name}/{meta_name}: {len(ex_lines)} examples but {len(meta_lines)} metas (not line-aligned)")
        return ex_lines, meta_lines, [], 0
    spliced, think = [], 0
    for i, mline in enumerate(meta_lines):
        meta = json.loads(mline)
        c = chosen.get(meta["id"])
        if c is None:
            continue
        ex = json.loads(ex_lines[i])
        last = ex["messages"][-1]
        if last.get("role") != "assistant" or last.get("tool_calls"):
            errors.append(f"{ex_name}:{i + 1}: {meta['id']}: last message is not a plain assistant reply")
            continue
        if c.get("family") not in (None, meta["family"]):
            errors.append(f"{ex_name}:{i + 1}: {meta['id']}: family {meta['family']!r} in the build, {c['family']!r} chosen")
            continue
        got = request_hash(ex["messages"][:-1], ex.get("tools"))
        if got != c["request_hash"]:
            errors.append(f"{ex_name}:{i + 1}: {meta['id']}: request_hash {got} in the build, {c['request_hash']} chosen")
            continue
        last.pop("reasoning_content", None)
        last.pop("reasoning", None)
        last["content"] = c["content"]
        reasoning = (c.get("reasoning") or "").strip()
        meta["distilled"], meta["think"], meta["thought"] = True, bool(reasoning), reasoning or None
        meta["distill_k"] = c["k"]
        ex_lines[i] = json.dumps(ex, ensure_ascii=False)
        meta_lines[i] = json.dumps(meta, ensure_ascii=False)
        spliced.append(meta["id"])
        think += bool(reasoning)
    return ex_lines, meta_lines, spliced, think


def splice(build: str, chosen_paths: list[str], out: str | None = None) -> dict:
    chosen, used, errors = load_chosen(chosen_paths)
    results, per_file = {}, {}
    for ex_name, meta_name in PAIRS:
        results[ex_name] = splice_pair(build, ex_name, meta_name, chosen, errors)
        per_file[ex_name] = per_file[meta_name] = len(results[ex_name][2])

    # One id, one content: an id spliced in all.jsonl must be spliced in exactly one of train/valid, and vice versa.
    ids = {n: collections.Counter(results[n][2]) for n, _ in PAIRS}
    for cid, n in ids["all.jsonl"].items():
        where = ids["train.jsonl"][cid] + ids["valid.jsonl"][cid]
        if n != 1 or where != 1:
            errors.append(f"{cid}: in all.jsonl x{n} but train+valid x{where}")
    for name in ("train.jsonl", "valid.jsonl"):
        errors += [f"{cid}: in {name} but not in all.jsonl" for cid in ids[name] if cid not in ids["all.jsonl"]]
    if errors:
        raise SystemExit("splice aborted, nothing written:\n  " + "\n  ".join(errors[:40])
                         + (f"\n  ... and {len(errors) - 40} more" if len(errors) > 40 else ""))

    metas = [json.loads(m) for m in results["all.jsonl"][1]]
    present = {m["id"] for m in metas}
    missing = sorted(set(chosen) - present)
    fams = {m["family"] for m in metas if m["id"] in chosen}      # the distilled families, as the build names them
    spliced_ids = set(results["all.jsonl"][2])
    by_family = {f: {"spliced": 0, "unchanged": 0} for f in sorted(fams)}
    for m in metas:
        if m["family"] in by_family:
            by_family[m["family"]]["spliced" if m["id"] in spliced_ids else "unchanged"] += 1

    dest = out or build
    if out and os.path.abspath(out) != os.path.abspath(build):
        os.makedirs(out, exist_ok=True)
        for name in os.listdir(build):
            if name not in FILES and os.path.isfile(os.path.join(build, name)):
                shutil.copy2(os.path.join(build, name), os.path.join(out, name))
    for ex_name, meta_name in PAIRS:
        ex_lines, meta_lines = results[ex_name][:2]
        write_atomic(os.path.join(dest, ex_name), "".join(ln + "\n" for ln in ex_lines))
        write_atomic(os.path.join(dest, meta_name), "".join(ln + "\n" for ln in meta_lines))
    report = {"build": os.path.abspath(build), "out": os.path.abspath(dest), "chosen_files": used,
              "chosen_ids": len(chosen), "spliced_per_file": per_file, "spliced_per_family": {
                  f: v["spliced"] for f, v in by_family.items()},
              "unchanged_per_family": {f: v["unchanged"] for f, v in by_family.items()},
              "think": results["all.jsonl"][3], "no_think": len(spliced_ids) - results["all.jsonl"][3],
              "missing_ids": missing}
    write_atomic(os.path.join(dest, "distill.json"), json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", required=True, help="builder output dir (all/train/valid .jsonl + .meta.jsonl)")
    ap.add_argument("--chosen", required=True, action="append", help="chosen.jsonl (repeatable)")
    ap.add_argument("--out", help="copy the build here and splice the copy (default: in place)")
    a = ap.parse_args(argv)
    r = splice(a.build, a.chosen, a.out)
    print(f"spliced {r['spliced_per_file']['all.jsonl']} of {r['chosen_ids']} chosen ids into {r['out']} "
          f"(all/train/valid: {r['spliced_per_file']['all.jsonl']}/{r['spliced_per_file']['train.jsonl']}/"
          f"{r['spliced_per_file']['valid.jsonl']}; think {r['think']}, no think {r['no_think']})")
    for fam in r["spliced_per_family"]:
        print(f"  {fam}: {r['spliced_per_family'][fam]} spliced, {r['unchanged_per_family'][fam]} unchanged "
              f"(no chosen sample)")
    if r["missing_ids"]:
        print(f"warning: {len(r['missing_ids'])} chosen ids are in no build row: {r['missing_ids'][:10]}"
              f"{' ...' if len(r['missing_ids']) > 10 else ''}", file=sys.stderr)


if __name__ == "__main__":
    main()
