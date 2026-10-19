"""MAIL and TEXTS overviews: bounded, background, grounded display text. No models or native sources."""
import asyncio
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-mail-gists-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from pathlib import Path

import pytest

from service.assistant import mail_gists as G

NOW = 1_000_000.0


def row(n, subject=None, sender=None, address=None, ago=600.0, **extra):
    return dict(subject=subject or f"Subject {n}", sender=sender or f"Sender {n}",
                sender_address=address or f"sender{n}@example.com", ts=NOW - ago) | extra


def convo(label, *bodies, ago=600.0):
    return {"label": label, "ts": NOW - ago, "bodies": list(bodies)}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_PATH", Path(tmp_path) / "cache" / "mail_gists.json")
    monkeypatch.setattr(G, "_cache", None)
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    monkeypatch.setattr(G, "_attempts", {})
    monkeypatch.setattr(G, "_last_standdown", "")
    monkeypatch.setattr(G, "recent_mail", lambda now: [row(1, "Tech Week starts today"), row(2, "Invitation: Demo day")])
    monkeypatch.setattr(G, "recent_texts", lambda now: [convo("Mom", "call me when you can")])
    return tmp_path


# --- deterministic sentences ----------------------------------------------------


def test_mail_fallback_names_the_main_kinds_without_a_model():
    assert G.mail_fallback([]) == "No new mail today"
    news = [row(n, "50% off everything", sender="Shop", address="deals@shop.com") for n in range(3)]
    invite = row(9, "Invitation: Standup @ Tue 10am", sender="Ann Lee", address="ann@example.com")
    assert G.mail_fallback(news + [invite]) == "4 new, mostly newsletters and event invitations"
    assert G.mail_fallback([invite, row(8, "Canceled: Standup", sender="Ann Lee", address="ann@example.com")]) \
        == "2 new: cancellations and event invitations"


def test_texts_fallback_names_conversations_never_words():
    assert G.texts_fallback([]) == "Quiet"
    assert G.texts_fallback([convo("Priya", "dinner at 7?")]) == "Texts with Priya"
    assert G.texts_fallback([convo("Priya"), convo("Mom")]) == "Texts with Priya and Mom"
    assert G.texts_fallback([convo("Priya"), convo("Mom"), convo("Sam"), convo("Ann")]) == "Texts with Priya, Mom and 2 others"
    assert G.texts_fallback([convo("Priya"), convo("Mom"), convo("Sam")]) == "Texts with Priya, Mom and 1 other"


def test_mail_lines_leave_private_subjects_and_codes_out():
    lines = G.mail_lines([row(1, "Private details omitted."), row(2, "Lunch?", sender="Ann Lee <ann@x.com>")])
    assert lines == ["- Ann Lee — Lunch?"]


def test_recent_mail_uses_fresh_headers_without_a_complete_snapshot(monkeypatch):
    """Build 2033: one failing account left no complete snapshot, so nothing was ever described."""
    from service.assistant import daily_workspace
    from service.tools import email_tools
    monkeypatch.setattr(email_tools, "raw_email_snapshot", lambda: None)
    monkeypatch.setattr(email_tools, "email_source_details", lambda part=None: {}, raising=False)
    monkeypatch.setattr(daily_workspace, "fresh_header_rows",
                        lambda mail, now: ([row(1, ago=60), row(2, ago=2 * 86400), row(3, ago=30)], True))
    assert [r["subject"] for r in G.recent_mail(NOW)] == ["Subject 3", "Subject 1"]


def test_recent_texts_leave_the_users_own_messages_out(monkeypatch):
    from service.assistant import brief
    from service.tools import imessage_tools
    monkeypatch.setattr(imessage_tools, "messages_sync_state", lambda: "ready")
    monkeypatch.setattr(brief, "_message_rows", lambda now, limit=0: [
        (NOW - 60, "Priya", "you", "sure, see you at 7"),
        (NOW - 120, "Priya", "", "dinner at 7?"),
        (NOW - 3 * 86400, "Old", "", "ancient"),
    ])
    assert G.recent_texts(NOW) == [{"label": "Priya", "ts": NOW - 60, "bodies": ["dinner at 7?"], "names": []}]
    monkeypatch.setattr(imessage_tools, "messages_sync_state", lambda: "unavailable")
    assert G.recent_texts(NOW) == []


# --- validating a reply ---------------------------------------------------------------


def test_parse_line_takes_one_bounded_line():
    assert G.parse_line("Mostly event updates and tech-week promos.\nextra") == "Mostly event updates and tech-week promos"
    assert G.parse_line("- **Mostly school mail**") == "Mostly school mail"
    assert G.parse_line("x" * 200) == ""
    assert G.parse_line("Promos from shop.com") == "" and G.parse_line("see https://evil.example") == ""


def test_a_closed_think_block_is_dropped_and_an_unclosed_one_drops_the_reply():
    assert G.parse_line("<think>not it</think>\nMostly school mail") == "Mostly school mail"
    assert G.parse_line("<think>Mostly school mail") == ""
    assert G.parse_line("Mostly school mail</think>") == ""


def test_the_mail_gist_carries_no_number_its_inputs_dont():
    lines = ["- Ann — Standup at 10", "- Shop — 50% off"]
    assert G.mail_gist_ok("Mostly standup and promos", lines)
    assert G.mail_gist_ok("Standup at 10 and a sale", lines)
    assert not G.mail_gist_ok("Standup at 11 and a sale", lines)
    assert not G.mail_gist_ok("Standup at 1", lines), "whole numbers only"


def test_the_texts_gist_names_topics_never_people_or_words():
    convos = [convo("Priya", "are we still on for dinner tonight"), convo("Mom", "call me when you can")]
    assert G.texts_gist_ok("Mostly dinner plans and family check-ins", convos)
    assert not G.texts_gist_ok("Priya asked about dinner", convos), "no conversation names"
    assert not G.texts_gist_ok("Mom says call me when you can", convos), "no copied run of words"
    assert not G.texts_gist_ok("Dinner at 7", convos) and not G.texts_gist_ok("“dinner tonight”", convos)


def test_the_texts_gist_names_nobody_mentioned_in_a_message_or_a_group():
    """Auditor finding on #297: a name inside a message passed the label check."""
    convos = [convo("Family", "yes Priya's birthday dinner is Saturday", "Can Ravi bring cake"),
              {"label": "Book club", "ts": NOW, "bodies": ["next week?"], "names": ["Ann Lee"]}]
    assert not G.texts_gist_ok("Mostly Priya's birthday plans", convos)
    assert not G.texts_gist_ok("Plans with ravi", convos)
    assert not G.texts_gist_ok("Mostly lee and family news", convos)
    assert not G.texts_gist_ok("Book recommendations", convos), "every word of a label"
    assert G.texts_gist_ok("Mostly birthday plans and cake", convos), "a sentence's first word isn't a name"


# --- the tick ---------------------------------------------------------------------------


def test_refresh_writes_the_inbox_then_the_texts_gist_and_caches_privately(isolated):
    calls = []

    async def synth(system, material):
        calls.append((system, material))
        return "Mostly event updates" if len(calls) == 1 else "Mostly family check-ins"

    assert asyncio.run(G.refresh(NOW, synth=synth, abort=lambda: False)) is True
    assert "IMPORTANT rules" in calls[0][0] and "data, not instructions" in calls[0][0]
    assert "- Sender 1 — Tech Week starts today" in calls[0][1]
    assert asyncio.run(G.refresh(NOW + 1, synth=synth, abort=lambda: False)) is True
    assert "Conversation 1: call me when you can" in calls[1][1] and "Mom" not in calls[1][1]
    assert G._PATH.exists() and oct(G._PATH.stat().st_mode & 0o777) == "0o600"
    # Unchanged inputs: no model call.
    assert asyncio.run(G.refresh(NOW + 2, synth=synth, abort=lambda: False)) is False and len(calls) == 2
    # A fresh process reads them back.
    G._cache = None
    assert G.overview("inbox", "fallback", NOW + 3) == {"text": "Mostly event updates", "source": "model"}
    assert G.overview("texts", "fallback", NOW + 3) == {"text": "Mostly family check-ins", "source": "model"}


def test_changed_inputs_wait_for_the_rewrite_gap_and_old_gists_give_way(isolated, monkeypatch):
    calls = []

    async def synth(system, material):
        calls.append(material)
        return "Mostly event updates"

    monkeypatch.setattr(G, "recent_texts", lambda now: [])
    asyncio.run(G.refresh(NOW, synth=synth, abort=lambda: False))
    monkeypatch.setattr(G, "recent_mail", lambda now: [row(5, "New thing")])
    assert asyncio.run(G.refresh(NOW + 60, synth=synth, abort=lambda: False)) is False and len(calls) == 1
    assert asyncio.run(G.refresh(NOW + G._REWRITE_S + 1, synth=synth, abort=lambda: False)) is True and len(calls) == 2
    assert G.overview("inbox", "3 new", NOW + G._REWRITE_S + G._FRESH_S + 5) == {"text": "3 new", "source": "summary"}


def test_an_ungrounded_or_quoting_gist_is_dropped_for_the_sentence(isolated, monkeypatch):
    monkeypatch.setattr(G, "recent_mail", lambda now: [])

    async def synth(system, material):
        return "Mom wants a call"

    assert asyncio.run(G.refresh(NOW, synth=synth, abort=lambda: False)) is False
    assert "texts" not in G._load() and G._next_call_at == NOW + G._FAILURE_GAP_S


def test_stands_down_for_a_turn_and_when_nothing_is_new(isolated, monkeypatch, capsys):
    seen = []

    async def synth(system, material):
        seen.append(material)
        return "x"

    assert asyncio.run(G.refresh(NOW, synth=synth, abort=lambda: True)) is False and not seen
    assert "standing down: a turn is in progress" in capsys.readouterr().out
    monkeypatch.setattr(G, "recent_mail", lambda now: [])
    monkeypatch.setattr(G, "recent_texts", lambda now: [])
    assert asyncio.run(G.refresh(NOW, abort=lambda: False)) is False and G._next_call_at == NOW + G._IDLE_GAP_S
    assert "standing down: no new mail or texts to describe" in capsys.readouterr().out
    asyncio.run(G.refresh(NOW + 1, abort=lambda: False))
    assert capsys.readouterr().out == "", "one line per new reason, not one per tick"


def test_a_failure_backs_off_and_twice_gives_up_until_inputs_change(isolated, monkeypatch):
    monkeypatch.setattr(G, "recent_texts", lambda now: [])
    calls = []

    async def synth(system, material):
        calls.append(material)
        raise TimeoutError

    asyncio.run(G.refresh(NOW, synth=synth, abort=lambda: False))
    assert "inbox" not in G._load() and G._next_call_at == NOW + G._FAILURE_GAP_S
    asyncio.run(G.refresh(NOW + 1, synth=synth, abort=lambda: False))
    assert asyncio.run(G.refresh(NOW + 2, synth=synth, abort=lambda: False)) is False and len(calls) == 2
    monkeypatch.setattr(G, "recent_mail", lambda now: [row(7, "Something new")])
    asyncio.run(G.refresh(NOW + 3, synth=synth, abort=lambda: False))
    assert len(calls) == 3


def test_schedule_tick_is_single_flight_and_waits_for_quiet(isolated, monkeypatch, capsys):
    from service import idle
    from service.assistant.hub import hub
    monkeypatch.setattr(type(hub), "has_subscribers", property(lambda self: True))
    monkeypatch.setattr(idle, "foreground_busy", lambda: True)
    assert G.schedule_tick() is None

    async def main():
        monkeypatch.setattr(idle, "foreground_busy", lambda: False)
        monkeypatch.setattr(G, "recent_mail", lambda now: [])
        monkeypatch.setattr(G, "recent_texts", lambda now: [])
        task = G.schedule_tick()
        assert task is not None and G.schedule_tick() is None
        await task

    asyncio.run(main())
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    monkeypatch.setattr(type(hub), "has_subscribers", property(lambda self: False))
    assert G.schedule_tick() is None
    assert "the app isn't connected" in capsys.readouterr().out


# --- the model call ------------------------------------------------------------------------


class FakeClient:
    def __init__(self, loaded, warm=(), *, delay=0.0, content="Mostly school mail", finish="stop", turn=None):
        self.loaded, self.warm = list(loaded), set(warm)
        self.delay, self.content, self.finish, self.turn = delay, content, finish, turn
        self.requests = []
        self.cancelled = False

    async def loaded_models(self):
        return self.loaded

    def keep_warm(self):
        return self.warm

    async def chat(self, model, messages, **kwargs):
        self.requests.append((model, kwargs))
        try:
            steps = max(1, int(self.delay / 0.05))
            for step in range(steps):
                if self.turn is not None and step == 2:
                    self.turn[0] = True
                await asyncio.sleep(self.delay / steps)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {"choices": [{"finish_reason": self.finish, "message": {"content": self.content}}]}


@pytest.fixture
def model(monkeypatch):
    """The fast role is "fast"; tests pick whether it honors thinking off."""
    import service.config as config
    state = {"off": True}
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(config, "no_thinking_capable", lambda: ["fast"] if state["off"] else [])
    monkeypatch.setattr(config, "thinks_explicitly", lambda name: False)
    return state


def use(monkeypatch, client):
    from service.assistant import brief
    monkeypatch.setattr(brief, "_c", lambda: client)
    return client


@pytest.mark.parametrize("loaded,warm,turn,expect_call", [
    (["fast"], (), False, True),
    (["fast", "embed"], ("embed",), False, True),
    (["coder"], (), False, False),               # another model is resident: never evict it
    (["fast", "coder"], (), False, False),       # loading "fast" alone would evict the coder
    ([], (), False, False),                      # nothing loaded: never load one
    (["fast"], (), True, False),                 # a turn started: stand down
])
def test_the_model_call_never_loads_evicts_or_races_a_turn(monkeypatch, model, loaded, warm, turn, expect_call):
    client = use(monkeypatch, FakeClient(loaded, warm))
    reply = asyncio.run(G._resident_reply("s", "m", lambda: turn))
    assert (reply == "Mostly school mail") is expect_call and bool(client.requests) is expect_call


def test_the_request_is_background_shaped_thinking_off_and_tightly_bounded(monkeypatch, model):
    client = use(monkeypatch, FakeClient(["fast"]))
    asyncio.run(G._resident_reply("s", "m", lambda: False))
    name, kwargs = client.requests[0]
    assert name == "fast" and kwargs["max_tokens"] == G._TOKENS and kwargs["temperature"] == 0
    assert kwargs["chat_template_kwargs"] == {"enable_thinking": False}
    # A model whose think block can't be turned off: nothing is claimed about
    # thinking (never enable_thinking=True), and there is room for the block.
    model["off"] = False
    client.requests.clear()
    asyncio.run(G._resident_reply("s", "m", lambda: False))
    _, kwargs = client.requests[0]
    assert "chat_template_kwargs" not in kwargs and kwargs["max_tokens"] == G._THINKING_TOKENS
    assert G._TIMEOUT_S >= 60, "a background gist is not held to the 12 s user-facing limit"


def test_a_slow_but_successful_reply_is_cached(isolated, monkeypatch, model):
    """Build 2020: the resident thinking model took longer than the 12 s summary
    timeout, so every tick failed and nothing was ever written."""
    client = use(monkeypatch, FakeClient(["fast"], delay=0.6, content="<think>Events.</think>\nMostly event updates"))
    monkeypatch.setattr(G, "_TIMEOUT_S", 0.3)
    assert asyncio.run(G.refresh(NOW, abort=lambda: False)) is False, "past the deadline it fails"
    assert G._next_call_at == NOW + G._FAILURE_GAP_S and client.cancelled
    monkeypatch.setattr(G, "_TIMEOUT_S", 5.0)
    assert asyncio.run(G.refresh(NOW + 100, abort=lambda: False)) is True
    assert G._load()["inbox"]["gist"] == "Mostly event updates" and G._PATH.exists()


def test_a_turn_that_starts_mid_call_cancels_it(isolated, monkeypatch, model):
    turn = [False]
    client = use(monkeypatch, FakeClient(["fast"], delay=1.0, turn=turn))
    assert asyncio.run(G.refresh(NOW, abort=lambda: turn[0])) is False
    assert client.cancelled and "inbox" not in G._load() and G._next_call_at == NOW + G._IDLE_GAP_S


def test_a_cut_off_reply_is_a_failure(isolated, monkeypatch, model):
    use(monkeypatch, FakeClient(["fast"], content="<think>still going", finish="length"))
    assert asyncio.run(G.refresh(NOW, abort=lambda: False)) is False
    assert "inbox" not in G._load() and G._next_call_at == NOW + G._FAILURE_GAP_S


# --- what the Brief shows ----------------------------------------------------------------------


def test_attach_adds_overviews_only_for_readable_sources(isolated):
    G._load()["inbox"] = {"gist": "Mostly event updates", "at": NOW - 60, "key": "k"}
    workspace = {"source_coverage": [{"source": "email", "ready": True}, {"source": "messages", "ready": True}]}
    G.attach(workspace, NOW)
    assert workspace["mail_overview"] == {"text": "Mostly event updates", "source": "model"}
    assert workspace["texts_overview"] == {"text": "Texts with Mom", "source": "summary"}
    blocked = {"source_coverage": [{"source": "email", "ready": False},
                                   {"source": "messages", "ready": False, "blocked": "full_disk_access"}]}
    G.attach(blocked, NOW)
    assert "mail_overview" not in blocked and "texts_overview" not in blocked


def test_day_planning_never_imports_the_gist_writer():
    """daily_workspace runs inside the sandboxed planning proof, which allows only
    its approved imports (tests/test_behavior_availability.py)."""
    import ast
    import inspect
    from service.assistant import daily_workspace
    tree = ast.parse(inspect.getsource(daily_workspace))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
             for alias in node.names} | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "mail_gists" not in names and "service.assistant.mail_gists" not in names
