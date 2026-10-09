#!/usr/bin/env python3
"""Label a sample of your own messages and score attention predictors against it.

  sample   choose what to label from a snapshot (deterministic; run once)
  label    answer one question per item; resumable, relabel any time
  status   how many are labelled
  report   precision/recall of a predictor against your labels (counts and ids only)

State lives under ~/.moe/attention/ (0700/0600). Nothing here sends, writes to a
source, or calls a model. Text is shown on your terminal only.

The one question every label answers: "if Wisp had seen this when it arrived,
should it have interrupted me?" Interrupt means: it states something coming up
soon that is in neither Calendar nor Reminders.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.attention import corpus, evaluate, labels  # noqa: E402
from service.paths import MOE_DIR  # noqa: E402

BASE = MOE_DIR / "attention"

HELP = """\
  m  MISSING  coming up soon, NOT on my calendar/reminders  (should alert)
  c  known    coming up soon, already on my calendar/reminders
  l  later    a commitment, but not soon (more than ~2 days out)
  n  none     nothing I need to act on or show up for
  p  promo    promotional / automated marketing
  s  scam     phishing or scam
  u  skip     unsure
  b  back     redo the previous item        q  quit (progress is saved)"""


def _snapshot(arg: str | None) -> corpus.Snapshot:
    return corpus.load_snapshot(Path(arg) if arg else corpus.latest_snapshot(MOE_DIR))


def _sample_path(snap: corpus.Snapshot) -> Path:
    return BASE / f"sample-{snap.snapshot_id}.json"


def _labels_path(snap: corpus.Snapshot) -> Path:
    return BASE / f"labels-{snap.snapshot_id}.jsonl"


def _load_sample(snap: corpus.Snapshot) -> list[dict]:
    path = _sample_path(snap)
    if not path.exists():
        raise SystemExit("No sample for this snapshot yet; run the `sample` command first.")
    data = json.loads(path.read_text())
    if data.get("snapshot_id") != snap.snapshot_id:
        raise SystemExit("Sample belongs to a different snapshot.")
    return data["items"]


def cmd_sample(args) -> int:
    snap = _snapshot(args.snapshot)
    path = _sample_path(snap)
    if path.exists() and not args.force:
        raise SystemExit(f"{path.name} exists. Labels are tied to it; pass --force to replace "
                         "it (existing labels for items no longer sampled are ignored).")
    items = corpus.sample(snap, seed=args.seed, n_cue=args.cue, n_control=args.control,
                          per_conversation=args.per_conversation, n_mail=args.mail)
    corpus.ensure_private_tree(MOE_DIR)
    path.write_text(json.dumps({"snapshot_id": snap.snapshot_id, "seed": args.seed,
                                "items": items}, indent=1))
    path.chmod(0o600)
    strata: dict[str, int] = {}
    for entry in items:
        strata[entry["stratum"]] = strata.get(entry["stratum"], 0) + 1
    print(f"sampled {len(items)} items from {snap.snapshot_id}: {strata}")
    return 0


def _when(ts: float) -> str:
    return time.strftime("%a %b %d %-I:%M%p", time.localtime(ts))


def _show(snap: corpus.Snapshot, item: corpus.Item, index: int, total: int) -> None:
    print("\n" + "─" * 72)
    where = "text" if item.source == "messages" else "email"
    print(f"[{index}/{total}] {where} · from {item.sender or '?'} · {_when(item.ts)}")
    for prev in snap.thread_before(item, 3):
        who = "me" if prev.direction == "outgoing" else (prev.sender or "them")
        print(textwrap.indent(textwrap.shorten(f"{who}: {prev.text}", 200), "   │ "))
    body = item.text if len(item.text) <= 700 else item.text[:700] + " …"
    print(textwrap.indent(textwrap.fill(body, 76), "  ▶ "))
    near = snap.commitments_after(item)
    if near:
        print("   on file within 48h of arrival:")
        for c in near:
            print(f"     · {_when(float(c['when_ts']))}  {textwrap.shorten(str(c.get('title')), 60)}"
                  f"  [{c.get('source')}]")
    else:
        print("   on file within 48h of arrival: nothing")


def cmd_label(args) -> int:
    snap = _snapshot(args.snapshot)
    sample = _load_sample(snap)
    store = labels.LabelStore(_labels_path(snap))
    by_id = snap.by_id()
    queue = [e for e in sample if e["id"] in by_id]
    done = store.current()
    pending = [e for e in queue if args.relabel or e["id"] not in done]
    print(f"{len(queue) - len(pending)} labelled, {len(pending)} to go. '?' for help.")
    history: list[int] = []
    i = 0
    while i < len(pending):
        entry = pending[i]
        _show(snap, by_id[entry["id"]], queue.index(entry) + 1, len(queue))
        try:
            key = input("  label [m c l n p s u / b q / ?]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nsaved; stopping.")
            return 0
        if key == "q":
            print("saved; stopping.")
            return 0
        if key == "?":
            print(HELP)
            continue
        if key == "b":
            if history:
                i = history.pop()
            continue
        label = labels.KEYS.get(key)
        if label is None:
            print("  (not a label; '?' for help)")
            continue
        note = ""
        if label in ("missing", "known", "later"):
            note = input("  when/where? (optional, enter to skip): ")
        store.append(entry["id"], label, note)
        history.append(i)
        i += 1
    print("\nAll items labelled. Run: scripts/replay_attention.py report")
    return 0


def cmd_status(args) -> int:
    snap = _snapshot(args.snapshot)
    sample = _load_sample(snap)
    done = labels.LabelStore(_labels_path(snap)).current()
    ids = {e["id"] for e in sample}
    counts: dict[str, int] = {}
    for item_id, rec in done.items():
        if item_id in ids:
            counts[rec["label"]] = counts.get(rec["label"], 0) + 1
    print(f"{sum(counts.values())}/{len(sample)} labelled: {counts}")
    return 0


def cmd_report(args) -> int:
    snap = _snapshot(args.snapshot)
    sample = _load_sample(snap)
    done = labels.LabelStore(_labels_path(snap)).current()
    predictor = evaluate.PREDICTORS[args.predictor]
    result = evaluate.evaluate(snap, sample, done, predictor)
    if not args.errors:
        result.pop("false_positive_ids"), result.pop("false_negative_ids")
    print(json.dumps({"predictor": args.predictor, "snapshot": snap.snapshot_id, **result},
                     indent=1, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", help="snapshot directory (default: the newest)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="choose items to label")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--cue", type=int, default=120, help="texts matching a time/place cue")
    s.add_argument("--control", type=int, default=60, help="texts without a cue (measures misses)")
    s.add_argument("--mail", type=int, default=30, help="email subjects matching a cue")
    s.add_argument("--per-conversation", type=int, default=30)
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_sample)

    l = sub.add_parser("label", help="label the sample interactively")
    l.add_argument("--relabel", action="store_true", help="revisit already-labelled items")
    l.set_defaults(fn=cmd_label)

    sub.add_parser("status", help="labelling progress").set_defaults(fn=cmd_status)

    r = sub.add_parser("report", help="score a predictor against your labels")
    r.add_argument("--predictor", choices=sorted(evaluate.PREDICTORS), default="cue_baseline")
    r.add_argument("--errors", action="store_true", help="include false-positive/negative item ids")
    r.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
