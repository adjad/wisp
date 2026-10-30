"""Build the dataset: run every scenario through /agent, keep one example per
model step (the captured request + the ideal response), split 90/10 by family."""
from __future__ import annotations

import copy
import json
import os
import random
import sys
import traceback
from collections import Counter, defaultdict

import harness as H
import leak
import scen

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
            try:
                policy, events, final = H.run_scenario(sc.prompt, sc.steps, sc.world, sc.when, profile=sc.profile)
            except Exception as exc:  # noqa: BLE001
                failures.append((sid, sc.prompt, f"harness: {exc!r}"))
                continue
            errs = [e for e in events if e.get("type") == "error"]
            want = sc.steps[-1].text if isinstance(sc.steps[-1], H.Final) else None
            if policy.idx != len(policy.steps) or errs or len(policy.requests) != len(policy.responses):
                failures.append((sid, sc.prompt, f"incomplete: idx={policy.idx}/{len(policy.steps)} "
                                 f"reqs={len(policy.requests)} errs={errs[:1]} final={final!r:.120}"))
                continue
            if want is not None and (final or "").strip() != want.strip():
                failures.append((sid, sc.prompt, f"final text changed by host: {final!r:.200} vs {want!r:.80}"))
                continue
            sent = [(c["function"]["name"], json.loads(c["function"]["arguments"]))
                    for r in policy.responses if r["kind"] == "call"
                    for c in r["message"]["tool_calls"]]
            bad_sent = [(c["function"]["name"], json.loads(c["function"]["arguments"]))
                        for r in policy.responses if r["kind"] == "bad"
                        for c in r["message"]["tool_calls"]]
            ran = [(n, a) for n, a in H.CURRENT["calls"] if (n, a) not in bad_sent]

            def same(s_, r_):
                if s_[0] != r_[0]:
                    return False
                if any(r_[1].get(k) != v for k, v in s_[1].items()):
                    return False
                extra = {k: v for k, v in r_[1].items() if k not in s_[1]}
                return all(v in ("", None, False, "start", 0) for v in extra.values())
            if len(sent) != len(ran) or not all(same(a, b) for a, b in zip(sent, ran)):
                failures.append((sid, sc.prompt, f"host rewrote tool args: sent={sent} ran={ran}"))
                continue
            per_family_scen[family] += 1
            for i, (req, resp) in enumerate(zip(policy.requests, policy.responses)):
                if resp["kind"] == "bad":
                    continue
                ex = {"messages": sanitize(clean_history(req["messages"])) + [target_message(resp)],
                      "tools": sanitize(copy.deepcopy(req.get("tools") or []))}
                examples.append(ex)
                metas.append({"id": f"{sid}-s{i}", "family": family, "paraphrase_of": sc.paraphrase_of,
                              "notes": sc.notes, "step": i, "steps_in_turn": len(policy.requests),
                              "target_kind": resp["kind"], "prompt": sc.prompt,
                              "clock": sc.when.strftime("%a %Y-%m-%d %H:%M"),
                              "tool_choice": req.get("tool_choice"),
                              "chat_template_kwargs": req.get("chat_template_kwargs")})
    return examples, metas, failures, per_family_scen


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
    with open(os.path.join(OUT, "all.jsonl"), "w") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    with open(os.path.join(OUT, "meta.jsonl"), "w") as f:
        for m in metas:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
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
    for name, idxs in (("train", train_idx), ("valid", valid_idx)):
        with open(os.path.join(OUT, f"{name}.jsonl"), "w") as f:
            for i in sorted(idxs):
                f.write(json.dumps(examples[i], ensure_ascii=False) + "\n")
        with open(os.path.join(OUT, f"{name}.meta.jsonl"), "w") as f:
            for i in sorted(idxs):
                f.write(json.dumps(metas[i], ensure_ascii=False) + "\n")
    return train_idx, valid_idx


def write_stats(metas, train_idx, valid_idx):
    """stats.json: the counts the README quotes, plus the code head they came from."""
    import subprocess
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=H.boot.REPO).stdout.strip()
    turns = {m["id"].rsplit("-s", 1)[0]: m for m in metas}
    split_of = {i: "train" for i in train_idx} | {i: "valid" for i in valid_idx}
    fam = defaultdict(lambda: Counter())
    for i, m in enumerate(metas):
        fam[m["family"]]["examples"] += 1
        fam[m["family"]][split_of[i]] += 1
        fam[m["family"]][m["target_kind"]] += 1
    for t, m in turns.items():
        fam[m["family"]]["turns"] += 1
    stats = {"code_head": head, "examples": len(metas), "turns": len(turns),
             "train": len(train_idx), "valid": len(valid_idx),
             "by_target": dict(Counter(m["target_kind"] for m in metas)),
             "paraphrased_turns": sum(1 for m in turns.values() if m.get("paraphrase_of")),
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
    else:
        with open(os.path.join(OUT, "builder", "partial.jsonl"), "w") as f:
            for ex, m in zip(examples, metas):
                f.write(json.dumps({"meta": m, **ex}, ensure_ascii=False) + "\n")
