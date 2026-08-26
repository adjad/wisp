#!/usr/bin/env python3
"""Human-graded evaluation of the resident model — terminal front end.

    python scripts/eval_ling.py                    # full set, read-only
    python scripts/eval_ling.py --quick            # ~16-prompt slice
    python scripts/eval_ling.py --only news,weather
    python scripts/eval_ling.py --ids stock_history,weather_noloc
    python scripts/eval_ling.py --resume           # continue the last run
    python scripts/eval_ling.py --report           # re-print the report only
    python scripts/eval_ling.py --dry-run          # list prompts, call nothing

For the browser version of the same thing — a board of every prompt, results
streaming in as you run them, grading in place — run `scripts/eval_server.py`.
Both share ~/.moe/evals/ling_eval.jsonl, so grades from either show up in both.

Prompts live in eval_ling_tasks.py; the run/safety/signal machinery is in
eval_core.py. See those for what each prompt is hunting and why nothing that
writes or sends can execute without --write.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(line_buffering=True)

import httpx  # noqa: E402

from scripts import eval_core as core  # noqa: E402
from scripts.eval_ling_tasks import CATEGORIES, Task, select  # noqa: E402


class C:
    dim = "\033[2m"; bold = "\033[1m"; off = "\033[0m"
    red = "\033[31m"; grn = "\033[32m"; yel = "\033[33m"
    blu = "\033[36m"; mag = "\033[35m"

    @classmethod
    def strip(cls):
        for k in ("dim", "bold", "off", "red", "grn", "yel", "blu", "mag"):
            setattr(cls, k, "")


def rule(ch: str = "─", n: int = 78) -> str:
    return C.dim + ch * n + C.off


_SIG_COLOR = {"bad": lambda: C.red, "warn": lambda: C.yel, "info": lambda: C.blu}


def show_card(idx: int, total: int, task: Task, turn: core.Turn,
              label: str = "") -> None:
    print()
    print(rule("━"))
    head = f"{C.bold}[{idx}/{total}] {task.id}{C.off}  {C.dim}{task.category}{C.off}"
    if label:
        head += f"  {C.mag}{label}{C.off}"
    if task.mode == "plan":
        head += f"  {C.mag}(plan only — nothing executed){C.off}"
    print(head)
    print(rule())
    print(f"{C.blu}PROMPT{C.off}  {turn.prompt}")
    if task.trap:
        print(f"{C.dim}TRAP    {task.trap}{C.off}")
    if turn.route:
        r = turn.route
        print(f"{C.dim}ROUTE   role={r.get('role')} tools={r.get('needs_tools')} "
              f"({r.get('source', '?')}: {r.get('reason', '')}){C.off}")
    for t in turn.tools:
        import json
        args = json.dumps(t["args"], ensure_ascii=False)
        if len(args) > 160:
            args = args[:160] + "…"
        print(f"{C.grn}TOOL{C.off}    {t['name']}({args})")
        if t["result"]:
            res = " ".join(str(t["result"]).split())
            print(f"{C.dim}        ↳ {res[:220]}{'…' if len(res) > 220 else ''}{C.off}")

    ftt = f"{turn.first_token_s:.1f}s" if turn.first_token_s is not None else "—"
    print(f"{C.dim}TIMING  first token {ftt} · total {turn.elapsed_s:.1f}s{C.off}")
    print(rule("╌"))
    print(turn.answer.strip() or f"{C.red}(no answer){C.off}")
    print(rule("╌"))

    sig = core.signals(turn, task.expect)
    if sig:
        print(f"{C.bold}SIGNALS{C.off}")
        for s in sig:
            col = _SIG_COLOR[s["level"]]()
            print(f"  • {col}{s['text']}{C.off}")
    focus = task.followup_grade_for if label else task.grade_for
    print(f"{C.bold}GRADE FOR{C.off}  {focus}")


_FLAG_KEYS = {f[0]: f for f in core.FLAG_NAMES}
# Two flags start with the same letter; give the collisions distinct keys.
_FLAG_KEYS.update({"h": "hallucination", "t": "wrong-tool", "a": "attribution",
                   "f": "format-ignored", "v": "verbose", "k": "thinking-leak",
                   "l": "slow", "e": "error", "u": "unhelpful",
                   "r": "refused-wrongly"})


def ask_grade() -> dict | None:
    """Collect one human judgment. None means 'redo this prompt'."""
    print(f"\n{C.bold}  1{C.off} broken   {C.bold}2{C.off} poor   "
          f"{C.bold}3{C.off} good   {C.bold}4{C.off} excellent"
          f"     {C.dim}s=skip  x=redo  q=save+quit  ?=flags{C.off}")
    while True:
        raw = input("  grade> ").strip().lower()
        if raw == "?":
            print("  " + "  ".join(f"{C.bold}{k}{C.off}={v}"
                                   for k, v in _FLAG_KEYS.items()))
            continue
        if raw == "q":
            raise KeyboardInterrupt
        if raw == "x":
            return None
        if raw == "s":
            return {"grade": None, "flags": [], "note": "", "skipped": True}
        if raw in ("1", "2", "3", "4"):
            flags: list[str] = []
            if raw in ("1", "2"):
                print(f"  {C.dim}flags ({'/'.join(_FLAG_KEYS)}) — comma-separated, "
                      f"blank for none{C.off}")
                fl = input("  flags> ").strip().lower()
                flags = [_FLAG_KEYS[c.strip()] for c in fl.split(",")
                         if c.strip() in _FLAG_KEYS]
            note = input("  note (optional)> ").strip()
            return {"grade": int(raw), "flags": flags, "note": note, "skipped": False}
        print(f"  {C.red}enter 1-4, or s/x/q/?{C.off}")


def print_report(records: dict[str, dict], model: str) -> None:
    s = core.summarize(records)
    print()
    print(rule("━"))
    print(f"{C.bold}EVALUATION REPORT{C.off}  —  {model}")
    print(rule("━"))
    if not s["n"]:
        print("Nothing graded yet.")
        return

    print(f"\n{C.bold}Overall{C.off}  {s['mean']}/4 across {s['n']} prompts")
    for g in (4, 3, 2, 1):
        n = s["dist"].get(g, 0)
        col = C.grn if g >= 3 else C.red
        print(f"  {g} {core.GRADE_NAMES[g]:<10} {col}{'█' * n}{C.off} {n}")

    print(f"\n{C.bold}By category{C.off}")
    for cat, m in s["by_category"].items():
        col = C.grn if m >= 3 else (C.yel if m >= 2.5 else C.red)
        print(f"  {cat:<10} {col}{m:.2f}{C.off}  (n={s['category_n'][cat]})")

    if s["flags"]:
        print(f"\n{C.bold}Failure modes{C.off}")
        for f, n in s["flags"].items():
            print(f"  {n:>2}×  {f}")

    if s["latency"]:
        print(f"\n{C.bold}Latency{C.off}  median {s['latency']['median']:.1f}s · "
              f"max {s['latency']['max']:.1f}s")

    if s["needs_work"]:
        print(f"\n{C.bold}Needs work{C.off}")
        for r in s["needs_work"]:
            note = f" — {r['note']}" if r["note"] else ""
            fl = f" [{', '.join(r['flags'])}]" if r["flags"] else ""
            print(f"  {C.red}{r['grade']}{C.off} {r['id']:<22}{fl}{note}")
    print()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help=f"categories: {','.join(CATEGORIES)}")
    ap.add_argument("--ids", default="", help="explicit task ids, comma-separated")
    ap.add_argument("--quick", action="store_true", help="~16-prompt slice")
    ap.add_argument("--resume", action="store_true", help="skip already-graded")
    ap.add_argument("--report", action="store_true", help="print report and exit")
    ap.add_argument("--dry-run", action="store_true", help="list prompts, call nothing")
    ap.add_argument("--write", action="store_true",
                    help="run plan-mode tasks FOR REAL (sends/writes actually happen)")
    ap.add_argument("--log", default=str(core.DEFAULT_LOG))
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        C.strip()

    log_path = Path(args.log)
    records = core.load_log(log_path)
    tasks = select(only=args.only, quick=args.quick, ids=args.ids)

    if args.report:
        print_report(records, "logged run")
        return 0
    if not tasks:
        print("No tasks matched.")
        return 1

    if args.dry_run:
        print(f"{C.bold}{len(tasks)} prompts{C.off}\n")
        for i, t in enumerate(tasks, 1):
            done = " (graded)" if records.get(t.id, {}).get("grade") else ""
            mode = f" {C.mag}[plan]{C.off}" if t.mode == "plan" else ""
            print(f"{i:>2}. {C.bold}{t.id}{C.off} {C.dim}({t.category}){C.off}{mode}{done}")
            print(f"    {t.prompt}")
            if t.followup:
                print(f"    {C.dim}↳ then: {t.followup}{C.off}")
        return 0

    async with httpx.AsyncClient() as client:
        try:
            h = (await client.get(f"{core.BASE}/health", timeout=10)).json()
        except Exception as exc:                               # noqa: BLE001
            print(f"{C.red}Wisp is not reachable on {core.BASE}: {exc}{C.off}")
            print("Launch Wisp (or scripts/run.sh) and try again.")
            return 1
        model = h.get("default_model", "?")

        print(rule("━"))
        print(f"{C.bold}Wisp evaluation{C.off} — model {C.bold}{model}{C.off}")
        print(f"{len(tasks)} prompts · log {log_path}")
        print(f"{C.red}--write: plan tasks will EXECUTE. Sends and writes are real.{C.off}"
              if args.write else
              f"{C.dim}read-only: write/send tasks run in plan mode{C.off}")
        print(rule("━"))

        queue = [t for t in tasks
                 if not (args.resume and records.get(t.id, {}).get("grade"))]
        if args.resume and len(queue) < len(tasks):
            print(f"{C.dim}resuming — {len(tasks) - len(queue)} already graded{C.off}")

        try:
            for i, task in enumerate(queue, 1):
                while True:                                     # redo loop
                    turn = await core.run_turn(client, task, task.prompt,
                                               None, args.write)
                    show_card(i, len(queue), task, turn)
                    verdict = ask_grade()
                    if verdict is not None:
                        break

                rec = {"id": task.id, "category": task.category, "mode": task.mode,
                       "model": model, "prompt": task.prompt, "answer": turn.answer,
                       "tools": turn.tools, "route": turn.route,
                       "elapsed_s": round(turn.elapsed_s, 1), "error": turn.error,
                       **verdict}
                core.append_log(rec, log_path)
                records[task.id] = rec

                if task.followup and not verdict.get("skipped"):
                    while True:
                        f_turn = await core.run_turn(client, task, task.followup,
                                                     turn.session_id, args.write)
                        show_card(i, len(queue), task, f_turn, label="follow-up")
                        f_verdict = ask_grade()
                        if f_verdict is not None:
                            break
                    f_rec = {"id": task.id + "::followup", "category": task.category,
                             "mode": task.mode, "model": model,
                             "prompt": task.followup, "answer": f_turn.answer,
                             "tools": f_turn.tools, "route": f_turn.route,
                             "elapsed_s": round(f_turn.elapsed_s, 1),
                             "error": f_turn.error, **f_verdict}
                    core.append_log(f_rec, log_path)
                    records[f_rec["id"]] = f_rec

        except KeyboardInterrupt:
            print(f"\n{C.yel}Stopped. Everything graded so far is saved.{C.off}")
            print("Resume with: python scripts/eval_ling.py --resume")

    print_report(records, model)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
