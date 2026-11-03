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

# ASK_LORA_OUT points a later round (round 3: training/ask-lora-r3) at its own data dir.
OUT = os.environ.get("ASK_LORA_OUT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Round 4 (training/ask-lora-r4) builds on round 3: every round-3 rule below holds for it too.
R3PLUS = os.environ.get("ASK_LORA_ROUND") in ("3", "4")


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
        draw = rng.random()   # always drawn, so round-2 seeds stay as committed
        # Round 3: a family's seed depends on its name only, so adding a family never reshuffles another.
        frng = random.Random(f"{family}-r3" if R3PLUS else f"{family}-{draw}")
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
            if R3PLUS and expect_cards is not None and list(expect_cards) != cards_seen:
                # The host refused or skipped a scripted write (e.g. intent_guard on a natural reschedule
                # phrasing), so the scripted final would claim an effect that never happened: drop it.
                failures.append((sid, sc.prompt, f"cards: expected {list(expect_cards)} saw {cards_seen}"))
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
                              "turn_writes": turn_writes,
                              "clarify": bool(getattr(sc, "clarify", False)) and resp["kind"] == "final",
                              "unsafe_reads": list(getattr(sc, "unsafe_reads", ()) or ()),
                              "thread": getattr(sc, "thread", None), "qkind": getattr(sc, "qkind", None),
                              "recall_refs": list(getattr(sc, "recall_refs", ()) or ()),
                              "recall_absent": getattr(sc, "recall_absent", None),
                              "recall_ok": list(getattr(sc, "recall_ok", ()) or ()),
                              "anchor_pos": getattr(sc, "anchor_pos", None)})
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
            extract = thinking.ROUND4 and m["step_kind"] == "narrate" and not thinking.decide_r3(m, ex["messages"])
            if errs and extract:
                # Round 4: no extraction passes (nothing in the results the final states, or it would restate
                # the reply): this final stays QUIET, as in round 3. Counted in stats.json.
                m["think"], m["thought"], m["extract_skipped"] = False, None, errs[:2]
            elif errs:
                failures.append((m["id"], m["prompt"], f"thought: {errs} {t!r}"))
            m["extract"] = bool(extract and m["think"])


def fix_adjacent(examples, metas, orders) -> list[str]:
    """No two consecutive THINK examples (in all.jsonl, train or valid order) share a thought: re-phrase the
    later one, avoiding both neighbours. Returns ids that could not be made distinct."""
    turn_of = defaultdict(list)
    for i, m in enumerate(metas):
        turn_of[m["id"].rsplit("-s", 1)[0]].append(i)
    stuck = []
    for _ in range(20 if R3PLUS else 5):     # round 4's larger mix needs more passes: a fix in one order can break another
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
                    stuck.append(f'{metas[b]["id"]}: {errs[:2]} thought={metas[b]["thought"]!r}')
                    continue
                metas[b]["thought"] = t
                changed = True
        if not changed:
            break
    if R3PLUS:   # whatever is still adjacent after the passes is stuck, not silently written
        for order in orders:
            seq = [i for i in order if metas[i].get("think")]
            stuck += [f'{metas[b]["id"]}: still equals its neighbour' for a, b in zip(seq, seq[1:])
                      if metas[a]["thought"] == metas[b]["thought"]]
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
    train_idx, valid_idx = split(metas, examples)
    write_manifest(metas, train_idx, valid_idx)
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


# Families whose rows share a seeded conversation: one chat history feeds many rows (a thread asked several ways,
# a long chat recalled at several points). Their split unit is the CONVERSATION, so no valid row has a train
# sibling with the same earlier turns. A scenario that records its own thread (session_chat_r4) keeps it; the
# others get one derived from the seeded history (thread_of).
CONVERSATION_FAMILIES = ("session_chat_r4", "session_recall_r4", "instructions_r4")
_DIGEST_USER = "- User: "


def conversation_key(messages) -> str:
    """The folded first two user messages of the seeded history. A long chat that production condensed carries its
    oldest user messages as "- User: ..." lines of the "Earlier in this conversation" system note, so those count
    too, in order. The final user message is never part of the key. "" when there is no earlier user message."""
    users = []
    for m in messages[:-1]:
        content = m.get("content")
        if not isinstance(content, str):
            continue
        if m.get("role") == "system" and content.startswith("Earlier in this conversation"):
            users += [line[len(_DIGEST_USER):].rstrip("…") for line in content.split("\n") if line.startswith(_DIGEST_USER)]
        elif m.get("role") == "user":
            users.append(content)
        if len(users) >= 2:
            break
    return "|".join(leak.fold(u)[:80] for u in users[:2])


def assign_threads(metas, examples):
    """meta.thread for every row of a CONVERSATION family that did not record one: a stable id of its seeded history
    (the same two opening user messages = the same conversation). Single-turn rows stay None (one group each)."""
    import hashlib
    for m, ex in zip(metas, examples):
        if m.get("thread") or m["family"] not in CONVERSATION_FAMILIES:
            continue
        key = conversation_key(ex["messages"])
        if key:
            m["thread"] = "conv-" + hashlib.sha1(key.encode()).hexdigest()[:12]


def _h(text: str) -> int:
    import hashlib
    return int(hashlib.sha1(text.encode()).hexdigest(), 16)


def split(metas, examples=None):
    """RL-SCOPE C16. The unit is a CONNECTED COMPONENT of prompt groups: groups (prompts that fold to the same text,
    leak.fold) are joined when they share a non-null meta.thread, so no thread spans train and valid; a row without
    a thread is a unit of its own group. A unit is valid when sha1 of its smallest fold is 0 mod 10, which for a
    one-group unit is exactly sha1(leak.fold(prompt)) % 10 == 0. Stratified: a family with no valid unit gets its
    lowest-sha1 unit (a unit belongs to the family that first uses its smallest group). Asserts that no group and no
    thread is on both sides. The result of the last call is kept in LAST_SPLIT for the manifest."""
    if examples is not None:
        assign_threads(metas, examples)
    fold_of = [leak.fold(m["prompt"]) for m in metas]
    group_family: dict[str, str] = {}
    for f, m in zip(fold_of, metas):
        group_family.setdefault(f, m["family"])
    parent = {f: f for f in group_family}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first_of_thread: dict[str, str] = {}
    for f, m in zip(fold_of, metas):
        t = m.get("thread")
        if t:
            ra, rb = find(first_of_thread.setdefault(t, f)), find(f)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)         # the smaller fold is the unit's key
    units: dict[str, list[str]] = defaultdict(list)
    for f in sorted(group_family):
        units[find(f)].append(f)
    by_family = defaultdict(list)
    for key in units:
        by_family[group_family[key]].append(key)
    valid_units = {k for k in units if _h(k) % 10 == 0}
    for family, keys in by_family.items():
        if not any(k in valid_units for k in keys):
            valid_units.add(min(keys, key=lambda k: (_h(k), k)))
    valid_groups = {f for k in valid_units for f in units[k]}
    train_idx, valid_idx = [], []
    for idx, f in enumerate(fold_of):
        (valid_idx if f in valid_groups else train_idx).append(idx)
    # asserts: no prompt group and no thread on both sides; every family has a valid unit
    assert not ({fold_of[i] for i in train_idx} & valid_groups), "a prompt group is in both splits"
    thread_side: dict[str, set] = defaultdict(set)
    for idx, m in enumerate(metas):
        if m.get("thread"):
            thread_side[m["thread"]].add(fold_of[idx] in valid_groups)
    spanning = sorted(t for t, v in thread_side.items() if len(v) > 1)
    assert not spanning, f"threads in both train and valid: {spanning[:5]}"
    assert all(any(k in valid_units for k in keys) for keys in by_family.values()), "a family has no valid unit"
    LAST_SPLIT.clear()
    LAST_SPLIT.update(valid_threads=sorted(t for t, v in thread_side.items() if v == {True}),
                      valid_units={fam: sum(k in valid_units for k in keys) for fam, keys in sorted(by_family.items())},
                      units={fam: len(keys) for fam, keys in sorted(by_family.items())},
                      valid_folds=sorted(valid_groups))
    return train_idx, valid_idx


LAST_SPLIT: dict = {}
R3_VALID_META = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ask-lora-r3", "data", "valid.meta.jsonl")


def write_manifest(metas, train_idx, valid_idx):
    """eval_manifest.json beside the build: the valid thread ids (sorted) and the folds that must never reach RL or SFT
    train. r3-valid folds that this build puts in train are listed (the hash split does not keep r3's old split), so a
    later export drops them too."""
    import hashlib
    sha = lambda t: hashlib.sha1(t.encode()).hexdigest()
    folds_now = {leak.fold(m["prompt"]): i in set(valid_idx) for i, m in enumerate(metas)}
    r3 = []
    if os.path.exists(R3_VALID_META):
        r3 = sorted({leak.fold(json.loads(l)["prompt"]) for l in open(R3_VALID_META) if l.strip()})
    manifest = {"split": "sha1(leak.fold(prompt)) % 10 == 0 over connected components of prompt groups joined by shared meta.thread; "
                         "stratified to at least one valid unit per family",
                "valid_threads": LAST_SPLIT["valid_threads"],
                "valid_units_per_family": LAST_SPLIT["valid_units"], "units_per_family": LAST_SPLIT["units"],
                "valid_fold_sha1": sorted(sha(f) for f in LAST_SPLIT["valid_folds"]),
                "r3_valid_folds": len(r3),
                "r3_valid_folds_in_build": sum(f in folds_now for f in r3),
                "r3_valid_folds_still_valid": sum(folds_now.get(f) is True for f in r3),
                "r3_valid_folds_now_train_sha1": sorted(sha(f) for f in r3 if folds_now.get(f) is False)}
    with open(os.path.join(OUT, "eval_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
        f.write("\n")
    return manifest


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
    extract_skips = Counter(str(e[0]).split(":")[0].split(" (")[0] for m in metas for e in [m.get("extract_skipped")] if e)
    thinking_stats = {"think": n_think, "no_think": len(metas) - n_think,
                      "extract": sum(bool(m.get("extract")) for m in metas),
                      "extract_skipped": sum(bool(m.get("extract_skipped")) for m in metas),
                      "extract_skip_reasons": dict(extract_skips.most_common()),
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
        with open(os.path.join(OUT, "partial.jsonl") if os.environ.get("ASK_LORA_OUT") else os.path.join(OUT, "builder", "partial.jsonl"), "w") as f:
            for ex, m in zip(examples, metas):
                f.write(json.dumps({"meta": m, **ex}, ensure_ascii=False) + "\n")
