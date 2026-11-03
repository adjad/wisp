#!/usr/bin/env python3
"""Prove that training rows do not overlap the eval and gate sets (RL-SCOPE 4.3, C15). Mac side, no model, no network.

    check_overlap.py --manifest eval_manifest.json --inputs grpo_prompts.overlap.jsonl grpo_text_dev.overlap.jsonl \\
        grpo_mm_dev.overlap.jsonl ../../ask-lora-r4/data/train.jsonl ../../ask-lora-r4/data/valid.jsonl \\
        --report overlap_report.json [--stats grpo_prompts.stats.json] [--media-root mm] [--signoff signoff.json]
    check_overlap.py --inputs new_dev/all.jsonl --against r3=../../ask-lora-r3/data/train.jsonl ...     # authoring time: no manifest

INPUTS are the rows to be proven clean. Three shapes, told apart by their keys:
    sidecar    {"id","modality","users":[..],"conv":str,"thread","folds"} (export_prompts.py writes <out>.overlap.jsonl beside every
               export; vision/audio rows carry "images" / "audio" / "reference" / "target" instead)
    builder    {"messages":[..],"tools":[..]} (train.jsonl, valid.jsonl, all.jsonl); a sibling X.meta.jsonl supplies ids and meta.thread
REFERENCES (the excluded sets) are the manifest's "sets" plus every --against NAME=FILE (same shapes as INPUTS; all user turns count).
Manifest (eval_manifest.json, frozen before RL data; this is the format read here):
    {"sets": {NAME: {"prompts": [text, ..], "folds": [leak.fold, ..], "threads": [id, ..]}},   # any of the three keys
     "threads": [..], "shingles": PATH | {"n_prompts", "items"}, "min_shingle_prompts": N,
     "fact_values": [lowercase strings],                      # else the builder's toolbench fact values (needs the repo's scripts/toolbench)
     "gate_images": [{"name","sha256","dhash":HEX256,"flat":bool}], "image_literals": [..], "image_literal_allowlist": [..],
     "audio": {"truth": [transcripts], "pcm_sha256": [..]}, "forbidden_ids": [row id or id prefix, ..]}

Checks, in the order of RL-SCOPE 4.3 (every user turn of every text row, not only the last):
    2 fold       a user turn whose leak.fold equals a reference fold                      -> hit
    1 thread     a row whose meta.thread is a reference thread (session families split by thread) -> hit
    3 trigram    char-trigram Jaccard >= 0.7 -> hit; 0.5 to 0.7 -> review (a person signs it off with --signoff)
    4 shingle    the toolbench sha1 word n-gram export: full-prompt hash -> hit; a shared 6-gram -> review
    5 facts      toolbench fact values in the conversation text before the target -> hit
    6 vision     sha256 or 256-bit dHash (Hamming <= 10, flat images excluded) against the gate images; gate image literals
                 inside a row's target (unless allowlisted); forbidden_ids -> hit
    7 audio      normalised transcript trigram >= 0.7 against audio.truth, PCM sha256 -> hit (voice reuse is not checked)
    8 record     sha256 of the report, of every input and of every excluded set; with --stats they go into the stats JSON ("overlap")
Exit: 0 clean, 1 any hit or unchecked row, 3 a check could not run (skipped, see report["skipped"]; --allow-skips), 2 only unsigned
review items remain. The report holds no timestamps, so the same inputs give the same sha256.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import wave
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDER_LEAK = HERE.parent.parent / "ask-lora-r2" / "builder" / "leak.py"
HIT_JACCARD, REVIEW_JACCARD = 0.7, 0.5
DHASH_HAMMING = 10
_LEAK = None


def leak_mod():
    """The builder's leak.py (fold, trigrams, LeakIndex, fact_hits), loaded by path so ask-lora-r2/builder never lands on sys.path."""
    global _LEAK
    if _LEAK is None:
        spec = importlib.util.spec_from_file_location("ask_lora_builder_leak", BUILDER_LEAK)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ask_lora_builder_leak"] = mod
        spec.loader.exec_module(mod)
        _LEAK = mod
    return _LEAK


def sha256_file(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canon_sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else ""


def _conv(msgs: list[dict]) -> str:
    parts = []
    for m in msgs:
        if m.get("role") == "system":
            continue
        parts.append(_text(m))
        for tc in m.get("tool_calls") or ():
            a = (tc.get("function") or {}).get("arguments")
            parts.append(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


# ------------------------------------------------------------------ reading records
def read_records(path) -> list[dict]:
    """Normalised records {"id","modality","users","conv","thread","images","audio","reference","target","src"} from one file."""
    path = Path(path)
    rows = [json.loads(l) for l in open(path) if l.strip()]
    side = path.with_suffix(".overlap.jsonl")
    if rows and "label" in rows[0] and "prompt" in rows[0] and "users" not in rows[0] and not path.name.endswith(".overlap.jsonl") and side.is_file():
        return read_records(side)           # grpo_prompts.jsonl / grpo_text_dev.jsonl / grpo_mm_dev.jsonl: the export's sidecar has the user turns
    metas: list = []
    sib = path.with_name(path.name[:-len(".jsonl")] + ".meta.jsonl") if path.name.endswith(".jsonl") and ".meta" not in path.name else None
    if sib is None and path.name == "all.jsonl":
        sib = path.with_name("meta.jsonl")
    if sib is not None and sib.is_file():
        metas = [json.loads(l) for l in open(sib) if l.strip()]
        if len(metas) != len(rows):
            metas = []
    out = []
    for i, row in enumerate(rows):
        meta = metas[i] if metas else {}
        rec = {"id": row.get("id") or meta.get("id") or f"{path.name}:{i + 1}", "modality": row.get("modality", "text"),
               "thread": row.get("thread") or meta.get("thread"), "images": row.get("images") or [], "audio": row.get("audio"),
               "reference": row.get("reference"), "target": row.get("target"), "src": path.name, "users": None, "conv": ""}
        if "messages" in row:
            msgs = row["messages"]
            hist = msgs[:-1] if msgs and msgs[-1].get("role") == "assistant" else msgs
            rec["users"] = [_text(m) for m in msgs if m.get("role") == "user"]
            rec["conv"] = _conv(hist)
        elif row.get("users") is not None:
            rec["users"], rec["conv"] = list(row["users"]), row.get("conv") or ""
        elif rec["modality"] != "text":
            rec["users"] = []
        out.append(rec)
    return out


# ------------------------------------------------------------------ image and audio helpers
def dhash(path, size: int = 16):
    """(256-bit dHash as an int, flat?) or None when PIL is missing / the file is not an image."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(path).convert("L").resize((size + 1, size))
    except Exception:  # noqa: BLE001 - a dummy or truncated file is just "not hashable"
        return None
    px = list(im.get_flattened_data()) if hasattr(im, "get_flattened_data") else list(im.getdata())
    flat = (max(px) - min(px)) < 8
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (px[r * (size + 1) + c] > px[r * (size + 1) + c + 1])
    return bits, flat


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def gate_image_hashes(directory) -> list[dict]:
    """gate_images entries for a manifest: every image under `directory` with its sha256 and dHash."""
    out = []
    for p in sorted(Path(directory).rglob("*")):
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp") and p.is_file():
            h = dhash(p)
            out.append({"name": p.name, "sha256": sha256_file(p), "dhash": f"{h[0]:064x}" if h else None, "flat": bool(h and h[1])})
    return out


def pcm_sha256(path):
    try:
        with wave.open(str(path), "rb") as w:
            return hashlib.sha256(w.readframes(w.getnframes())).hexdigest()
    except (wave.Error, EOFError, OSError):
        return None


def squash(s: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", (s or "").lower()).split())


def _norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _h(tokens: list[str]) -> str:
    return hashlib.sha1(" ".join(tokens).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ the checks
def load_references(manifest: dict | None, against: list[str], L) -> dict:
    """{name: {"prompts": [...], "folds": set, "threads": set}} from the manifest sets and the --against files."""
    refs: dict = {}
    for name, s in ((manifest or {}).get("sets") or {}).items():
        refs[name] = {"prompts": list(s.get("prompts") or ()), "folds": set(s.get("folds") or ()), "threads": set(s.get("threads") or ())}
    if (manifest or {}).get("threads"):
        refs.setdefault("manifest", {"prompts": [], "folds": set(), "threads": set()})["threads"] |= set(manifest["threads"])
    for spec in against:
        name, _, path = spec.partition("=")
        if not path:
            name, path = Path(spec).stem, spec
        recs = read_records(path)
        prompts = [u for r in recs for u in (r["users"] or ())]
        refs[name] = {"prompts": prompts, "folds": set(), "threads": {r["thread"] for r in recs if r["thread"]}}
    for r in refs.values():
        r["folds"] |= {L.fold(p) for p in r["prompts"] if p}
        r["folds"].discard("")
    return refs


def run_checks(inputs: list, manifest: dict | None = None, against: list | None = None, media_root=None, signoff: set | None = None,
               facts: str | None = None, allow_skips: bool = False, shingles: dict | None = None) -> dict:
    L = leak_mod()
    manifest = manifest or {}
    refs = load_references(manifest, against or [], L)
    skipped: list[str] = []
    hits: list[dict] = []
    review: list[dict] = []

    def hit(check, rec, **kw):
        hits.append({"check": check, "row": rec["id"], "file": rec["src"], **kw})

    records: list[dict] = []
    in_info = {}
    for p in inputs:
        recs = read_records(p)
        records += recs
        in_info[str(p)] = {"sha256": sha256_file(p), "rows": len(recs)}
    fold_owner = {}
    for name in sorted(refs):
        for f in refs[name]["folds"]:
            fold_owner.setdefault(f, name)
    thread_owner = {t: name for name in sorted(refs) for t in refs[name]["threads"]}
    # trigram index over every reference prompt
    entries = [L._entry(f"{name}#{i}", p) for name in sorted(refs) for i, p in enumerate(refs[name]["prompts"]) if p.strip()]
    index = L.LeakIndex(questions=entries, toolbench=False, chat=False) if entries else None
    # shingles
    sh = shingles if shingles is not None else manifest.get("shingles")
    if isinstance(sh, str):
        sh = json.loads(Path(sh).read_text())
    full_hashes, six = set(), set()
    if sh is not None:
        for it in sh.get("items") or ():
            full_hashes.add(it["full"])
            six |= set((it.get("sh") or {}).get("6") or ())
        need = manifest.get("min_shingle_prompts")
        if need and sh.get("n_prompts", 0) < need:
            hits.append({"check": "shingle", "row": None, "file": None,
                         "why": f"the shingle export holds {sh.get('n_prompts')} prompts, the manifest expects at least {need}: a held-out set was left out"})
    else:
        skipped.append("shingle: no shingles in the manifest")
    # fact values
    fact_re = None
    mode = facts or ("manifest" if manifest.get("fact_values") else "toolbench")
    if mode == "manifest":
        vals = sorted((v.lower() for v in manifest.get("fact_values") or ()), key=len, reverse=True)
        fact_re = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(v) for v in vals) + r")(?![a-z0-9])", re.I) if vals else None
    elif mode == "toolbench":
        try:
            fact_re = L._fact_regex()
        except Exception as e:  # noqa: BLE001 - no repo toolbench here: say so, never pass silently
            skipped.append(f"facts: toolbench fact values unavailable ({type(e).__name__}: {e})")
    else:
        skipped.append("facts: skipped by flag")
    gate_images = manifest.get("gate_images") or []
    gate_sha = {g["sha256"]: g.get("name") for g in gate_images}
    gate_dh = [(int(g["dhash"], 16), g.get("name")) for g in gate_images if g.get("dhash") and not g.get("flat")]
    audio_truth = [L._entry(f"truth#{i}", t) for i, t in enumerate((manifest.get("audio") or {}).get("truth") or ())]
    audio_index = L.LeakIndex(questions=audio_truth, toolbench=False, chat=False) if audio_truth else None
    pcm_gate = set((manifest.get("audio") or {}).get("pcm_sha256") or ())
    literals = [squash(x) for x in manifest.get("image_literals") or () if len(squash(x)) >= 4]
    allow_lit = {squash(x) for x in manifest.get("image_literal_allowlist") or ()}
    forbidden = list(manifest.get("forbidden_ids") or ())
    unchecked = 0
    cache: dict = {}
    for rec in records:
        base_id = rec["id"].rsplit("#", 1)[0]
        for fid in forbidden:
            if base_id == fid or base_id.startswith(fid):
                hit("forbidden_id", rec, why=f"{fid} must be dropped (it carries a gate payload)")
        if rec["modality"] == "text":
            if rec["users"] is None:
                unchecked += 1
                hit("unchecked", rec, why="no user turns in the record: use the export's .overlap.jsonl sidecar")
                continue
            if rec["thread"] and rec["thread"] in thread_owner:
                hit("thread", rec, thread=rec["thread"], set=thread_owner[rec["thread"]])
            for u in rec["users"]:
                if not u.strip():
                    continue
                f = L.fold(u)
                if f in fold_owner:
                    hit("fold", rec, turn=u[:80], set=fold_owner[f])
                    continue                                         # an exact hit needs no similarity pass
                if index is not None:
                    if u not in cache:
                        cache[u] = index.nearest(u)
                    score, q = cache[u]
                    if score >= HIT_JACCARD:
                        hit("trigram", rec, turn=u[:80], jaccard=round(score, 3), match=q["id"], matched=q["prompt"][:80])
                    elif score >= REVIEW_JACCARD:
                        key = f"{rec['id']}|{f}|{q['id']}"
                        review.append({"check": "trigram", "key": key, "row": rec["id"], "turn": u[:80], "jaccard": round(score, 3),
                                       "match": q["id"], "matched": q["prompt"][:80], "signed": key in (signoff or ())})
                if sh is not None:
                    toks = _norm_words(u)
                    if toks and _h(toks) in full_hashes:
                        hit("shingle", rec, turn=u[:80], why="full-prompt hash")
                    elif len(toks) >= 6:
                        grams = {_h(toks[i:i + 6]) for i in range(len(toks) - 5)}
                        if grams & six:
                            key = f"{rec['id']}|{f}|6gram"
                            review.append({"check": "shingle", "key": key, "row": rec["id"], "turn": u[:80], "signed": key in (signoff or ())})
            if fact_re is not None and rec["conv"]:
                found = sorted({m.group(0).lower() for m in fact_re.finditer(rec["conv"])})
                if found:
                    hit("facts", rec, values=found[:6])
        elif rec["modality"] == "vision":
            tgt = squash(rec.get("target") or "")
            for lit in literals:
                if lit in tgt and lit not in allow_lit:
                    hit("image_literal", rec, literal=lit)
            for rel in rec["images"]:
                p = Path(media_root) / rel if media_root else None
                if p is None or not p.is_file():
                    skipped.append(f"vision: image {rel} not found under --media-root")
                    continue
                s = sha256_file(p)
                if s in gate_sha:
                    hit("image_sha256", rec, image=rel, gate=gate_sha[s])
                if gate_dh:
                    h = dhash(p)
                    if h is None:
                        if "vision: dHash unavailable (PIL missing or unreadable image)" not in skipped:
                            skipped.append("vision: dHash unavailable (PIL missing or unreadable image)")
                    elif not h[1]:
                        for gh, gname in gate_dh:
                            d = hamming(h[0], gh)
                            if d <= DHASH_HAMMING:
                                hit("image_dhash", rec, image=rel, gate=gname, hamming=d)
        elif rec["modality"] == "audio":
            ref = rec.get("reference")
            if ref and audio_index is not None:
                score, q = audio_index.nearest(ref)
                if score >= HIT_JACCARD:
                    hit("audio_transcript", rec, jaccard=round(score, 3), match=q["id"])
            if pcm_gate and rec.get("audio"):
                p = Path(media_root) / rec["audio"] if media_root else None
                if p and p.is_file():
                    s = pcm_sha256(p)
                    if s and s in pcm_gate:
                        hit("audio_pcm", rec, clip=rec["audio"])
    unsigned = [r for r in review if not r["signed"]]
    if hits:
        result = 1
    elif skipped and not allow_skips:
        result = 3
    elif unsigned:
        result = 2
    else:
        result = 0
    report = {
        "version": 1, "inputs": in_info, "rows": len(records), "by_modality": dict(Counter(r["modality"] for r in records)),
        "manifest_sha256": canon_sha(manifest) if manifest else None,
        "excluded_sets": {n: {"sha256": canon_sha({"prompts": sorted(refs[n]["prompts"]), "folds": sorted(refs[n]["folds"]),
                                                   "threads": sorted(refs[n]["threads"])}),
                              "prompts": len(refs[n]["prompts"]), "folds": len(refs[n]["folds"]), "threads": len(refs[n]["threads"])}
                          for n in sorted(refs)},
        "shingle_n_prompts": (sh or {}).get("n_prompts"), "facts_mode": mode,
        "hits": hits, "review": review, "review_unsigned": len(unsigned), "unchecked_rows": unchecked, "skipped": sorted(set(skipped)),
        "hit_count": len(hits), "exit": result,
        "result": {0: "clean", 1: "hits", 2: "review", 3: "incomplete"}[result],
    }
    return report


def update_stats(stats_path, report: dict, report_path) -> None:
    """Record the report sha256 and every excluded set's sha256 in grpo_prompts.stats.json (RL-SCOPE 4.3 step 8)."""
    p = Path(stats_path)
    st = json.loads(p.read_text()) if p.exists() else {}
    st["overlap"] = {"checked": True, "report": str(report_path), "report_sha256": sha256_file(report_path), "exit": report["exit"],
                     "result": report["result"], "manifest_sha256": report["manifest_sha256"],
                     "excluded_sets": {n: v["sha256"] for n, v in report["excluded_sets"].items()},
                     "inputs": {k: v["sha256"] for k, v in report["inputs"].items()}}
    p.write_text(json.dumps(st, indent=1) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inputs", nargs="*", default=[], help="rows to prove clean (sidecar or builder jsonl)")
    ap.add_argument("--manifest", default=None, help="eval_manifest.json")
    ap.add_argument("--against", action="append", default=[], metavar="NAME=FILE", help="a further excluded set (builder or sidecar jsonl)")
    ap.add_argument("--report", default="overlap_report.json")
    ap.add_argument("--stats", default=None, help="grpo_prompts.stats.json: gets an 'overlap' block with the report sha256")
    ap.add_argument("--media-root", default=None, help="folder the vision/audio paths of the sidecars are relative to (the export's mm/)")
    ap.add_argument("--signoff", default=None, help="JSON list of review keys a person has signed off")
    ap.add_argument("--shingles", default=None, help="run_toolbench.py --export-shingles output (overrides the manifest's)")
    ap.add_argument("--facts", choices=("manifest", "toolbench", "skip"), default=None)
    ap.add_argument("--allow-skips", action="store_true", help="a skipped check is reported but does not make the exit non-zero")
    ap.add_argument("--hash-images", default=None, metavar="DIR", help="print gate_images manifest entries for the images under DIR and exit")
    a = ap.parse_args(argv)
    if a.hash_images:
        print(json.dumps(gate_image_hashes(a.hash_images), indent=1))
        return 0
    if not a.inputs:
        ap.error("--inputs is required")
    manifest = json.loads(Path(a.manifest).read_text()) if a.manifest else None
    signoff = set()
    if a.signoff:
        raw = json.loads(Path(a.signoff).read_text())
        signoff = {x if isinstance(x, str) else x["key"] for x in (raw.get("signed", raw) if isinstance(raw, dict) else raw)}
    sh = json.loads(Path(a.shingles).read_text()) if a.shingles else None
    rep = run_checks(a.inputs, manifest, a.against, a.media_root, signoff, a.facts, a.allow_skips, sh)
    Path(a.report).write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n")
    if a.stats:
        update_stats(a.stats, rep, a.report)
    print(f"check_overlap: {rep['rows']} rows, {rep['hit_count']} hits, {len(rep['review'])} review ({rep['review_unsigned']} unsigned), "
          f"{len(rep['skipped'])} skipped -> {rep['result']} (report {a.report})")
    for h in rep["hits"][:10]:
        print("  HIT", {k: v for k, v in h.items() if v is not None})
    for s in rep["skipped"]:
        print("  SKIPPED", s)
    return rep["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
