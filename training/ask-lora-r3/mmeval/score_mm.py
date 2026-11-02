#!/usr/bin/env python
"""Score saved generations: per checkpoint JSON + one markdown table, each judged against the base.
Pure python (no torch); eval_mm.py calls it at the end, and you can re-run it any time on answers.jsonl.

    python score_mm.py --out RUN_DIR --vision-rows vision_rows.jsonl [--audio-rows audio_rows.jsonl] \
        [--train-log OUT/train.log.jsonl] [--tol vision_facts=0.01 ...]

answers.jsonl lines: {"ckpt": "base"|"step-00300"|..., "modality": "vision"|"audio", "id", "raw", "error", "secs"}.
Writes RUN_DIR/results.json, RUN_DIR/<ckpt>.json (per-row scores) and RUN_DIR/report.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rows as R          # noqa: E402
import serving as SV      # noqa: E402
import scoring as S       # noqa: E402
from pick_checkpoint import checkpoint_losses  # noqa: E402


def load_answers(path) -> dict[str, dict[str, dict[str, dict]]]:
    """{ckpt: {modality: {id: answer}}}; a later line for the same key wins (a resumed run)."""
    out: dict = {}
    for line in Path(path).read_text().splitlines():
        if line.strip():
            a = json.loads(line)
            out.setdefault(a["ckpt"], {}).setdefault(a["modality"], {})[a["id"]] = a
    return out


def score_checkpoint(answers: dict, vision_rows: list[dict], audio_rows: list[dict]) -> dict:
    """{"metrics": {"vision": {...}, "audio": {...}}, "rows": {"vision": [...], "audio": [...]}}.
    A dev row with no answer scores as an error (an unfinished run must not look good)."""
    out = {"metrics": {}, "rows": {}}
    serving_rows = [r for r in audio_rows if r.get("_serving")]
    audio_rows = [r for r in audio_rows if not r.get("_serving")]
    reading_rows = [r for r in vision_rows if R.is_reading(r)]       # READ-call rows are their own group
    vision_rows = [r for r in vision_rows if not R.is_reading(r)]
    for modality, group, rows, scorer, agg in (("vision", "vision", vision_rows, S.score_vision_row, S.aggregate_vision),
                                                ("vision", "reading", reading_rows, S.score_reading_row, S.aggregate_reading),
                                                ("audio", "audio", audio_rows, S.score_audio_row, S.aggregate_audio),
                                                ("audio", "audio_serving", serving_rows, S.score_audio_row, S.aggregate_audio)):
        if not rows:
            continue
        got = answers.get(modality, {})
        scored = []
        for r in rows:
            a = got.get(r["id"])
            if a is None:
                scored.append(scorer(r, None, error="no answer"))
            else:
                scored.append(scorer(r, a.get("raw"), error=a.get("error")))
        out["rows"][group] = scored
        out["metrics"][group] = agg(scored)
    return out


def run(out_dir: Path, vision_rows, audio_rows, train_log=None, tol=None, ckpt_root=None) -> dict:
    answers = load_answers(out_dir / "answers.jsonl")
    # The Mac gate's images/clips (rows flagged _gate) are report-only: the Mac gate is the final test on them, so
    # checkpoint selection never sees them (they would make a pass there optimistic).
    gate_v = [r for r in vision_rows if r.get("_gate")]
    gate_a = [r for r in audio_rows if r.get("_gate")]
    vision_rows = [r for r in vision_rows if not r.get("_gate")]
    audio_rows = [r for r in audio_rows if not r.get("_gate")]       # includes the _serving copies of dev rows
    if "base" not in answers:
        raise SystemExit(f"{out_dir}/answers.jsonl has no 'base' generations; the base is the yardstick")
    base = score_checkpoint(answers["base"], vision_rows, audio_rows)
    losses = checkpoint_losses(train_log, list(answers), ckpt_root) if train_log else {}
    results = {"base": base["metrics"], "tolerances": {**S.DEFAULT_TOL, **(tol or {})},
               "n": {"vision": sum(not R.is_reading(r) for r in vision_rows), "reading": sum(R.is_reading(r) for r in vision_rows), "audio": sum(not r.get("_serving") for r in audio_rows),
                                                              "audio_serving": sum(bool(r.get("_serving")) for r in audio_rows)}, "checkpoints": {}}
    gbase = score_checkpoint(answers["base"], gate_v, gate_a)
    results["gate_report_only"] = {}
    if gate_v or gate_a:
        results["gate_n"] = {"vision": len(gate_v), "audio": len(gate_a)}
        results["gate_base"] = gbase["metrics"]
    (out_dir / "base.json").write_text(json.dumps(base, indent=1, ensure_ascii=False))
    for name in answers:
        if name == "base":
            continue
        sc = score_checkpoint(answers[name], vision_rows, audio_rows)
        verdict = S.compare(base["metrics"], sc["metrics"], tol)
        info = losses.get(name, {})
        if gate_v or gate_a:
            gsc = score_checkpoint(answers[name], gate_v, gate_a)
            results["gate_report_only"][name] = {"metrics": gsc["metrics"],
                                                 "verdict": S.compare(gbase["metrics"], gsc["metrics"], tol)}
        results["checkpoints"][name] = {"metrics": sc["metrics"], "verdict": verdict,
                                        "dev_loss_text": info.get("dev_loss_text"), "step": info.get("step"),
                                        "dev_loss_source": info.get("source")}
        (out_dir / f"{name}.json").write_text(json.dumps({**sc, "verdict": verdict}, indent=1, ensure_ascii=False))
    (out_dir / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    md = (S.render_markdown(results) if results["checkpoints"] else
          "# Multimodal checkpoint check\n\nbase only; no checkpoints scored.\n")
    if results["gate_report_only"]:
        md += "\n## Mac-gate images and clips (report only, never used to pick)\n\n"
        for name, g in results["gate_report_only"].items():
            md += f"- {name}: {'within tolerance' if g['verdict']['ok'] else 'FAILS ' + ', '.join(g['verdict']['failed'])}\n"
    (out_dir / "report.md").write_text(md)
    return results


def parse_tol(items) -> dict:
    tol = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if k not in S.DEFAULT_TOL:
            raise SystemExit(f"--tol {k}: unknown check; one of {sorted(S.DEFAULT_TOL)}")
        tol[k] = float(v)
    return tol


def _tag(rows: list[dict], root) -> list[dict]:
    """Remember where a row's media live (eval_mm.py resolves relative image/clip paths against it)."""
    for r in rows:
        r["_root"] = str(root)
    return rows


def load_eval_rows(a) -> tuple[list[dict], list[dict]]:
    """(vision dev rows, audio dev rows) from the CLI flags shared by score_mm.py and eval_mm.py."""
    vis, aud = [], []
    data = Path(a.data) if getattr(a, "data", None) else None
    vp = a.vision_rows or (str(data / "vision_rows.jsonl") if data and (data / "vision_rows.jsonl").is_file() else None)
    if vp:
        vis += _tag(R.load_vision_dev(vp, a.split), Path(vp).resolve().parent)
    if a.vision_cases:
        vis += _tag(R.vision_from_cases(a.vision_cases), Path(a.vision_cases).resolve().parent)
    ap = a.audio_rows or (str(data / "audio_rows.jsonl") if data and (data / "audio_rows.jsonl").is_file() else None)
    if ap:
        aud += _tag(R.load_audio_dev(ap), Path(ap).resolve().parent)
    if a.audio_truth:
        aud += _tag(R.audio_from_truth(a.audio_truth), Path(a.audio_truth).resolve().parent)
    frac = getattr(a, "audio_serving_frac", 0.0) or 0.0
    seed = getattr(a, "seed", 0)
    aud += [dict(r, id=r["id"] + SV.SERVING_SUFFIX, _serving=True) for r in aud
            if not r.get("_gate") and SV.use_serving_shape(r["id"], frac, seed)]
    if getattr(a, "limit", 0):
        vis, aud = vis[:a.limit], aud[:a.limit]
    ids = [r["id"] for r in vis]
    if len(set(ids)) != len(ids) or len({r["id"] for r in aud}) != len(aud):
        sys.exit("duplicate row ids across the vision (or audio) sources; ids key the answers")
    return vis, aud


def add_row_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--data", help="dir holding vision_rows.jsonl / audio_rows.jsonl and the media (the unpacked mmeval zip)")
    ap.add_argument("--vision-rows")
    ap.add_argument("--audio-rows")
    ap.add_argument("--vision-cases", help="the Mac gate's gate/vision/cases.json (images next to it under images/)")
    ap.add_argument("--audio-truth", help="the Mac gate's gate/audio/truth.json (clips next to it under clips/)")
    ap.add_argument("--split", default="dev", choices=["dev", "all"], help="vision rows to use (default dev)")
    ap.add_argument("--audio-serving-frac", type=float, default=SV.SERVING_FRAC,
                    help="share of audio dev clips ALSO run in oMLX's serving shape (ceil(ms/40) audio tokens), "
                         "picked by the same hash train.py's --audio-serving-frac uses (default %(default)s; 0 = none)")
    ap.add_argument("--seed", type=int, default=0, help="train.py's --seed (the serving-shape hash uses it)")
    ap.add_argument("--limit", type=int, default=0, help="first N rows per modality (smoke runs)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--train-log", help="train.log.jsonl of the run (text dev loss per checkpoint)")
    ap.add_argument("--ckpt-root", help="the run dir with step-*/best/final (to resolve best -> step)")
    ap.add_argument("--tol", action="append", metavar="CHECK=VALUE")
    add_row_args(ap)
    a = ap.parse_args()
    vis, aud = load_eval_rows(a)
    if not vis and not aud:
        sys.exit("no dev rows: pass --data, --vision-rows/--audio-rows or the gate's --vision-cases/--audio-truth")
    res = run(Path(a.out), vis, aud, a.train_log, parse_tol(a.tol), a.ckpt_root)
    print((Path(a.out) / "report.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
