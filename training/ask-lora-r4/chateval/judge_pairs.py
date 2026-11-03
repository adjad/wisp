#!/usr/bin/env python3
"""Blind A/B pairs for the r4 chat eval, the mechanical conversation checks, and the gate scorer.

    judge_pairs.py build --replies RUN/replies.jsonl [--base A --cand B] --out RUN/judge
    judge_pairs.py mech  --replies RUN/replies.jsonl [--base A --cand B] [--json-out F]
    judge_pairs.py score --replies RUN/replies.jsonl --key RUN/judge/pairs_key.json \
                         --judgments 'RUN/judge/judgments_*.jsonl' [--base A --cand B] --out RUN/judge/score

build   One pair per (unit, sample) present for both models. The judge sees the WHOLE conversation of each side
        (the same user messages, each model's own replies), order randomised with a recorded key, exactly half of
        the pairs swapped. Writes pairs_for_judge.jsonl (blind), pairs_key.json (the key), chunk_NN.jsonl (one
        ready-to-send judge prompt per pair) and judge_instructions.md.
mech    The per-conversation mechanical checks (recall details present, standing instructions obeyed on every
        turn, casual facts right, no invented context) plus non-replies, leaked internals and repeats, per model.
score   Applies the PLAN.md chat bars to the judgments and the replies. Exit 0 = every hard bar holds,
        1 = a bar failed, 3 = the judgments do not cover every pair (no verdict).

--base defaults to the first model in the replies file (gate.sh runs the untuned model A first), --cand to the second.
"""
from __future__ import annotations

import argparse
import glob
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chatlib as cl  # noqa: E402

FLAGS = ("not_a_reply", "leaked_internals", "ignored_instruction", "forgot_earlier", "invented_context",
         "repeated_itself", "wrong_fact", "ignored_the_question", "too_long", "too_terse", "ritual_closer",
         "stock_opener", "third_person_user", "over_upbeat", "preachy", "weird_emoji")
NO_REPLY = "(no reply: the conversation stopped here)"
# the PLAN.md chat bars
MAX_CATEGORY_DEFICIT = 2        # single-turn category: base wins - candidate wins must not exceed this
MIN_MEDIAN_RATIO = 0.8
MIN_EMOJI_RATIO = 0.5
ALPHA = 0.05


# ------------------------------------------------------------------------------------ replies
def load_rows(paths) -> list[dict]:
    rows = []
    for p in paths:
        for line in Path(p).read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def models_in(rows) -> list[str]:
    return list(dict.fromkeys(r["model"] for r in rows))


def pick_models(rows, base, cand) -> tuple[str, str]:
    ms = models_in(rows)
    base = base or (ms[0] if ms else None)
    cand = cand or next((m for m in ms if m != base), None)
    if not base or not cand or base == cand or base not in ms or cand not in ms:
        raise SystemExit(f"need two models in the replies: found {ms}; pass --base and --cand")
    return base, cand


def group(rows) -> dict:
    """{(model, unit, sample, variant): {turn: row}}"""
    g: dict = defaultdict(dict)
    for r in rows:
        g[(r["model"], r["unit"], r["sample"], r.get("variant", 0))][r["turn"]] = r
    return g


def got_reply(row) -> bool:
    """A row whose reply actually arrived (not an engine error, a timeout or nothing)."""
    return bool(row) and cl.non_reply(row.get("reply"), row.get("error"), row.get("timeout")) not in ("timeout", "error", "empty")


def replies_of(g, model, unit_id, sample, variant, n_turns) -> list[str | None]:
    rows = g.get((model, unit_id, sample, variant), {})
    return [rows[t]["reply"] if got_reply(rows.get(t)) else None for t in range(n_turns)]


# ------------------------------------------------------------------------------------ build
JUDGE_INSTRUCTIONS = """\
You are judging two assistants, A and B, on the same conversation with one person. The labels are anonymous and
their order was randomised. Each side shows the SAME user messages with that assistant's OWN replies; each
assistant answered from its own earlier replies, so the two histories differ. Read the whole conversation on
both sides, then decide which assistant was better for this person overall.

What good looks like:
- Memory and coherence: later replies use what the user said earlier, correctly. A detail the user never gave
  is never invented. It does not contradict itself, and it does not repeat an earlier reply.
- Instructions: a standing instruction from the user ("one sentence", "no questions", "no lists", "call me X",
  "keep it under N words") is obeyed on EVERY later reply until the user lifts it; once lifted, it no longer
  applies. Breaking it later in the conversation counts against the assistant, even if the first replies obeyed.
- Voice: gentle, warm, natural and engaging. It answers what was asked with something concrete, not a generic
  paragraph, not a stock opener or closer. An emoji is fine when it fits the mood. A feeling gets acknowledged
  before any advice, and no advice is pushed on someone who only wants to vent.
- Casual facts asked in passing are answered correctly and briefly, and the chat then picks up where it was.
- A reply that is a failed lookup or a stall ("I couldn't find any results", "I don't have that yet") is not a
  reply. Tool names, skill names, JSON or "I used the X tool" must never show.
- Length is not a virtue either way: judge whether the length suits the message and any instruction.

For a single message, judge that reply. For a set of rewordings of one question (each asked in a fresh
session), judge which assistant is the better, less repetitive conversational partner across the set.
Weigh the later turns most when they depend on earlier ones. Answer "tie" only when you truly cannot separate them.

Reply with ONE line of JSON and nothing else:
{"pair_id": "<id>", "overall_winner": "A" | "B" | "tie", "confidence": 1 | 2 | 3,
 "flags": {"A": [<flags>], "B": [<flags>]}, "rationale": "<one sentence>"}
flags are zero or more of: """ + ", ".join(FLAGS) + "\n"


def _transcript(user_turns, replies) -> list[dict]:
    out = []
    for u, r in zip(user_turns, replies):
        out.append({"role": "user", "text": u})
        out.append({"role": "assistant", "text": r if r is not None else NO_REPLY})
    return out


def build_pairs(rows, units, base: str, cand: str, seed: int = 0) -> tuple[list[dict], dict, list[str]]:
    """(blind pairs, key, notes). Pairs exist for every (unit, sample) with rows for both models."""
    g = group(rows)
    notes: list[str] = []
    todo = []
    for u in units:
        samples = sorted({k[2] for k in g if k[1] == u["id"] and k[0] in (base, cand)})
        for s in samples:
            if not all(any(k[:3] == (m, u["id"], s) for k in g) for m in (base, cand)):
                notes.append(f"{u['id']}.s{s}: only one model has rows; skipped")
                continue
            todo.append((u, s))
    rng = random.Random(f"{seed}:pairs")
    swaps = [True] * (len(todo) // 2) + [False] * (len(todo) - len(todo) // 2)
    rng.shuffle(swaps)
    pairs, key = [], {}
    for (u, s), swap in zip(todo, swaps):
        pid = f"{u['id']}.s{s}"
        side = {"A": cand, "B": base} if swap else {"A": base, "B": cand}
        body = {}
        for label, model in side.items():
            if u["kind"] == "variety":
                body[label] = {"sets": [{"user": turns[0], "reply": (replies_of(g, model, u["id"], s, v, 1)[0] or NO_REPLY)}
                                        for v, turns in cl.unit_runs(u)]}
            else:
                (variant, turns), = cl.unit_runs(u)
                body[label] = {"transcript": _transcript(turns, replies_of(g, model, u["id"], s, variant, len(turns)))}
        pairs.append({"pair_id": pid, "category": u["category"], "kind": u["kind"], "skill": u.get("skill"), **body})
        key[pid] = {"A": side["A"], "B": side["B"], "unit": u["id"], "sample": s, "category": u["category"],
                    "kind": u["kind"], "skill": u.get("skill")}
    return pairs, key, notes


def judge_prompt(pair: dict) -> str:
    shown = {k: v for k, v in pair.items() if k not in ("skill",)}
    return JUDGE_INSTRUCTIONS + "\n## Pair to judge\n```json\n" + json.dumps(shown, ensure_ascii=False, indent=1) + "\n```\n"


def cmd_build(args) -> int:
    rows = load_rows(args.replies)
    base, cand = pick_models(rows, args.base, args.cand)
    units = cl.load_prompts(args.prompts)
    pairs, key, notes = build_pairs(rows, units, base, cand, args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pairs_for_judge.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pairs))
    (out / "pairs_key.json").write_text(json.dumps({"base": base, "cand": cand, "pairs": key}, indent=1))
    (out / "judge_instructions.md").write_text(JUDGE_INSTRUCTIONS)
    for i in range(0, len(pairs), args.chunk):
        chunk = pairs[i:i + args.chunk]
        (out / f"chunk_{i // args.chunk:02d}.jsonl").write_text("".join(
            json.dumps({"pair_id": p["pair_id"], "judge_prompt": judge_prompt(p)}, ensure_ascii=False) + "\n" for p in chunk))
    kinds = Counter(p["kind"] for p in pairs)
    print(f"{len(pairs)} pairs ({', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))}); base={base} cand={cand}")
    print(f"candidate on side A in {sum(1 for k in key.values() if k['A'] == cand)} pairs, side B in the rest")
    for n in notes:
        print("note:", n)
    print(f"wrote {out}/pairs_for_judge.jsonl, pairs_key.json, chunk_*.jsonl")
    print("next: have an independent judge answer every line of every chunk file with the one-line JSON verdict, "
          f"writing {out}/judgments_NN.jsonl (one file per chunk is fine), then run `score`.")
    return 0


# ------------------------------------------------------------------------------------ mechanical checks
def _ratio(ok, n) -> float | None:
    return ok / n if n else None


def tally_models(rows, units, models: list[str], names=()) -> dict:
    """Per model: conversation checks, non-replies, leaks, repeats and the reply measures."""
    g = group(rows)
    samples = sorted({r["sample"] for r in rows})
    out = {}
    for m in models:
        t = {"samples": 0, "convs": 0, "convs_ok": 0, "checks": defaultdict(lambda: [0, 0]),
             "standing_replies": [0, 0], "by_skill": defaultdict(lambda: [0, 0]), "by_unit": defaultdict(lambda: [0, 0]),
             "missing_turns": 0, "non_replies": 0, "non_replies_multi": 0, "leaks": 0, "leaks_multi": 0,
             "repeats": 0, "failed": [], "chars": [], "emoji": [], "reply_rows": 0}
        for u in units:
            for s in samples:
                for variant, turns in cl.unit_runs(u):
                    k = (m, u["id"], s, variant)
                    if k not in g:
                        t["missing_turns"] += len(turns)
                        if u["kind"] == "conversation":
                            t["convs"] += 1
                            t["by_unit"][u["id"]][1] += 1
                            if u.get("skill"):
                                t["by_skill"][u["skill"]][1] += 1
                        continue
                    drows = g[k]
                    reps = replies_of(g, m, u["id"], s, variant, len(turns))
                    t["missing_turns"] += sum(r is None for r in reps)
                    for r in drows.values():
                        if got_reply(r):
                            t["chars"].append(len(r["reply"].strip()))
                            t["emoji"].append(cl.emoji_count(r["reply"]) > 0)
                    multi = u["kind"] == "conversation"
                    for i, rep in enumerate(reps):
                        if rep is None:
                            nr, lk = True, []         # an expected reply that never came is a non-reply
                        else:
                            nr, lk = bool(cl.non_reply(rep)), cl.leaks(rep, names)
                        t["non_replies"] += nr
                        t["leaks"] += bool(lk)
                        if multi:
                            t["non_replies_multi"] += nr
                            t["leaks_multi"] += bool(lk)
                    if multi:
                        ev = cl.evaluate_conversation(u, reps)
                        t["convs"] += 1
                        t["convs_ok"] += ev["ok"]
                        t["by_unit"][u["id"]][0] += ev["ok"]
                        t["by_unit"][u["id"]][1] += 1
                        if u.get("skill"):
                            t["by_skill"][u["skill"]][0] += ev["ok"]
                            t["by_skill"][u["skill"]][1] += 1
                        t["repeats"] += len(ev["repeats"])
                        for c in ev["checks"]:
                            t["checks"][c["type"]][0] += c["ok"]
                            t["checks"][c["type"]][1] += 1
                            if c["type"] == "standing":
                                t["standing_replies"][0] += c["ok"]
                                t["standing_replies"][1] += 1
                        for c in ev["checks"]:
                            if not c["ok"]:
                                t["failed"].append(f"{u['id']}.s{s} t{c['turn']} {c['type']}: {c['detail']}")
        t["samples"] = len(samples)
        t["checks"] = {k: list(v) for k, v in t["checks"].items()}
        t["by_skill"] = {k: list(v) for k, v in t["by_skill"].items()}
        t["by_unit"] = {k: list(v) for k, v in t["by_unit"].items()}
        t["reply_rows"] = len(t["chars"])
        t["median_chars"] = cl.median(t["chars"])
        t["emoji_share"] = sum(t["emoji"]) / len(t["emoji"]) if t["emoji"] else 0.0
        del t["chars"], t["emoji"]
        out[m] = t
    return out


def _pct(pair) -> str:
    return "n/a" if not pair[1] else f"{pair[0]}/{pair[1]} ({pair[0] / pair[1]:.0%})"


def render_mech(tally: dict) -> str:
    models = list(tally)
    lines = ["| metric | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]

    def row(label, f):
        lines.append(f"| {label} | " + " | ".join(f(tally[m]) for m in models) + " |")
    row("conversations passing every explicit check", lambda t: _pct([t["convs_ok"], t["convs"]]))
    for skill in cl.SKILLS:
        row(f"  skill {skill}", lambda t, s=skill: _pct(t["by_skill"].get(s, [0, 0])))
    for ty in cl.CHECK_TYPES:
        if any(ty in tally[m]["checks"] for m in models):
            label = {"standing": "standing instruction, per reply", "recall": "recall of a user detail"}.get(ty, f"check {ty}")
            row(label, lambda t, k=ty: _pct(t["checks"].get(k, [0, 0])))
    row("non-replies (all / multi-turn)", lambda t: f"{t['non_replies']} / {t['non_replies_multi']}")
    row("replies leaking internals (all / multi-turn)", lambda t: f"{t['leaks']} / {t['leaks_multi']}")
    row("repeated replies in conversations", lambda t: str(t["repeats"]))
    row("expected replies that never came", lambda t: str(t["missing_turns"]))
    row("median reply chars", lambda t: f"{t['median_chars']:.0f}")
    row("replies with an emoji", lambda t: f"{t['emoji_share']:.0%}")
    return "\n".join(lines)


def cmd_mech(args) -> int:
    rows = load_rows(args.replies)
    base, cand = pick_models(rows, args.base, args.cand)
    units = cl.load_prompts(args.prompts)
    names = _tool_names(args.tool_names)
    tally = tally_models(rows, units, [base, cand], names)
    print(render_mech(tally))
    if args.details:
        for m, t in tally.items():
            print(f"\n{m}: {len(t['failed'])} failed checks")
            for f in t["failed"]:
                print("  -", f)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(tally, indent=1))
    return 0


def _tool_names(path) -> list[str]:
    if not path:
        return []
    data = json.loads(Path(path).read_text())
    return [str(x) for x in (data["tools"] if isinstance(data, dict) else data)]


# ------------------------------------------------------------------------------------ score
def load_judgments(patterns) -> dict[str, dict]:
    """pair_id -> judgment (the last valid line wins). Invalid lines are skipped."""
    out: dict[str, dict] = {}
    files = sorted({f for p in patterns for f in (glob.glob(p) or [p])})
    for f in files:
        for line in Path(f).read_text().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if isinstance(j, dict) and j.get("pair_id") and j.get("overall_winner") in ("A", "B", "tie"):
                out[j["pair_id"]] = j
    return out


def _flags(j, side) -> list[str]:
    f = (j.get("flags") or {}).get(side) or []
    return [x for x in f if isinstance(x, str)]


def judge_tally(key: dict, judgments: dict, base: str, cand: str) -> dict:
    pairs = key["pairs"]
    res = {"judged": 0, "unjudged": [], "wins": 0, "losses": 0, "ties": 0, "by_kind": {}, "by_category": {},
           "by_skill": {}, "by_unit": defaultdict(int), "flags": {base: Counter(), cand: Counter()},
           "multi_not_a_reply": {base: 0, cand: 0}, "not_a_reply": {base: 0, cand: 0}}
    def bump(d, k, w):
        d.setdefault(k, {"wins": 0, "losses": 0, "ties": 0})[w] += 1
    for pid, k in pairs.items():
        j = judgments.get(pid)
        if j is None:
            res["unjudged"].append(pid)
            continue
        res["judged"] += 1
        win = j["overall_winner"]
        outcome = "ties" if win == "tie" else ("wins" if k[win] == cand else "losses")
        res[outcome] += 1
        bump(res["by_kind"], k["kind"], outcome)
        if k["kind"] != "conversation":
            bump(res["by_category"], k["category"], outcome)
        elif k.get("skill"):
            bump(res["by_skill"], k["skill"], outcome)
        res["by_unit"][k["unit"]] += {"wins": 1, "losses": -1, "ties": 0}[outcome]
        for side in ("A", "B"):
            fl = _flags(j, side)
            res["flags"][k[side]].update(fl)
            if "not_a_reply" in fl:
                res["not_a_reply"][k[side]] += 1
                if k["kind"] == "conversation":
                    res["multi_not_a_reply"][k[side]] += 1
    res["by_unit"] = dict(res["by_unit"])
    return res


def apply_bars(jt: dict, mt: dict, base: str, cand: str, *, max_leaks: int = 0, strict_mech: bool = False) -> list[dict]:
    """The PLAN.md chat bars as [{name, ok, detail, hard}]. `mt` is tally_models()."""
    bars = []

    def bar(name, ok, detail, hard=True):
        bars.append({"name": name, "ok": bool(ok), "detail": detail, "hard": hard})
    w, l, t = jt["wins"], jt["losses"], jt["ties"]
    p = cl.sign_test_p(w, l)
    bar("blind judge, whole conversation: candidate wins more pairs, sign test p < 0.05", w > l and p < ALPHA,
        f"candidate {w} - base {l}, {t} ties, p={p:.4f}")
    worst = []
    for cat, c in sorted(jt["by_category"].items()):
        deficit = c["losses"] - c["wins"]
        if deficit > MAX_CATEGORY_DEFICIT:
            worst.append(f"{cat} {c['wins']}-{c['losses']}")
    bar(f"no single-turn category loses by more than {MAX_CATEGORY_DEFICIT} pairs", not worst,
        "; ".join(worst) if worst else f"{len(jt['by_category'])} categories, worst deficit "
        f"{max([c['losses'] - c['wins'] for c in jt['by_category'].values()] or [0])}")
    b, c_ = mt[base], mt[cand]
    nr_ok = (c_["non_replies"] <= b["non_replies"] and jt["not_a_reply"][cand] <= jt["not_a_reply"][base]
             and c_["non_replies_multi"] == 0 and jt["multi_not_a_reply"][cand] == 0)
    bar("non-replies <= base, and 0 in multi-turn", nr_ok,
        f"mechanical cand {c_['non_replies']} vs base {b['non_replies']} (multi-turn {c_['non_replies_multi']}); "
        f"judge flag cand {jt['not_a_reply'][cand]} vs base {jt['not_a_reply'][base]} "
        f"(multi-turn {jt['multi_not_a_reply'][cand]})")
    bar(f"no leaked tool or skill names (<= {max_leaks} replies)", c_["leaks"] <= max_leaks,
        f"candidate {c_['leaks']} replies leak (base {b['leaks']}); judge flag leaked_internals "
        f"cand {jt['flags'][cand]['leaked_internals']} base {jt['flags'][base]['leaked_internals']}")
    ratio = c_["median_chars"] / b["median_chars"] if b["median_chars"] else 1.0
    bar(f"median reply >= {MIN_MEDIAN_RATIO}x base", ratio >= MIN_MEDIAN_RATIO,
        f"{c_['median_chars']:.0f} vs {b['median_chars']:.0f} chars = {ratio:.2f}x")
    eratio = c_["emoji_share"] / b["emoji_share"] if b["emoji_share"] else 1.0
    bar(f"emoji share >= {MIN_EMOJI_RATIO}x base", eratio >= MIN_EMOJI_RATIO,
        f"{c_['emoji_share']:.0%} vs {b['emoji_share']:.0%} of replies = {eratio:.2f}x")
    # proposed (advisory unless --strict-mech): the mechanical checks must not be worse than base
    def rate(t, f):
        ok, n = f(t)
        return ok / n if n else None
    for name, f in (("conversations passing every explicit check", lambda t: (t["convs_ok"], t["convs"])),
                    ("recall of user details", lambda t: tuple(t["checks"].get("recall", [0, 0]))),
                    ("standing instructions obeyed, per reply", lambda t: tuple(t["standing_replies"]))):
        rb, rc = rate(b, f), rate(c_, f)
        ok = rb is None or rc is None or rc >= rb
        bar(f"[proposed] mechanical: {name} >= base", ok,
            f"candidate {_pct(f(c_))} vs base {_pct(f(b))}", hard=strict_mech)
    return bars


def unit_level(jt) -> tuple[int, int, int, float]:
    w = sum(v > 0 for v in jt["by_unit"].values())
    l = sum(v < 0 for v in jt["by_unit"].values())
    return w, l, len(jt["by_unit"]) - w - l, cl.sign_test_p(w, l)


def render_score(base, cand, jt, mt, bars, n_pairs) -> str:
    L = [f"# Chat eval, whole-conversation blind judge: {cand} (candidate) vs {base} (base)", ""]
    w, l, t = jt["wins"], jt["losses"], jt["ties"]
    uw, ul, ut, up = unit_level(jt)
    L += [f"{jt['judged']} of {n_pairs} pairs judged. Sign test = exact two-sided binomial on decisive pairs (ties dropped).",
          "The samples of one unit are not independent; the unit-level line is the conservative view.", "",
          "| contest | cand wins | base wins | ties | p |", "|---|---:|---:|---:|---:|",
          f"| all pairs | {w} | {l} | {t} | {cl.sign_test_p(w, l):.4f} |",
          f"| per unit (net of its samples) | {uw} | {ul} | {ut} | {up:.4f} |"]
    for k, c in sorted(jt["by_kind"].items()):
        L.append(f"| {k} pairs | {c['wins']} | {c['losses']} | {c['ties']} | {cl.sign_test_p(c['wins'], c['losses']):.4f} |")
    L += ["", "## Single-turn categories (candidate wins - base wins)", "", "| category | cand | base | ties |", "|---|---:|---:|---:|"]
    for cat, c in sorted(jt["by_category"].items()):
        L.append(f"| {cat} | {c['wins']} | {c['losses']} | {c['ties']} |")
    if jt["by_skill"]:
        L += ["", "## Multi-turn conversations by skill", "", "| skill | cand | base | ties |", "|---|---:|---:|---:|"]
        for sk, c in sorted(jt["by_skill"].items()):
            L.append(f"| {sk} | {c['wins']} | {c['losses']} | {c['ties']} |")
    L += ["", "## Judge flags", ""]
    for m in (base, cand):
        L.append(f"- {m}: " + (", ".join(f"{k} x{n}" for k, n in jt["flags"][m].most_common()) or "none"))
    L += ["", "## Mechanical checks (no judge)", "", render_mech({base: mt[base], cand: mt[cand]}), "", "## Bars", ""]
    for b in bars:
        mark = "PASS" if b["ok"] else ("FAIL" if b["hard"] else "note")
        L.append(f"- **{mark}** {b['name']}: {b['detail']}" + ("" if b["hard"] else "  (advisory)"))
    hard = [b for b in bars if b["hard"]]
    L += ["", f"**VERDICT: {'PASS' if all(b['ok'] for b in hard) else 'FAIL'}** ({sum(b['ok'] for b in hard)}/{len(hard)} hard bars hold)"]
    return "\n".join(L)


def cmd_score(args) -> int:
    rows = load_rows(args.replies)
    key = json.loads(Path(args.key).read_text())
    base, cand = args.base or key["base"], args.cand or key["cand"]
    if {base, cand} != {key["base"], key["cand"]}:
        raise SystemExit(f"--base/--cand disagree with the key ({key['base']}, {key['cand']})")
    judgments = load_judgments(args.judgments)
    jt = judge_tally(key, judgments, base, cand)
    if jt["unjudged"]:
        print(f"INCOMPLETE: {len(jt['unjudged'])} of {len(key['pairs'])} pairs have no judgment, e.g. {jt['unjudged'][:5]}")
        print("a verdict needs every pair judged; judge the rest and run score again")
        return 3
    units = cl.load_prompts(args.prompts)
    mt = tally_models(rows, units, [base, cand], _tool_names(args.tool_names))
    bars = apply_bars(jt, mt, base, cand, max_leaks=args.max_leaks, strict_mech=args.strict_mech)
    text = render_score(base, cand, jt, mt, bars, len(key["pairs"]))
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".md").write_text(text + "\n")
        out.with_suffix(".json").write_text(json.dumps({
            "base": base, "cand": cand, "pairs": len(key["pairs"]),
            "wins": jt["wins"], "losses": jt["losses"], "ties": jt["ties"],
            "p": cl.sign_test_p(jt["wins"], jt["losses"]), "bars": bars,
            "verdict": "PASS" if all(b["ok"] for b in bars if b["hard"]) else "FAIL",
            "mech": mt, "judge": {k: v for k, v in jt.items() if k != "flags"},
            "flags": {m: dict(c) for m, c in jt["flags"].items()}}, indent=1, default=list))
    return 0 if all(b["ok"] for b in bars if b["hard"]) else 1


# ------------------------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--replies", nargs="+", required=True, help="replies.jsonl file(s) from chat_eval.py")
        p.add_argument("--prompts", default=str(cl.DEFAULT_PROMPTS))
        p.add_argument("--base", default="", help="the untuned model (default: first in the replies)")
        p.add_argument("--cand", default="", help="the candidate (default: the second)")
        p.add_argument("--tool-names", default="", help="optional JSON list of tool/skill names that must never appear")
    b = sub.add_parser("build")
    common(b)
    b.add_argument("--out", required=True)
    b.add_argument("--chunk", type=int, default=20, help="pairs per chunk file")
    b.add_argument("--seed", type=int, default=0, help="seed of the A/B order")
    m = sub.add_parser("mech")
    common(m)
    m.add_argument("--json-out", default="")
    m.add_argument("--details", action="store_true", help="list every failed check")
    s = sub.add_parser("score")
    common(s)
    s.add_argument("--key", required=True)
    s.add_argument("--judgments", nargs="+", required=True, help="judgments file(s) or globs")
    s.add_argument("--out", default="", help="write <out>.md and <out>.json")
    s.add_argument("--max-leaks", type=int, default=0)
    s.add_argument("--strict-mech", action="store_true", help="make the proposed mechanical bars hard")
    args = ap.parse_args(argv)
    return {"build": cmd_build, "mech": cmd_mech, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
