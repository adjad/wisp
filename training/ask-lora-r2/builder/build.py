"""Build the dataset: run every scenario through /agent, keep one example per
model step (the captured request + the ideal response), split 90/10 by family."""
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import random
import sys
import traceback
from collections import Counter, defaultdict

import harness as H
import leak
import scen
import thinking

OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# The production scaffold once quoted a few real-user details; since f6a72430
# the repository uses made-up stand-ins, so there is nothing left to replace.
SCAFFOLD_SUBS: list[tuple[str, str]] = []


def sanitize(obj):
    if isinstance(obj, str):
        for a, b in SCAFFOLD_SUBS:
            obj = obj.replace(a, b)
        return obj
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    return obj


def target_message(resp: dict) -> dict:
    m = resp["message"]
    if m.get("tool_calls"):
        return {"role": "assistant", "content": m.get("content") or "", "tool_calls": copy.deepcopy(m["tool_calls"])}
    return {"role": "assistant", "content": m["content"]}


def clean_history(messages):
    out = []
    for m in messages:
        m = {k: v for k, v in m.items() if v is not None or k == "content"}
        if m.get("content") is None:
            m["content"] = ""
        out.append(m)
    return out


def _calls(responses, kinds) -> list[tuple[str, dict]]:
    """(tool, args) of every scripted call in `responses` whose kind is in `kinds`."""
    return [(c["function"]["name"], json.loads(c["function"]["arguments"]))
            for r in responses if r["kind"] in kinds
            for c in r["message"]["tool_calls"]]


# Arguments the HOST adds or rewrites before a write runs, exactly as production
# does: update_event's start is pinned to the Mac's UTC offset before the card
# (assistant_tools.bind_calendar_local_start); a bulk delete is bound to the ids its
# card listed; a reply is frozen to the envelope Mail prepared (prepare_reply_args).
HOST_BOUND = {
    "clear_reminders": {"expected_ids"},
    "reply_to_email": {"expected_reply", "account", "reply_all"},
}


def _same_call(s_, r_) -> bool:
    if s_[0] != r_[0]:
        return False
    sent, ran = dict(s_[1]), dict(r_[1])
    if s_[0] in ("update_event", "add_calendar_event") and "when_iso" in sent:
        from service.tools.assistant_tools import bind_calendar_local_start
        try:
            sent["when_iso"] = bind_calendar_local_start(sent["when_iso"])
        except Exception:  # noqa: BLE001  (an invalid time never reaches the tool)
            pass
    if s_[0] == "schedule_send" and sent.get("when") != ran.get("when"):
        # The schedule card resolves the user's words ("tomorrow at 9") to ISO before it runs.
        try:
            _dt.datetime.fromisoformat(str(ran.get("when")))
            sent.pop("when", None)
            ran.pop("when", None)
        except ValueError:
            return False
    if any(ran.get(k) != v for k, v in sent.items()):
        return False
    bound = HOST_BOUND.get(s_[0], set())
    extra = {k: v for k, v in ran.items() if k not in sent and k not in bound}
    return all(v in ("", None, False, "start", 0) for v in extra.values())


def _match_ran(sent, ran) -> str | None:
    """Every scripted call must run with its own arguments, in order. A WRITE may
    legitimately not run at all (its card was denied, or the host refused it before
    the card, e.g. a move the loop's prepare_event_move rejected); a read may not."""
    j = 0
    for s_ in sent:
        if j < len(ran) and _same_call(s_, ran[j]):
            j += 1
        elif not H.is_write(*s_):
            return f"read {s_[0]} did not run as scripted"
    return None if j == len(ran) else f"{len(ran) - j} unscripted tool run(s)"


def main(only=None):
    rng = random.Random(20261029)
    examples, metas, failures = [], [], []
    per_family_scen = Counter()
    leaks = leak.LeakIndex()
    for family, fn in scen.FAMILIES.items():
        if only and family not in only:
            continue
        frng = random.Random(f"{family}-{rng.random()}")
        scenarios = fn(frng)
        for k, sc in enumerate(scenarios):
            sid = f"{family}-{k:03d}"
            leaked, score, q = leaks.leaks(sc.prompt)
            if leaked:
                failures.append((sid, sc.prompt, f"leaks eval {q['id']} ({score:.2f}): {q['prompt']!r}"))
                continue
            # Round-2 scenario fields (writes.py); scen.Scenario may not declare them yet.
            approve = getattr(sc, "approve", None)
            expect_cards = getattr(sc, "expect_cards", None)
            try:
                policy, events, final = H.run_scenario(sc.prompt, sc.steps, sc.world, sc.when, profile=sc.profile,
                                                       approve=approve)
            except Exception as exc:  # noqa: BLE001
                failures.append((sid, sc.prompt, f"harness: {exc!r}"))
                continue
            cards_seen = list(H.CURRENT["cards"])
            errs = [e for e in events if e.get("type") == "error"]
            want = sc.steps[-1].text if isinstance(sc.steps[-1], H.Final) else None
            if policy.idx != len(policy.steps) or errs or len(policy.requests) != len(policy.responses):
                failures.append((sid, sc.prompt, f"incomplete: idx={policy.idx}/{len(policy.steps)} "
                                 f"reqs={len(policy.requests)} errs={errs[:1]} final={final!r:.120}"))
                continue
            called = _calls(policy.responses, ("call", "bad"))
            wrote = any(H.is_write(n, a) for n, a in called)
            # The training target is always the scripted step. On a turn that called a
            # write tool the host may show its own text instead (_verified_final:
            # receipts, denial text, delivery checks); that is recorded, not trained.
            # A read turn whose final the host changed still fails.
            shown_final = None
            if want is None or (final or "").strip() != want.strip():
                if want is not None and not wrote:
                    failures.append((sid, sc.prompt, f"final text changed by host: {final!r:.200} vs {want!r:.80}"))
                    continue
                if wrote:
                    shown_final = final
            sent = _calls(policy.responses, ("call",))
            bad_sent = _calls(policy.responses, ("bad",))
            ran = [(n, a) for n, a in H.CURRENT["calls"] if (n, a) not in bad_sent]
            problem = _match_ran(sent, ran)
            if problem:
                failures.append((sid, sc.prompt, f"host rewrote tool args: {problem} sent={sent} ran={ran}"))
                continue
            effects = [[t, a] for t, a in getattr(sc.world, "effects", [])]
            turn_writes = [n for n, a in _calls(policy.responses, ("call", "bad", "load", "bad_load")) if H.is_write(n, a)]
            per_family_scen[family] += 1
            first_new = len(examples)
            for i, (req, resp) in enumerate(zip(policy.requests, policy.responses)):
                if resp["kind"] in ("bad", "bad_load"):
                    continue
                ex = {"messages": sanitize(clean_history(req["messages"])) + [target_message(resp)],
                      "tools": sanitize(copy.deepcopy(req.get("tools") or []))}
                examples.append(ex)
                writes = [n for n, a in _calls(policy.responses[:i + 1], ("call", "bad", "load", "bad_load")) if H.is_write(n, a)]
                metas.append({"id": f"{sid}-s{i}", "family": family, "paraphrase_of": sc.paraphrase_of,
                              "notes": sc.notes, "step": i, "steps_in_turn": len(policy.requests),
                              "target_kind": resp["kind"], "prompt": sc.prompt,
                              "clock": sc.when.strftime("%a %Y-%m-%d %H:%M"),
                              "tool_choice": req.get("tool_choice"),
                              "chat_template_kwargs": req.get("chat_template_kwargs"),
                              "writes": writes, "approve": approve, "expect_cards": expect_cards,
                              "cards_seen": cards_seen, "shown_final": shown_final, "effects": effects,
                              "turn_writes": turn_writes})
            think_turn(examples[first_new:], metas[first_new:], failures)
    return examples, metas, failures, per_family_scen


def turn_context(exs: list[dict], k: int) -> dict:
    """What thinking.make_thought needs from the rest of the turn: the later steps' calls and the final."""
    final = next((e["messages"][-1]["content"] for e in exs if not e["messages"][-1].get("tool_calls")), "")
    later = [c for e in exs[k + 1:] for c in thinking.calls_of(e["messages"][-1])]
    return {"later_calls": later, "final": final}


def think_turn(exs: list[dict], ms: list[dict], failures: list) -> None:
    """Situational thinking (thinking.py): record per step whether it thinks, and the thought."""
    for k, (ex, m) in enumerate(zip(exs, ms)):
        m["step_kind"] = thinking.step_kind(m, ex["messages"])
        m["think"] = thinking.decide(m, ex["messages"])
        m["thought"] = None
        if m["think"]:
            t, errs = thinking.make_thought(m, ex["messages"], turn_context(exs, k))
            m["thought"] = t
            if errs:
                failures.append((m["id"], m["prompt"], f"thought: {errs} {t!r}"))


def fix_adjacent(examples, metas, orders) -> list[str]:
    """No two consecutive THINK examples (in all.jsonl, train or valid order) share a thought: re-phrase the
    later one, avoiding both neighbours. Returns ids that could not be made distinct."""
    turn_of = defaultdict(list)
    for i, m in enumerate(metas):
        turn_of[m["id"].rsplit("-s", 1)[0]].append(i)
    stuck = []
    for _ in range(5):
        changed = False
        for order in orders:
            seq = [i for i in order if metas[i].get("think")]
            for a, b, c in zip(seq, seq[1:], seq[2:] + [None]):
                if metas[a]["thought"] != metas[b]["thought"]:
                    continue
                tidx = turn_of[metas[b]["id"].rsplit("-s", 1)[0]]
                ctx = turn_context([examples[i] for i in tidx], tidx.index(b))
                avoid = {metas[a]["thought"]} | ({metas[c]["thought"]} if c is not None else set())
                t, errs = thinking.make_thought(metas[b], examples[b]["messages"], ctx, avoid)
                if errs:
                    stuck.append(metas[b]["id"])
                    continue
                metas[b]["thought"] = t
                changed = True
        if not changed:
            break
    return sorted(set(stuck))


def for_training(ex: dict) -> dict:
    """Gemma 4's chat template takes tool-call arguments as an object and raises
    on the JSON string the OpenAI wire format (and Wisp's loop) uses, so the
    written files carry objects. The captured requests are otherwise unchanged."""
    ex = copy.deepcopy(ex)
    for m in ex["messages"]:
        for tc in m.get("tool_calls") or []:
            if isinstance(tc["function"].get("arguments"), str):
                tc["function"]["arguments"] = json.loads(tc["function"]["arguments"])
    return ex


def write(examples, metas):
    examples = [for_training(ex) for ex in examples]
    train_idx, valid_idx = split(metas)
    stuck = fix_adjacent(examples, metas, [list(range(len(metas))), train_idx, valid_idx])
    if stuck:
        raise SystemExit(f"thoughts: consecutive duplicates that could not be re-phrased: {stuck[:5]}")
    with open(os.path.join(OUT, "all.jsonl"), "w") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    with open(os.path.join(OUT, "meta.jsonl"), "w") as f:
        for m in metas:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    for name, idxs in (("train", train_idx), ("valid", valid_idx)):
        with open(os.path.join(OUT, f"{name}.jsonl"), "w") as f:
            for i in sorted(idxs):
                f.write(json.dumps(examples[i], ensure_ascii=False) + "\n")
        with open(os.path.join(OUT, f"{name}.meta.jsonl"), "w") as f:
            for i in sorted(idxs):
                f.write(json.dumps(metas[i], ensure_ascii=False) + "\n")
    return train_idx, valid_idx


def split(metas):
    # 90/10 split by family. The unit is a prompt GROUP (prompts that fold to the
    # same text, see leak.fold; a group belongs to the family that first uses it),
    # so every step of a turn, and every turn that
    # asks the same question of a different world, lands in the same split.
    group_family: dict[str, str] = {}
    for m in metas:
        group_family.setdefault(leak.fold(m["prompt"]), m["family"])
    by_family = defaultdict(set)
    for g, family in group_family.items():
        by_family[family].add(g)
    split_rng = random.Random(7)
    valid_groups = set()
    for family, groups in sorted(by_family.items()):
        groups = sorted(groups)
        split_rng.shuffle(groups)
        valid_groups |= set(groups[:max(1, round(len(groups) * 0.1))])
    train_idx, valid_idx = [], []
    for idx, m in enumerate(metas):
        (valid_idx if leak.fold(m["prompt"]) in valid_groups else train_idx).append(idx)
    return train_idx, valid_idx


def write_stats(metas, train_idx, valid_idx):
    """stats.json: the counts the README quotes, plus the code head they came from."""
    import subprocess
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=H.boot.REPO).stdout.strip()
    # the code that shaped the prompts and tools: the last commit touching service/
    service = subprocess.run(["git", "log", "-1", "--format=%H", "--", "service"], capture_output=True, text=True,
                             cwd=H.boot.REPO).stdout.strip()
    turns = {m["id"].rsplit("-s", 1)[0]: m for m in metas}
    split_of = {i: "train" for i in train_idx} | {i: "valid" for i in valid_idx}
    fam = defaultdict(lambda: Counter())
    for i, m in enumerate(metas):
        fam[m["family"]]["examples"] += 1
        fam[m["family"]][split_of[i]] += 1
        fam[m["family"]][m["target_kind"]] += 1
        fam[m["family"]]["think"] += bool(m.get("think"))
    for t, m in turns.items():
        fam[m["family"]]["turns"] += 1
    by_kind = defaultdict(Counter)
    for i, m in enumerate(metas):
        by_kind[m.get("step_kind")]["think" if m.get("think") else "no_think"] += 1
        by_kind[m.get("step_kind")][f"{split_of[i]}_think"] += bool(m.get("think"))
    n_think = sum(bool(m.get("think")) for m in metas)
    thinking_stats = {"think": n_think, "no_think": len(metas) - n_think,
                      "think_share": round(n_think / max(1, len(metas)), 3),
                      "train_think": sum(bool(metas[i].get("think")) for i in train_idx),
                      "valid_think": sum(bool(metas[i].get("think")) for i in valid_idx),
                      "by_step_kind": {k: dict(v) for k, v in sorted(by_kind.items(), key=lambda kv: str(kv[0]))}}
    stats = {"code_head": head, "service_head": service, "examples": len(metas), "turns": len(turns),
             "train": len(train_idx), "valid": len(valid_idx),
             "by_target": dict(Counter(m["target_kind"] for m in metas)),
             "paraphrased_turns": sum(1 for m in turns.values() if m.get("paraphrase_of")),
             "thinking": thinking_stats,
             "families": {k: dict(v) for k, v in sorted(fam.items())}}
    with open(os.path.join(OUT, "stats.json"), "w") as f:
        json.dump(stats, f, indent=1, sort_keys=True)
        f.write("\n")


if __name__ == "__main__":
    only = set(sys.argv[1:]) or None
    examples, metas, failures, per_scen = main(only)
    print("scenarios ok per family:", dict(per_scen))
    print("examples:", len(examples), "failures:", len(failures))
    for f in failures:
        print("FAIL", f)
    if not only:
        tr, va = write(examples, metas)
        fam = Counter(m["family"] for m in metas)
        print("per family:", dict(fam))
        print("train", len(tr), "valid", len(va))
        print("train per family:", dict(Counter(metas[i]["family"] for i in tr)))
        print("valid per family:", dict(Counter(metas[i]["family"] for i in va)))
        write_stats(metas, tr, va)
        n_think = sum(bool(m.get("think")) for m in metas)
        print(f"thinking: {n_think} THINK / {len(metas) - n_think} NO THINK ({n_think / len(metas):.1%})")
        for f_ in sorted(fam):
            k = sum(bool(m.get("think")) for m in metas if m["family"] == f_)
            print(f"   {f_:30s} {k:4d}/{fam[f_]:4d}")
    if any(str(f[2]).startswith("thought:") for f in failures):
        sys.exit(1)
    else:
        with open(os.path.join(OUT, "builder", "partial.jsonl"), "w") as f:
            for ex, m in zip(examples, metas):
                f.write(json.dumps({"meta": m, **ex}, ensure_ascii=False) + "\n")
