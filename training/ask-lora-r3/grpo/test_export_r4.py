"""Tests for export_prompts.py's round-4 pieces (labels and mix); pure python, no tokenizer.
Run: python -m pytest -q test_export_r4.py"""
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_prompts as X  # noqa: E402
import reward as W  # noqa: E402

CLOCK = {"role": "system", "content": "\nThe current date and time is Tuesday, April 6, 2027 at 10:37 PM."}
RULES = {"role": "system", "content": "RULES " * 2000}


def call_msg(name, args, cid="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": args}}]}


def example(history_calls, target, user="text Dad that I'm running late"):
    msgs = [RULES, CLOCK, {"role": "user", "content": user}]
    for i, (name, args, result) in enumerate(history_calls):
        msgs += [call_msg(name, args, f"c{i}"), {"role": "tool", "tool_call_id": f"c{i}", "content": result}]
    return {"messages": msgs + [target], "tools": []}


def meta(step, family="compose_r3", turn="compose_r3-001", writes=("send_message",), **kw):
    return {"id": f"{turn}-s{step}", "family": family, "step": step, "turn_writes": list(writes), "effects": [], **kw}


def test_turn_reads_are_the_reads_before_the_first_write():
    steps = [(0, ["get_tool_schemas"]), (1, ["lookup_contact"]), (2, ["get_upcoming"]), (3, ["send_message"]), (4, [])]
    assert X.turn_reads(steps, ["send_message"]) == ["lookup_contact", "get_upcoming"]
    assert X.turn_reads(list(reversed(steps)), ["send_message"]) == ["lookup_contact", "get_upcoming"]     # any order in
    assert X.turn_reads(steps, []) == []                                   # a turn with no write has nothing to protect
    assert X.turn_reads([(0, ["send_message"])], ["send_message"]) == []   # a write with no read before it
    assert X.turn_reads([(0, ["lookup_contact"]), (1, ["lookup_contact", "draft_email"])], ["draft_email"]) == ["lookup_contact"]
    assert X.turn_reads([(0, ["lookup_contact"]), (1, ["summarize_emails"])], ["send_email"]) == []        # the write never happens


def test_classify_r4_labels_requires_read_and_reads_done():
    turn = [(0, ["get_tool_schemas"]), (1, ["lookup_contact"]), (2, ["send_message"]), (3, [])]
    turns = {"compose_r3-001": turn}
    ex0 = example([], call_msg("lookup_contact", {"name": "Dad"}))
    lab0 = X.classify(ex0, meta(1), 4, turns, ["lookup_contact", "send_message", "summarize_emails"])
    assert lab0["round"] == 4 and lab0["requires_read"] == ["lookup_contact"] and lab0["reads_done"] == []
    assert not lab0["narrate"] and "ground" not in lab0
    ex1 = example([("lookup_contact", {"name": "Dad"}, "Dad: +1 555 0100")], call_msg("send_message", {"to": "Dad", "text": "late"}))
    lab1 = X.classify(ex1, meta(2), 4, turns, [])
    assert lab1["requires_read"] == ["lookup_contact"] and lab1["reads_done"] == ["lookup_contact"]
    # the reward reads the label exactly as written: early send -> -2, send after the read -> 0
    send = "\n<channel|><|tool_call>call:send_message{text:<|\"|>late<|\"|>}<tool_call|><|tool_response>"
    assert W.score(send, {**lab0, "forced": True}, rnd=4).parts["early_write"] == -2.0 or lab0["forbidden"]
    assert W.score(send, {**lab1, "forced": True}, rnd=4).parts["early_write"] == 0.0


def test_classify_r4_narrate_chat_ground_and_tool_names():
    final = {"role": "assistant", "content": "Dad's number is +1 555 0100."}
    ex = example([("lookup_contact", {"name": "Dad"}, "Dad: +1 555 0100")], final, user="what's Dad's number")
    lab = X.classify(ex, meta(2, writes=()), 4, {}, ["lookup_contact", "get_upcoming"])
    assert lab["narrate"] and not lab["chat"] and lab["requires_read"] == []
    assert "+1 555 0100" in lab["ground"] and "what's Dad's number" in lab["ground"] and '"name": "Dad"' in lab["ground"]
    assert "10:37 PM" in lab["ground"] and "RULES" not in lab["ground"]               # the clock yes, the long rules prompt no
    assert lab["tool_names"] == ["lookup_contact", "get_upcoming"]
    assert W.score("\n<channel|>" + final["content"] + "<turn|>", {**lab, "forced": True}, rnd=4).parts["ungrounded"] == 0.0
    chat = X.classify(example([], {"role": "assistant", "content": "That sounds lovely."}, user="i had a good day"),
                      meta(0, family="chat_voice_r3", writes=()), 4, {}, [])
    assert chat["chat"] and not chat["narrate"] and "ground" not in chat
    # a round-3 export is unchanged: none of the new fields
    old = X.classify(ex, meta(2, writes=()))
    assert not ({"round", "narrate", "chat", "ground", "requires_read", "reads_done", "tool_names"} & set(old))


def item(i, family, narrate=False, facts=()):
    return {"id": f"{family}-{i}", "family": family, "prompt": "p", "weight": 1.0, "_tokens": 100,
            "label": {"narrate": narrate, "facts": list(facts), "step": "final" if narrate else "call"}}


def pool():
    items = []
    for fam, n_nar, n_dec in (("compose_r3", 300, 300), ("safe_restraint", 200, 200), ("gather_then_send_r4", 100, 100),
                              ("restraint_r4", 100, 100), ("reminders_r3", 200, 200), ("mail_r3", 400, 400), ("device_r3", 300, 300), ("chat_voice_r3", 200, 200)):
        items += [item(f"n{i}", fam, True, ["6 PM"]) for i in range(n_nar)] + [item(f"d{i}", fam) for i in range(n_dec)]
    return items


def test_r4_mix_shares():
    rng = random.Random(1)
    pics = [dict(item(f"p{i}", "picture_turn_r3"), weight=1.0) for i in range(30)]
    n_all, n_mm = 1488, 223
    rows, st = X.r4_text_rows(pool(), pics, n_all - n_mm, n_all, rng)
    assert len(rows) == n_all - n_mm and st["short"] == {}
    b = Counter(X.r4_bucket(r) for r in rows)
    assert abs(b["narrate"] / n_all - 0.35) < 0.01
    assert abs(b["compose"] / n_all - 0.20) < 0.01
    assert abs((b["other"]) / n_all - 0.30) < 0.02
    # narrate is any family; compose holds only decision steps of the safety families
    assert all(not r["label"]["narrate"] for r in rows if X.r4_bucket(r) == "compose")
    assert {r["family"] for r in rows if X.r4_bucket(r) == "compose"} <= X.COMPOSE_SAFETY_FAMILIES
    assert not any(r["family"] in X.COMPOSE_SAFETY_FAMILIES for r in rows if X.r4_bucket(r) == "other")
    assert sum(r["family"] == "picture_turn_r3" for r in rows) == 30             # the picture-turn rows ride along


def test_r4_mix_upweights_the_named_families():
    rng = random.Random(2)
    rows, _ = X.r4_text_rows(pool(), [], 1265, 1488, rng)
    comp = Counter(r["family"] for r in rows if X.r4_bucket(r) == "compose")
    pools = Counter(i["family"] for i in pool() if i["family"] in X.COMPOSE_SAFETY_FAMILIES and not i["label"]["narrate"])
    rate = {f: comp[f] / pools[f] for f in pools}
    # inside the compose bucket the named families are drawn at a higher rate than the one at weight 1.0
    assert min(rate["safe_restraint"], rate["gather_then_send_r4"], rate["restraint_r4"]) > rate["reminders_r3"] * 1.5
    assert rate["compose_r3"] > rate["reminders_r3"]
    assert X.r4_weight(item(1, "safe_restraint")) == 3.0 and X.r4_weight(item(1, "compose_r3")) == 2.5
    assert X.r4_weight(item(1, "mail_r3", True, ["6 PM"])) == X.R4_FACT_BONUS > X.r4_weight(item(1, "mail_r3", True))


def test_r4_mix_copies_are_capped_and_a_thin_bucket_is_reported():
    rng = random.Random(3)
    thin = [item(i, "mail_r3", True, ["6 PM"]) for i in range(10)] + [item(i, "device_r3") for i in range(2000)]
    rows, st = X.r4_text_rows(thin, [], 1265, 1488, rng)
    assert Counter(r["id"] for r in rows).most_common(1)[0][1] <= X.MAX_COPIES
    q = st["quota"]
    assert st["got"]["narrate"] == X.MAX_COPIES * 10 and st["short"]["narrate"] == q["narrate"] - 30
    assert st["got"]["compose"] == 0 and st["short"]["compose"] == q["compose"]       # no safety-family rows in this pool


def test_r4_defaults_point_at_the_r4_dirs():
    root = Path(X.__file__).resolve().parent.parent.parent / "ask-lora-r4"
    assert root.name == "ask-lora-r4" and root.parent.name == "training"
