#!/usr/bin/env python3
"""Build the Mellum R1 tool-calling dataset (plan: docs/plans/2026-10-14-mellum-finetune.md).

    python build.py pool                 # draw items in seeded worlds; hold-out filter; train/dev split
    python build.py gold                 # corrective rows: replay gold plans through the real loop
    python build.py distill [--k 2]      # Mellum samples (thinking off on narration), keep the best pass
    python build.py render               # prompt/completion text with Mellum's own tokenizer + template
    python build.py validate             # schema, offered tools, privacy, hold-out, template consistency
    python build.py stats                # counts and token lengths -> stats.json + DATA_CARD.md
    python build.py all --k 2            # everything above, in order

Run from the repository root's venv (`../MOE_Project/.venv/bin/python` in a
worktree). `distill` needs oMLX with Mellum loaded and Wisp QUIT; every other
step is offline. Nothing here executes a real Wisp tool: rows come from
toolbench's fake world, the network is limited to the oMLX port, and
subprocesses to read-only probes (scripts/toolbench/runner.py).

Every row's context is captured from the real router + agent loop, so it is
exactly what Wisp sends at that step. Its target is either a passing Mellum
step (distill) or a gold step (gold), and only the target is trained on.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(line_buffering=True)

MODEL = "Mellum2.1-12B-A2.5B-Thinking-oQ5e"
MELLUM_DIR = Path.home() / "Desktop/OMLX_Model_Files/niteg/Mellum2.1-12B-A2.5B-Thinking-oQ5e"
SEED0, N_WORLDS = 1000, 60          # training worlds; the gate uses the canonical world and seeds 0..49
DEV_EVERY = 10                      # every 10th world is dev
MAX_DUP = 4                         # identical (prompt, history) text kept at most this many times
OVERLAP_MAX = 0.5                   # reject an item sharing >= this share of its 4-grams with one gate prompt
THINK_STEP_MAX, THINK_TOTAL_MAX = 1500, 2500   # distill filter, reasoning characters
PRIVATE = re.compile(r"adijain|trishala|/Users/(?!test\b)[A-Za-z]|@gmail\.com", re.I)  # Wisp prompts say /Users/<name>

OUT = {k: HERE / f"{k}.jsonl" for k in ("pool", "gold_rows", "distill_rows", "distill_log",
                                         "train", "dev", "rejects")}


def _w(path: Path, rows) -> int:
    n = 0
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def _r(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


# ---------------------------------------------------------------- worlds and clock

def world_clock(seed: int) -> datetime:
    """A per-world 'now': a random 2026 date, between 08:00 and 13:59 (so 'tonight' is ahead)."""
    rng = random.Random(seed * 7919)
    d = date(2026, 1, 1) + timedelta(days=rng.randrange(365))
    return datetime(d.year, d.month, d.day, rng.randrange(8, 14), rng.choice([0, 15, 30, 45]))


# --------------------------------------------------------------------------- pool

_TOK = re.compile(r"[a-z0-9']+")


def _toks(s: str) -> list[str]:
    return _TOK.findall(s.lower())


def _grams(t: list[str], n: int = 4) -> set[tuple]:
    return {tuple(t[i:i + n]) for i in range(len(t) - n + 1)} if len(t) >= n else {tuple(t)}


def _fill(v, m):
    from scripts.toolbench.cases import _fill as f
    return f(f(v, m), m)  # slot values may themselves hold fact placeholders


def cmd_pool(args) -> None:
    from families import FAMILIES
    from scripts.toolbench.cases import load_cases, parse_case
    from scripts.toolbench.world import make_facts

    gate = [c for c in load_cases() if c.split == "gate"]
    gate_grams: dict[int | None, list[set]] = {}

    def gate_for(seed):
        if seed not in gate_grams:
            facts = make_facts(seed)
            texts = [c.bind(facts).prompt for c in gate] + \
                    [t["user"] for c in gate for t in c.bind(facts).history if t.get("user")]
            gate_grams[seed] = [(_grams(_toks(x)), tuple(_toks(x))) for x in texts]
        return gate_grams[seed]

    items, rejects, dup = [], [], Counter()
    for wi in range(N_WORLDS):
        seed = SEED0 + wi
        facts = make_facts(seed)
        rng = random.Random(seed)
        split = "dev" if wi % DEV_EVERY == 0 else "train"
        for fam in FAMILIES:
            for k in range(args.per_world):
                m = dict(facts)
                for slot, values in fam.slots.items():
                    m.update({key: _fill(val, facts) for key, val in rng.choice(values).items()})
                prompt = _fill(rng.choice(fam.prompts), m)
                m["_prompt"] = prompt
                history = [_fill(dict(h), m) for h in fam.history]
                key = (prompt, json.dumps(history, sort_keys=True))
                if dup[key] >= MAX_DUP:
                    continue
                pt = _toks(prompt)
                pg = _grams(pt)
                worst = 0.0
                for seed_g in (None, seed):
                    for g, full in gate_for(seed_g):
                        if full == tuple(pt):
                            worst = 1.0
                        elif pg:
                            worst = max(worst, len(pg & g) / len(pg))
                if worst >= OVERLAP_MAX:
                    rejects.append({"family": fam.id, "prompt": prompt, "overlap": round(worst, 2)})
                    continue
                dup[key] += 1
                raw = {"id": f"{fam.id}-{seed}-{k}", "prompt": prompt, "history": history,
                       "expect": _fill(fam.expect, m), "forbid": fam.forbid, "answer": _fill(fam.answer, m),
                       "no_tool": fam.no_tool, "max_calls": fam.max_calls}
                parse_case(raw, fam.category)  # validates the expectation shape
                items.append({**raw, "family": fam.id, "category": fam.category, "seed": seed,
                              "split": split, "mode": fam.mode, "has_gold": fam.gold is not None,
                              "overlap": round(worst, 2), "slots": {k2: v for k2, v in m.items()
                                                                     if k2 not in facts}})
    n = _w(OUT["pool"], items)
    _w(OUT["rejects"], rejects)
    by = Counter((i["split"], i["mode"]) for i in items)
    print(f"pool: {n} items ({dict(by)}), {len(rejects)} rejected for gate overlap -> {OUT['pool'].name}")


# ---------------------------------------------------------------------- running

def _setup():
    from scripts.toolbench import runner
    runner.isolate(Path(tempfile.mkdtemp(prefix="mellum-r1-")))
    return runner


def _case(item):
    from scripts.toolbench.cases import parse_case
    raw = {k: item[k] for k in ("id", "prompt", "history", "expect", "forbid", "answer", "no_tool", "max_calls")}
    return parse_case(raw, item["category"])


def _tool_errors(traj) -> list[str]:
    """Tool results that are loop errors (bad arguments, unknown tool). A row
    built on one would train a recovery from a mistake the target never made."""
    seen = []
    for st in traj["steps"]:
        for m in (st.get("request") or {}).get("messages") or ():
            c = m.get("content") or ""
            if m.get("role") == "tool" and c.startswith("(error") and c not in seen:
                seen.append(c)
    return seen


def _rows(item, traj, source) -> list[dict]:
    """One row per model step whose target is non-empty."""
    out = []
    for i, st in enumerate(traj["steps"]):
        req = st.get("request")
        if not req:
            continue
        content = st.get("content") or ""
        target = {"role": "assistant", "content": content if content.strip() else "",
                  "reasoning_content": st.get("reasoning") or "",
                  "tool_calls": [{"type": "function", "function": {"name": c["name"],
                                                                    "arguments": c.get("raw") or json.dumps(c["args"])}}
                                 for c in st.get("calls") or ()] or None}
        if not target["content"].strip() and not target["tool_calls"]:
            continue
        out.append({"id": f"{item['id']}-s{i}", "item": item["id"], "family": item["family"],
                    "category": item["category"], "split": item["split"], "source": source, "step": i,
                    "enable_thinking": req["enable_thinking"], "tools": req["tools"],
                    "messages": req["messages"], "target": target})
    return out


def _keep_reasoning(client_cls):
    """Make the bench client keep reasoning text and raw argument strings on each step."""
    orig = client_cls._finish

    def finish(step, message):
        orig(step, message)
        step["reasoning"] = message.get("reasoning_content") or ""
        raws = [(tc.get("function") or {}).get("arguments") for tc in message.get("tool_calls") or ()]
        for call, raw in zip(step["calls"], raws):
            call["raw"] = raw if isinstance(raw, str) else json.dumps(raw)
    client_cls._finish = staticmethod(finish)
    return client_cls


async def _run_gold(runner, items, out_rows, log) -> Counter:
    from families import FAMILIES
    from scripts.toolbench.scorer import score
    from scripts.toolbench.world import build_world
    fams = {f.id: f for f in FAMILIES}
    Scripted = _keep_reasoning(runner.scripted_client_class())
    client = Scripted(timeout=30)
    client.capture = True
    runner.set_thinking(client, MODEL, "narration-off")
    outcome = Counter()
    for item in items:
        fam = fams[item["family"]]
        now = world_clock(item["seed"])
        runner.set_clock(now)
        world = build_world(today=now.date(), seed=item["seed"])
        names = runner.install_fakes(world)
        m = {**world.facts, **item["slots"]}
        client.plan, client.failure = fam.gold(m, now.date()), ""
        case = _case(item)
        traj = await runner.run_case(client, MODEL, case, world, timeout=60)
        v = score(case, traj, registry_names=names, today=world.today)
        why = client.failure or ("" if v.passed else ",".join(v.labels))
        if not why and _tool_errors(traj):
            why = "tool_error: " + _tool_errors(traj)[0][:80]
        if traj.get("escapes"):
            why = why or f"escape {traj['escapes']}"
        if why:
            outcome["dropped:" + why.split(":")[0][:40]] += 1
            log.append({"item": item["id"], "gold": "dropped", "why": why, "route": traj.get("route")})
            continue
        rows = _rows(item, traj, "gold" if item["mode"] == "gold" else "gold-fallback")
        out_rows.extend(rows)
        outcome["kept"] += 1
    runner.set_clock(None)
    return outcome


def cmd_gold(args) -> None:
    runner = _setup()
    items = [i for i in _r(OUT["pool"]) if i["mode"] == "gold" and i["has_gold"]]
    rows, log = [], []
    outcome = asyncio.run(_run_gold(runner, items, rows, log))
    _w(OUT["gold_rows"], rows)
    _w(HERE / "gold_log.jsonl", log)
    print(f"gold: {len(items)} items -> {outcome['kept']} kept, {len(rows)} rows; {dict(outcome)}")


def cmd_distill(args) -> None:
    runner = _setup()
    from scripts.toolbench.scorer import score
    from scripts.toolbench.world import build_world

    items = [i for i in _r(OUT["pool"]) if i["mode"] == "distill"]
    if args.limit:
        items = items[: args.limit]
    done = {r["item"] for r in _r(OUT["distill_log"])}
    todo = [i for i in items if i["id"] not in done]
    print(f"distill: {len(items)} items, {len(done & {i['id'] for i in items})} already done, {len(todo)} to run, k={args.k}")

    async def go():
        Bench = _keep_reasoning(runner.bench_client_class())
        client = Bench(timeout=args.timeout)
        client.capture = True
        await client.make_resident(MODEL)
        runner.set_thinking(client, MODEL, "narration-off")
        fallback = []
        with OUT["distill_log"].open("a") as logf, OUT["distill_rows"].open("a") as rowf:
            for n, item in enumerate(todo, 1):
                now = world_clock(item["seed"])
                runner.set_clock(now)
                world = build_world(today=now.date(), seed=item["seed"])
                names = runner.install_fakes(world)
                case = _case(item)
                best, tries = None, []
                for j in range(args.k):
                    # Try 0 is production sampling (the loop's greedy tool steps), so a
                    # kept row is what Mellum already does in Wisp. Later tries only run
                    # when it failed, and sample tool steps at the profile temperature.
                    if j and best:
                        break
                    client.seed = int(hashlib.sha1(f"{item['id']}:{j}".encode()).hexdigest()[:8], 16)
                    client.temperature = None if j == 0 else args.retry_temperature
                    traj = await runner.run_case(client, MODEL, case, world, timeout=args.timeout)
                    v = score(case, traj, registry_names=names, today=world.today)
                    think = [len(s.get("reasoning") or "") for s in traj["steps"]]
                    thought = all((s.get("reasoning") or "").strip() for s in traj["steps"]
                                  if (s.get("request") or {}).get("enable_thinking"))
                    ok = (v.passed and not traj.get("escapes") and not v.labels and thought
                          and not _tool_errors(traj)
                          and max(think or [0]) <= THINK_STEP_MAX and sum(think) <= THINK_TOTAL_MAX)
                    tries.append({"passed": v.passed, "labels": v.labels, "think": sum(think), "ok": ok,
                                  "tool_error": bool(_tool_errors(traj)), "thought": thought,
                                  "seconds": v.seconds})
                    if ok and (best is None or sum(think) < best[1]):
                        best = (traj, sum(think))
                if best:
                    rows = _rows(item, best[0], "distill")
                    for r in rows:
                        rowf.write(json.dumps(r, ensure_ascii=False) + "\n")
                    status = f"kept {len(rows)} rows"
                else:
                    status = "no pass"
                    if item["has_gold"]:
                        fallback.append(item)
                logf.write(json.dumps({"item": item["id"], "family": item["family"], "status": status,
                                       "tries": tries}) + "\n")
                logf.flush()
                rowf.flush()
                print(f"[{n}/{len(todo)}] {item['id']:<34} {status}  "
                      f"{[('P' if t['passed'] else 'F') + str(t['think']) for t in tries]}")
        runner.set_clock(None)
        # Fallback covers every unpassed item in the log, including ones from an
        # earlier interrupted run, minus any that already have fallback rows.
        have = {r["item"] for r in _r(OUT["distill_rows"])}
        unpassed = {r["item"] for r in _r(OUT["distill_log"]) if r["status"] == "no pass"}
        fallback = [i for i in items if i["id"] in unpassed - have and i["has_gold"]]
        if fallback:
            rows, log = [], []
            outcome = await _run_gold(runner, fallback, rows, log)
            with OUT["distill_rows"].open("a") as rowf:
                for r in rows:
                    rowf.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"gold fallback for {len(fallback)} unpassed items: {dict(outcome)}")
    asyncio.run(go())


# ------------------------------------------------------------------------ render

def _tokenizer():
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(str(MELLUM_DIR))


def completion_text(target: dict, enable_thinking: bool) -> str:
    """The assistant turn exactly as Mellum's chat template renders it, after the generation prompt."""
    out = ""
    if enable_thinking:
        out += "<think>\n" + (target.get("reasoning_content") or "").strip("\n") + "\n</think>\n\n"
    content = (target.get("content") or "").lstrip("\n")
    out += content
    for i, tc in enumerate(target.get("tool_calls") or ()):
        fn = tc.get("function") or tc
        args = fn["arguments"] if isinstance(fn["arguments"], str) else json.dumps(fn["arguments"])
        if (i == 0 and content) or i > 0:
            out += "\n"
        out += '<tool_call>\n{"name": "' + fn["name"] + '", "arguments": ' + args + "}\n</tool_call>"
    return out + "<|im_end|>"


def cmd_render(args) -> None:
    tok = _tokenizer()
    rows = _r(OUT["gold_rows"]) + _r(OUT["distill_rows"])
    by_split = defaultdict(list)
    for r in rows:
        prompt = tok.apply_chat_template(r["messages"], tools=r["tools"] or None, add_generation_prompt=True,
                                         enable_thinking=r["enable_thinking"], tokenize=False)
        completion = completion_text(r["target"], r["enable_thinking"])
        r2 = {k: r[k] for k in ("id", "item", "family", "category", "source", "step", "enable_thinking")}
        r2.update(prompt=prompt, completion=completion,
                  prompt_tokens=len(tok(prompt, add_special_tokens=False).input_ids),
                  completion_tokens=len(tok(completion, add_special_tokens=False).input_ids))
        by_split[r["split"]].append(r2)
    for split in ("train", "dev"):
        print(f"render: {split} {_w(OUT[split], by_split[split])} rows -> {OUT[split].name}")


# ---------------------------------------------------------------------- validate

def cmd_validate(args) -> None:
    rows = _r(OUT["gold_rows"]) + _r(OUT["distill_rows"])
    pool = {i["id"]: i for i in _r(OUT["pool"])}
    problems = Counter()
    examples = defaultdict(list)

    def bad(kind, rid, detail=""):
        problems[kind] += 1
        if len(examples[kind]) < 3:
            examples[kind].append(f"{rid}: {detail}"[:200])

    seen = set()
    for r in rows:
        if r["id"] in seen:
            bad("duplicate_id", r["id"])
        seen.add(r["id"])
        t = r["target"]
        if t["role"] != "assistant" or not (t["content"].strip() or t["tool_calls"]):
            bad("empty_target", r["id"])
        offered = {s["function"]["name"] for s in r["tools"]}
        for tc in t["tool_calls"] or ():
            if tc["function"]["name"] not in offered:
                bad("call_not_offered", r["id"], tc["function"]["name"])
            try:
                json.loads(tc["function"]["arguments"])
            except ValueError:
                bad("bad_arguments_json", r["id"])
        if not r["enable_thinking"] and t["reasoning_content"]:
            bad("reasoning_on_no_think_step", r["id"])
        if r["enable_thinking"] and not t["reasoning_content"].strip():
            bad("empty_reasoning_on_think_step", r["id"])
        if r["enable_thinking"] and len(t["reasoning_content"]) > THINK_STEP_MAX:
            bad("reasoning_too_long", r["id"], str(len(t["reasoning_content"])))
        blob = json.dumps(r["messages"]) + json.dumps(t)
        if PRIVATE.search(blob):
            bad("private_data", r["id"], PRIVATE.search(blob).group(0))
        for leak in ("<think", "</think>", "<tool_call>"):
            if leak in (t["content"] or ""):
                bad("markup_in_content", r["id"], leak)
        item = pool.get(r["item"])
        if item is None:
            bad("orphan_row", r["id"])
        elif item["overlap"] >= OVERLAP_MAX:
            bad("gate_overlap", r["id"], item["prompt"])
        errs = [m for m in r["messages"] if m["role"] == "tool" and (m.get("content") or "").startswith("(error")]
        if errs:
            bad("tool_error_in_context", r["id"], errs[0]["content"])
        if r["messages"][-1]["role"] not in ("user", "tool"):
            bad("context_not_ending_in_user_or_tool", r["id"], r["messages"][-1]["role"])
    # Template consistency: for thinking steps, rendering context+target as one
    # conversation must equal generation prompt + completion. This catches a
    # completion format that drifts from Mellum's real template.
    checked = 0
    if (OUT["train"].exists() or OUT["dev"].exists()) and not args.no_template:
        tok = _tokenizer()
        rendered = {x["id"]: x for x in _r(OUT["train"]) + _r(OUT["dev"])}
        for r in rows:
            x = rendered.get(r["id"])
            if x is None:
                bad("not_rendered", r["id"])
                continue
            if not r["enable_thinking"] or checked >= args.template_checks:
                continue
            full = tok.apply_chat_template(r["messages"] + [r["target"]], tools=r["tools"] or None,
                                           tokenize=False, enable_thinking=True)
            checked += 1
            if full.rstrip("\n") != (x["prompt"] + x["completion"]).rstrip("\n"):
                bad("template_mismatch", r["id"])
    report = {"rows": len(rows), "template_checks": checked, "problems": dict(problems),
              "examples": dict(examples), "ok": not problems}
    (HERE / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if problems:
        raise SystemExit(1)


# ------------------------------------------------------------------------- stats

def cmd_stats(args) -> None:
    pool = _r(OUT["pool"])
    rows = _r(OUT["gold_rows"]) + _r(OUT["distill_rows"])
    rendered = _r(OUT["train"]) + _r(OUT["dev"])
    tok_by_id = {x["id"]: x for x in rendered}
    split = Counter(r["split"] for r in rows)
    source = Counter(r["source"] for r in rows)
    items_kept = len({r["item"] for r in rows})

    def q(vals, p):
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(p * len(vals)))] if vals else 0

    pt = [x["prompt_tokens"] for x in rendered]
    ct = [x["completion_tokens"] for x in rendered]
    stats = {
        "built": datetime.now().isoformat(timespec="seconds"),
        "pool_items": len(pool), "items_with_rows": items_kept, "rows": len(rows),
        "rows_by_split": dict(split), "rows_by_source": dict(source),
        "rows_by_category": dict(Counter(r["category"] for r in rows)),
        "rows_by_family": dict(sorted(Counter(r["family"] for r in rows).items())),
        "thinking_steps": sum(r["enable_thinking"] for r in rows),
        "tool_call_targets": sum(bool(r["target"]["tool_calls"]) for r in rows),
        "prompt_tokens": {"max": max(pt, default=0), "p95": q(pt, .95), "median": q(pt, .5)},
        "completion_tokens": {"max": max(ct, default=0), "p95": q(ct, .95), "median": q(ct, .5),
                              "total_train": sum(x["completion_tokens"] for x in _r(OUT["train"]))},
        "tokens_per_epoch_train": sum(x["prompt_tokens"] + x["completion_tokens"] for x in _r(OUT["train"])),
        "worlds": {"train": [SEED0 + i for i in range(N_WORLDS) if i % DEV_EVERY],
                   "dev": [SEED0 + i for i in range(N_WORLDS) if not i % DEV_EVERY]},
        "gate_rejects": len(_r(OUT["rejects"])),
    }
    (HERE / "stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    fam_lines = "\n".join(f"| {k} | {v} |" for k, v in stats["rows_by_family"].items())
    card = f"""# Mellum R1 dataset — data card (generated by `build.py stats`)

Built {stats['built']}. Plan: `docs/plans/2026-10-14-mellum-finetune.md`.

| | |
|---|---|
| pool items | {stats['pool_items']} ({stats['gate_rejects']} more rejected for gate overlap) |
| items with rows | {stats['items_with_rows']} |
| rows (one per assistant step) | {stats['rows']} — train {split.get('train', 0)}, dev {split.get('dev', 0)} |
| by source | {', '.join(f'{k} {v}' for k, v in source.items())} |
| tool-call targets / thinking steps | {stats['tool_call_targets']} / {stats['thinking_steps']} |
| prompt tokens (Mellum tokenizer) | max {stats['prompt_tokens']['max']}, p95 {stats['prompt_tokens']['p95']}, median {stats['prompt_tokens']['median']} |
| completion tokens | max {stats['completion_tokens']['max']}, p95 {stats['completion_tokens']['p95']}, median {stats['completion_tokens']['median']}; train total {stats['completion_tokens']['total_train']} |
| tokens per train epoch | {stats['tokens_per_epoch_train']} |

## Rows by family

| family | rows |
|---|---|
{fam_lines}
"""
    (HERE / "DATA_CARD.md").write_text(card)
    print(json.dumps({k: stats[k] for k in ("pool_items", "items_with_rows", "rows", "rows_by_split",
                                             "rows_by_source", "prompt_tokens", "completion_tokens")}, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["pool", "gold", "distill", "render", "validate", "stats", "all"])
    ap.add_argument("--per-world", type=int, default=1, help="items per family per world (pool)")
    ap.add_argument("--k", type=int, default=2, help="max Mellum tries per distill item (later tries only on failure)")
    ap.add_argument("--retry-temperature", type=float, default=0.6, help="tool-step temperature for retries")
    ap.add_argument("--limit", type=int, default=0, help="distill only the first N items (pilot)")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--template-checks", type=int, default=200, help="validate: rows to re-render and compare")
    ap.add_argument("--no-template", action="store_true", help="validate: skip tokenizer checks")
    args = ap.parse_args()
    steps = ["pool", "gold", "distill", "render", "validate", "stats"] if args.cmd == "all" else [args.cmd]
    for s in steps:
        globals()[f"cmd_{s}"](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
