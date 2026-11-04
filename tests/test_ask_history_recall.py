"""Long-chat recall: turns that miss the verbatim cut become digest lines.

No model, no server, no user data: a temporary session store, the synthetic
20-exchange chat in tests/ask_history_session.py, and the real agent loop with
the synthetic registry from test_model_led_integration.
"""
import copy

import pytest

from service import model_only as mo
from service.agent import loop
from service.memory import context
from service.memory.store import SessionStore
from service.tools import registry
from tests import ask_history_session as session
from tests.test_model_led_integration import call, synthetic  # noqa: F401  (fixture)
from tests.test_model_only_quality import ask, _model_only  # noqa: F401  (fixture)


@pytest.fixture
def chat(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.db")
    monkeypatch.setattr(context, "store", store)
    sid = store.create_session()
    yield store, sid
    store._db.close()


def text(messages):
    return "\n".join(str(m.get("content") or "") for m in messages)


def tokens(messages):
    return sum(context.estimate_tokens(m["content"]) for m in messages)


def history_messages():
    out = []
    for user, assistant, _, _ in session.EXCHANGES:
        out += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
    return out


# --- the digest line ------------------------------------------------------------

def test_digest_line_keeps_the_users_words_and_the_replys_facts():
    assert context.digest_line("user", "My locker code is 4817.") == "- User: My locker code is 4817."
    reply = ("Your manager asked for the deck by Friday. If you want, I can help. "
             "She also said the offsite moved to the Halverson Lodge. Let me know.")
    line = context.digest_line("assistant", reply)
    assert line.startswith("- Assistant: Your manager asked for the deck by Friday.")
    assert "Halverson Lodge" in line and "I can help" not in line and "Let me know" not in line
    assert (context.digest_line("assistant", "**Booked.**\n- Table for 4 at 7:30\n- See you there")
            == "- Assistant: Booked. Table for 4 at 7:30 See you there")
    assert len(context.digest_line("user", "x" * 1000)) <= len("- User: ") + 160
    assert context.digest_line("user", [{"type": "image_url"}]) == ""
    assert context.digest_line("assistant", "   ") == ""


def test_digest_cap_drops_the_oldest_lines_first():
    lines = [f"- User: line {i} " + "x" * 300 for i in range(40)]
    kept = context.cap_digest(lines)
    assert kept == lines[-len(kept):] and 0 < len(kept) < 40
    assert sum(len(line) + 1 for line in kept) <= context.DIGEST_MAX_CHARS


# --- build_messages -------------------------------------------------------------

def test_a_short_chat_is_sent_verbatim_with_no_digest(chat):
    store, sid = chat
    store.add_turn(sid, "user", "My locker code is 4817.")
    store.add_turn(sid, "assistant", "Noted.")
    assert context.build_messages(sid, max_tokens=4000) == [
        {"role": "user", "content": "My locker code is 4817."},
        {"role": "assistant", "content": "Noted."}]


def test_a_long_chat_condenses_older_turns_instead_of_dropping_them(chat):
    store, sid = chat
    session.plant(store, sid)
    budget = 2500
    messages = context.build_messages(sid, max_tokens=budget)
    assert all(session.recall_depths(messages).values())
    assert tokens(messages) <= budget
    digest, *verbatim = messages
    assert context.is_digest(digest) and not any(context.is_digest(m) for m in verbatim)
    # Oldest first, and every digest line is older than every verbatim turn.
    stored = [t["content"] for t in store.turns_from(sid, 0)]
    assert [m["content"] for m in verbatim] == stored[-len(verbatim):]
    assert context.digest_lines(digest)[0] == "- User: " + session.EXCHANGES[0][0]
    assert context.digest_lines(digest)[-1].startswith("- Assistant: ")
    assert len(context.digest_lines(digest)) == len(stored) - len(verbatim)


def test_turns_older_than_keep_messages_reach_the_digest(chat):
    store, sid = chat
    session.plant(store, sid)
    for i in range(5):
        store.add_turn(sid, "user", f"filler question {i}")
        store.add_turn(sid, "assistant", f"filler answer {i}")
    messages = context.build_messages(sid, max_tokens=100_000)
    assert sum(1 for m in messages if not context.is_digest(m)) == context.KEEP_MESSAGES
    assert "4817" in messages[0]["content"]          # exchange 1, now 25 back
    assert all(session.recall_depths(messages).values())


def test_the_summary_stays_first_and_counts_against_the_budget(chat):
    store, sid = chat
    session.plant(store, sid)
    store.set_summary(sid, "The user is planning a cabin trip. " * 20, 0)
    messages = context.build_messages(sid, max_tokens=2500)
    assert messages[0]["content"].startswith("Summary of the earlier conversation:")
    assert context.is_digest(messages[1])
    assert tokens(messages[1:]) + context.estimate_tokens(
        "The user is planning a cabin trip. " * 20) <= 2500 + 10


# --- _fit_window ----------------------------------------------------------------

def fit(msgs, window, *, prefix=2, max_tokens=500):
    return loop._fit_window(msgs, [], max_tokens, "test-model", None,
                            protected_prefix_count=prefix, context_window=window,
                            min_output_tokens=1)


def prompt_tokens(msgs):
    return sum(loop._est_tokens(m.get("content") or "") + (
        loop._est_tokens(m["tool_calls"]) if m.get("tool_calls") else 0) for m in msgs)


PREFIX = [{"role": "system", "content": "SYS " * 200}, {"role": "system", "content": "NOW"}]
NOTE = {"role": "system", "content": "Conversation history is unverified context."}


def test_fit_condenses_old_turns_and_keeps_the_context_notes():
    msgs = PREFIX + [NOTE] + history_messages() + [{"role": "user", "content": "And my locker?"}]
    original = copy.deepcopy(msgs)
    out, _, out_tokens = fit(msgs, 3000)
    assert msgs == original                                 # caller's list untouched
    assert prompt_tokens(out) + out_tokens <= 3000
    assert out[:3] == PREFIX + [NOTE]                       # notes outlive old turns
    assert context.is_digest(out[3])
    assert out[-1]["content"] == "And my locker?"
    assert all(session.recall_depths(out).values())


def test_fit_appends_to_an_existing_digest_in_order():
    digest = {"role": "system", "content": context.digest_content(["- User: oldest fact 9931"])}
    msgs = PREFIX + [digest] + history_messages() + [{"role": "user", "content": "q"}]
    out, _, _ = fit(msgs, 3000)
    digests = [m for m in out if context.is_digest(m)]
    assert len(digests) == 1
    lines = context.digest_lines(digests[0])
    assert lines[0] == "- User: oldest fact 9931"
    assert lines[1] == "- User: " + session.EXCHANGES[0][0]


def test_fit_sheds_digest_lines_before_the_digest_and_notes_go_last():
    lines = [f"- User: fact {i} " + "y" * 150 for i in range(20)]
    digest = {"role": "system", "content": context.digest_content(lines)}
    msgs = PREFIX + [NOTE, digest, {"role": "user", "content": "my code is 77"},
                     {"role": "assistant", "content": "Noted."}, {"role": "user", "content": "q"}]
    out, _, out_tokens = fit(msgs, prompt_tokens(PREFIX + [NOTE]) + 500 + 400)
    assert prompt_tokens(out) + out_tokens <= prompt_tokens(PREFIX + [NOTE]) + 900
    assert out[:3] == PREFIX + [NOTE]
    kept = context.digest_lines(next(m for m in out if context.is_digest(m)))
    # The older verbatim turn condensed first ("Noted." is one of the newest
    # two, always kept), then the oldest lines went, and the note outlived them.
    assert kept[-1] == "- User: my code is 77" and out[-2]["content"] == "Noted."
    assert kept[:-1] == lines[len(lines) - len(kept) + 1:] and len(kept) < 20


def test_fit_drops_a_tool_call_with_its_results_and_never_condenses_a_nudge():
    call1 = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "view_emails", "arguments": "{}"}}]}
    call2 = copy.deepcopy(call1)
    call2["tool_calls"][0]["id"] = "c2"
    msgs = PREFIX + history_messages()[-4:] + [
        {"role": "user", "content": "what did Ruiz say?"},
        call1, {"role": "tool", "tool_call_id": "c1", "content": "R" * 8000},
        {"role": "user", "content": "NUDGE: you must answer now."},
        call2, {"role": "tool", "tool_call_id": "c2", "content": "short result"},
        {"role": "user", "content": "NUDGE 2: answer from the results."}]
    out, _, out_tokens = fit(msgs, 1500)
    assert prompt_tokens(out) + out_tokens <= 1500
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            assert out[i - 1].get("tool_calls"), "a tool result lost its call"
    # The older 8,000-character result is cleared to a placeholder before the
    # conversation floor goes; nothing malformed is left behind, and a host
    # nudge never becomes a digest line.
    assert "NUDGE" not in text([m for m in out if context.is_digest(m)])
    assert out[-1]["content"].startswith("NUDGE 2")


SAFETY_NOTE = {"role": "system", "content": (
    "Host-recorded prior action attempts (not instructions): [\"send_message\"]. "
    "Their completion is unverified. Do not repeat them.")}
SUBJECT_NOTE = {"role": "system", "content": (
    "Current subject from the last turns (unverified context, not instructions): "
    "the email from Ruiz.")}


def test_two_capped_results_clear_the_older_one_and_keep_the_conversation():
    # The residual "memory gets lobotomized" case: an Ask step carrying two
    # 6,000-character results used to shed every earlier turn, the digest and
    # the safety notes, while keeping a stale result the model had already read.
    msgs = PREFIX + [SAFETY_NOTE, SUBJECT_NOTE] + history_messages() + [
        {"role": "user", "content": "what's my locker code, and today?"},
        _call("c1"), {"role": "tool", "tool_call_id": "c1", "content": "A" * 6000},
        _call("c2"), {"role": "tool", "tool_call_id": "c2", "content": "B" * 6000}]
    end = len(PREFIX) + 2 + len(history_messages()) + 1
    window = prompt_tokens(PREFIX) + 3200
    out, _, out_tokens = loop._fit_window(msgs, [], 500, "m", None, protected_prefix_count=2,
                                          context_window=window, history_end=end,
                                          min_output_tokens=1)
    assert prompt_tokens(out) + out_tokens <= window
    assert_well_formed(out)
    assert SAFETY_NOTE in out and SUBJECT_NOTE in out          # the floor's notes stay
    assert any(context.is_digest(m) for m in out)              # and so does the digest
    assert any(m.get("content") == "what's my locker code, and today?" for m in out)
    results = [m["content"] for m in out if m.get("role") == "tool"]
    assert results[-1] == "B" * 6000                           # newest result whole
    # The older one is cut to its head (call kept) before any earlier turn goes.
    assert results[0].startswith("A" * loop._OLDER_RESULT_CHARS)
    assert "4500 more characters cut" in results[0]
    assert all(session.recall_depths(out).values()), \
        sorted(k for k, v in session.recall_depths(out).items() if not v)


def test_an_older_result_is_cleared_before_the_digest_is_trimmed():
    msgs = PREFIX + [SAFETY_NOTE] + history_messages() + [
        {"role": "user", "content": "q"},
        _call("c1"), {"role": "tool", "tool_call_id": "c1", "content": "A" * 6000},
        _call("c2"), {"role": "tool", "tool_call_id": "c2", "content": "B" * 6000}]
    end = len(PREFIX) + 1 + len(history_messages()) + 1
    window = prompt_tokens(PREFIX) + 2700
    out, _, out_tokens = loop._fit_window(msgs, [], 500, "m", None, protected_prefix_count=2,
                                          context_window=window, history_end=end,
                                          min_output_tokens=1)
    assert prompt_tokens(out) + out_tokens <= window
    assert_well_formed(out)
    results = [m["content"] for m in out if m.get("role") == "tool"]
    assert results[0].startswith(loop._CLEARED_RESULT_HEAD) and "6000 characters" in results[0]
    assert SAFETY_NOTE in out
    digest = next(m for m in out if context.is_digest(m))
    assert len(context.digest_lines(digest)) >= loop._DIGEST_FLOOR_LINES


def test_the_floor_still_gives_way_when_nothing_else_is_left():
    # Under extreme pressure the floor is shed too (after the cleared result),
    # rather than the request or the newest result.
    msgs = PREFIX + [SAFETY_NOTE] + history_messages()[-4:] + [
        {"role": "user", "content": "q"},
        _call("c1"), {"role": "tool", "tool_call_id": "c1", "content": "A" * 6000},
        _call("c2"), {"role": "tool", "tool_call_id": "c2", "content": "B" * 2000}]
    end = len(PREFIX) + 1 + 4 + 1
    window = prompt_tokens(PREFIX) + 700
    out, _, out_tokens = loop._fit_window(msgs, [], 100, "m", None, protected_prefix_count=2,
                                          context_window=window, history_end=end,
                                          min_output_tokens=1)
    assert prompt_tokens(out) + out_tokens <= window
    assert_well_formed(out)
    assert any(m.get("content") == "q" for m in out)
    assert out[-1]["content"] == "B" * 2000


def test_kept_note_prefixes_match_the_notes_their_writers_send():
    import inspect
    from service import main
    from service.memory import subject
    source = inspect.getsource(main) + inspect.getsource(subject)
    for prefix in loop._KEPT_NOTE_PREFIXES:
        assert prefix in source, prefix


# --- an Ask turn end to end -----------------------------------------------------

def long_upcoming(synthetic, monkeypatch):
    async def read(**args):
        return "Synthetic calendar: " + "Standup at 10:30 with the team. " * 190
    tool = registry.REGISTRY["get_upcoming"]
    monkeypatch.setitem(registry.REGISTRY, "get_upcoming", registry.Tool(
        tool.name, tool.description, tool.parameters, tool.category, read))


def test_an_ask_turn_recalls_twenty_exchanges_back_before_and_after_a_tool(synthetic, monkeypatch):
    long_upcoming(synthetic, monkeypatch)
    client, _, _ = ask([[call("get_upcoming", days=1)], "Your locker code is 4817."],
                       history=history_messages(), prompt="what's my locker code, and today?")
    for request in client.requests[:2]:
        assert all(session.recall_depths(request["messages"]).values()), \
            sorted(k for k, v in session.recall_depths(request["messages"]).items() if not v)
        estimate = loop._est_tokens(request["tools"]) + prompt_tokens(request["messages"])
        assert estimate <= mo.ASK_PROMPT_BUDGET + mo.ASK_HISTORY_RESERVE + 200
    assert any(m.get("role") == "tool" for m in client.requests[1]["messages"])


def test_a_chat_that_just_fits_is_not_condensed(chat):
    store, sid = chat
    for i in range(6):
        store.add_turn(sid, "user" if i % 2 == 0 else "assistant", "x" * 76 + f" {i}")
    total = tokens(context.build_messages(sid, max_tokens=100_000))
    for slack in (0, 5, 20, 40):
        messages = context.build_messages(sid, max_tokens=total + slack)
        assert len(messages) == 6 and not any(context.is_digest(m) for m in messages)
    # One token short: the oldest turn condenses, and the result still fits.
    squeezed = context.build_messages(sid, max_tokens=total - 1)
    assert context.is_digest(squeezed[0]) and tokens(squeezed) <= total - 1


def test_the_digest_gives_way_when_only_the_newest_turn_fits(chat):
    store, sid = chat
    session.plant(store, sid)
    newest = context.estimate_tokens(session.EXCHANGES[-1][1])
    messages = context.build_messages(sid, max_tokens=newest + 60)
    assert messages[-1]["content"] == session.EXCHANGES[-1][1]
    assert tokens(messages) <= newest + 60


def test_digest_keeps_identifiers_intact():
    line = context.digest_line("user", "## wifi is blue_fox#42, mail jane_doe@x.com, a|b `code`")
    assert line == "- User: wifi is blue_fox#42, mail jane_doe@x.com, a|b code"


def _call(cid):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "get_upcoming", "arguments": "{}"}}]}


def assert_well_formed(out):
    """Every tool result follows its call, and every call keeps all its results."""
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            j = i
            while out[j - 1].get("role") == "tool":
                j -= 1
            assert out[j - 1].get("tool_calls"), "a tool result lost its call"
        if m.get("tool_calls"):
            ids = {c["id"] for c in m["tool_calls"]}
            got = set()
            j = i + 1
            while j < len(out) and out[j].get("role") == "tool":
                got.add(out[j].get("tool_call_id"))
                j += 1
            assert got == ids or j == len(out), "a tool call lost a result"


def test_a_nudge_after_tool_traffic_never_orphans_the_result():
    # Release Auditor repro (PR #442 at 137fba8f).
    msgs = [{"role": "system", "content": "S" * 400}, {"role": "system", "content": "now"},
            {"role": "system", "content": "N" * 400},
            {"role": "user", "content": "what is on my calendar?"},
            _call("c1"), {"role": "tool", "tool_call_id": "c1", "content": "R" * 2400},
            {"role": "user", "content": "Now answer in plain words."}]
    out, _, _ = loop._fit_window(msgs, [], 100, "m", None, protected_prefix_count=2,
                                 context_window=900, history_end=4)
    assert_well_formed(out)
    assert any(m.get("tool_calls") for m in out)


def test_a_nudge_before_any_tool_call_is_never_condensed():
    msgs = PREFIX + history_messages()[-6:] + [
        {"role": "user", "content": "what did Ruiz say?"},
        {"role": "assistant", "content": "Ruiz is a plumber."},
        {"role": "user", "content": "NUDGE: look it up with a tool."}]
    end = len(PREFIX) + 7
    out, _, _ = loop._fit_window(msgs, [], 500, "m", None, protected_prefix_count=2,
                                 context_window=prompt_tokens(PREFIX) + 560, history_end=end)
    digest = text([m for m in out if context.is_digest(m)])
    # The request stays verbatim even though the nudge is now the last user
    # message: it is protected by identity (history_end), not position.
    assert "what did Ruiz say?" not in digest
    assert "NUDGE" not in digest and "Ruiz is a plumber" not in digest
    assert [m["content"] for m in out[-3:]] == [
        "what did Ruiz say?", "Ruiz is a plumber.", "NUDGE: look it up with a tool."]


def test_fitting_never_leaves_a_malformed_tool_transcript():
    import random
    rng = random.Random(442)
    for _ in range(3000):
        msgs = [{"role": "system", "content": "S" * rng.randint(10, 800)},
                {"role": "system", "content": "now"}]
        if rng.random() < 0.5:
            msgs.append({"role": "system", "content": "note " * rng.randint(1, 80)})
        for k in range(rng.randint(0, 6)):
            msgs.append({"role": "user" if k % 2 == 0 else "assistant",
                         "content": "h" * rng.randint(1, 900)})
        msgs.append({"role": "user", "content": "the request"})
        end = len(msgs)
        for c in range(rng.randint(0, 3)):
            calls = [f"c{c}_{n}" for n in range(rng.randint(1, 2))]
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": "t", "arguments": "{}"}}
                for cid in calls]})
            msgs += [{"role": "tool", "tool_call_id": cid, "content": "r" * rng.randint(1, 3000)}
                     for cid in calls]
            if rng.random() < 0.5:
                msgs.append({"role": "user", "content": "NUDGE answer now"})
        original = copy.deepcopy(msgs)
        out, _, _ = loop._fit_window(msgs, [], rng.choice([100, 500, 2000]), "m", None,
                                     protected_prefix_count=2,
                                     context_window=rng.randint(300, 4000),
                                     history_end=end if rng.random() < 0.7 else None,
                                     min_output_tokens=1)
        assert msgs == original
        assert_well_formed(out)
        assert "NUDGE" not in text([m for m in out if context.is_digest(m)])
