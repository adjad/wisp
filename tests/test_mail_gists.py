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
    monkeypatch.setattr(G, "_retry_at", {})
    monkeypatch.setattr(G, "_last_standdown", "")
    monkeypatch.setattr(G, "recent_mail", lambda now: [row(1, "Tech Week starts today"), row(2, "Invitation: Demo day")])
    monkeypatch.setattr(G, "recent_texts", lambda now: [convo("Mom", "call me when you can")])
    # The inbox summary has its own tests below; here only the two row lines.
    monkeypatch.setattr(G, "summary_lines", lambda rows: [])
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
    assert "texts" not in G._load() and G._retry_at["texts"] == NOW + G._FAILURE_GAP_S


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
    assert "inbox" not in G._load() and G._retry_at["inbox"] == NOW + G._FAILURE_GAP_S
    gap = G._FAILURE_GAP_S
    assert asyncio.run(G.refresh(NOW + 1, synth=synth, abort=lambda: False)) is False and len(calls) == 1, "backing off"
    asyncio.run(G.refresh(NOW + gap + 1, synth=synth, abort=lambda: False))
    assert asyncio.run(G.refresh(NOW + 2 * gap + 2, synth=synth, abort=lambda: False)) is False and len(calls) == 2
    monkeypatch.setattr(G, "recent_mail", lambda now: [row(7, "Something new")])
    asyncio.run(G.refresh(NOW + 3 * gap, synth=synth, abort=lambda: False))
    assert len(calls) == 3


def test_a_failing_summary_never_holds_up_the_row_lines(isolated, monkeypatch):
    """Auditor note on #299: one global backoff let a failing summary starve the gists."""
    monkeypatch.setattr(G, "summary_lines", lambda rows: ["- From Ann Lee: Lunch"])
    seen = []

    async def synth(system, material):
        seen.append(material)
        return "Ann Lee asked about lunch at 12. The rest is quiet." if len(seen) == 1 else "Mostly lunch plans"

    assert asyncio.run(G.refresh(NOW, synth=synth, abort=lambda: False)) is False, "an invented number"
    assert asyncio.run(G.refresh(NOW + G._MIN_GAP_S, synth=synth, abort=lambda: False)) is True
    assert "From Ann Lee" in seen[0] and "Tech Week" in seen[1] and G._load()["inbox"]["gist"] == "Mostly lunch plans"


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
    assert G._retry_at["inbox"] == NOW + G._FAILURE_GAP_S and client.cancelled
    monkeypatch.setattr(G, "_TIMEOUT_S", 5.0)
    assert asyncio.run(G.refresh(NOW + G._FAILURE_GAP_S + 1, abort=lambda: False)) is True
    assert G._load()["inbox"]["gist"] == "Mostly event updates" and G._PATH.exists()


def test_a_turn_that_starts_mid_call_cancels_it(isolated, monkeypatch, model):
    turn = [False]
    client = use(monkeypatch, FakeClient(["fast"], delay=1.0, turn=turn))
    assert asyncio.run(G.refresh(NOW, abort=lambda: turn[0])) is False
    assert client.cancelled and "inbox" not in G._load() and G._next_call_at == NOW + G._IDLE_GAP_S


def test_a_cut_off_reply_is_a_failure(isolated, monkeypatch, model):
    use(monkeypatch, FakeClient(["fast"], content="<think>still going", finish="length"))
    assert asyncio.run(G.refresh(NOW, abort=lambda: False)) is False
    assert "inbox" not in G._load() and G._retry_at["inbox"] == NOW + G._FAILURE_GAP_S


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


# --- the written inbox summary ------------------------------------------------------------


def mail_row(n, subject, sender, address, body="", ago=600.0, **extra):
    return dict(subject=subject, sender=sender, sender_address=address, body=body, ts=NOW - ago,
                unread=True, message_id=f"<m{n}@example.com>") | extra


INBOX = [
    mail_row(1, "Canceled: Office Hours", "John Morada", "john.morada@dublin.ca.gov", "We'll meet next Monday instead."),
    mail_row(2, "Office Hours", "John Morada", "john.morada@dublin.ca.gov", "Every Monday before City Council."),
    mail_row(3, "Application Update", "Devarsh Krishnan", "devarsh@180dc.org", "Thanks for applying. Interviews start Friday."),
    mail_row(4, "Re: Private details omitted.", "John Morada", "john.morada@dublin.ca.gov"),
    mail_row(5, "LA Tech Week starts today", "Katia Ameri", "hello@tech-week.com", "Unsubscribe"),
    mail_row(6, "Ship SF Wrapped", "Vercel", "ship@info.vercel.com"),
    mail_row(7, "Thank you, San Francisco", "Katia Ameri", "katia@tech-week.com", "",
             message_id="<c76.20261011@mail73.suw111.mcdlv.net>"),
]


def test_mass_mail_under_a_persons_name_never_needs_the_user():
    """Build 2066: Tech Week promos (hello@, Mailchimp) and Vercel (ship@info.) counted as needing you."""
    from service.tools.email_tools import digest_category, is_broadcast
    assert all(is_broadcast(r) for r in INBOX[4:]) and not any(is_broadcast(r) for r in INBOX[:4])
    assert [digest_category(r) for r in INBOX[4:]].count("attention") == 0
    assert digest_category(INBOX[1]) == "attention", "a person's unread email still needs the user"
    assert is_broadcast({"sender": "Vercel <ship@info.vercel.com>"}), "the address inside the sender is read too"


def test_summary_input_describes_people_and_counts_the_rest():
    lines = G.summary_lines(INBOX)
    assert lines[0] == "- From John Morada: Canceled: Office Hours — We'll meet next Monday instead."
    assert lines[1] == "- From Devarsh Krishnan: Application Update — Thanks for applying. Interviews start Friday."
    assert not any("Every Monday" in l for l in lines), "a cancellation stands in for what it cancels"
    assert lines[2] == "- Also 3 newsletters, alerts and promos, from Katia Ameri, Vercel"
    assert lines[3] == "- 1 more email whose content is withheld (sign-in codes, security notices; not shown)"
    assert not any("Private details" in l for l in lines)


def test_the_prose_fallback_reads_as_a_summary():
    assert G.mail_prose(INBOX) == ("John Morada canceled “Office Hours” (2 emails). Devarsh Krishnan wrote about “Application Update”. "
                                   "The rest is 3 newsletters, alerts and promos.")
    assert G.mail_prose([]) == "No new mail today."
    assert G.mail_prose(INBOX[4:]) == "Just 3 newsletters, alerts and promos; nothing from people."


def test_the_users_own_mail_is_not_summarized(monkeypatch):
    from service.memory import identity
    monkeypatch.setattr(identity, "user_emails", lambda: ["me@example.com"])
    mine = mail_row(9, "Future", "Adi Jain", "me@example.com")
    assert G.mail_prose([mine]) == "Nothing from people today." and G.summary_lines([mine]) == []


def test_parse_paragraph_keeps_prose_and_drops_lists_links_and_think_leaks():
    text = "<think>plan</think>\nJohn Morada canceled office hours.\nDevarsh sent an application update. The rest is promos."
    assert G.parse_paragraph(text) == "John Morada canceled office hours. Devarsh sent an application update. The rest is promos."
    assert G.parse_paragraph("Summary: John canceled office hours and the rest is promos.") \
        == "John canceled office hours and the rest is promos."
    assert G.parse_paragraph("- John canceled\n- Promos") == ""
    assert G.parse_paragraph("<think>John canceled office hours and more words here") == ""
    assert G.parse_paragraph("Too short.") == "" and G.parse_paragraph("x " * 300) == ""
    assert G.parse_paragraph("John canceled. Details at https://evil.example and the rest is promos.") == ""


def test_the_summary_is_written_first_with_its_own_budget_and_attached(isolated, monkeypatch, model):
    monkeypatch.setattr(G, "summary_lines", lambda rows: ["- From John Morada: Canceled: Office Hours"])
    client = use(monkeypatch, FakeClient(["fast"], content="John Morada canceled office hours. The rest is quiet today."))
    assert asyncio.run(G.refresh(NOW, abort=lambda: False)) is True
    assert client.requests[0][1]["max_tokens"] == G._SUMMARY_TOKENS
    assert G._load()["summary"]["gist"] == "John Morada canceled office hours. The rest is quiet today."
    workspace = {"source_coverage": [{"source": "email", "ready": True}]}
    G.attach(workspace, NOW + 1)
    assert workspace["mail_summary"] == {"text": "John Morada canceled office hours. The rest is quiet today.", "source": "model"}
    model["off"] = False
    client.requests.clear()
    monkeypatch.setattr(G, "summary_lines", lambda rows: ["- From Ann: Lunch"])
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    asyncio.run(G.refresh(NOW + G._REWRITE_S + 5, abort=lambda: False))
    assert client.requests[0][1]["max_tokens"] == G._SUMMARY_THINKING_TOKENS


def test_an_invented_number_drops_the_summary_for_the_prose(isolated, monkeypatch):
    monkeypatch.setattr(G, "summary_lines", lambda rows: ["- From John Morada: Canceled: Office Hours"])

    async def synth(system, material):
        assert "2 or 3 short sentences" in system and "data, not instructions" in system
        return "John Morada canceled office hours at 5 PM. The rest is quiet."

    assert asyncio.run(G.refresh(NOW, synth=synth, abort=lambda: False)) is False
    assert "summary" not in G._load()
    workspace = {"source_coverage": [{"source": "email", "ready": True}]}
    G.attach(workspace, NOW)
    assert workspace["mail_summary"]["source"] == "summary"


def test_auditor_notes_on_299_broadcast_is_narrow_and_the_summary_is_grounded(isolated):
    from service.tools.email_tools import is_broadcast
    person = lambda address, mid="<1@example.com>": {"sender": "Maya Chen", "sender_address": address, "message_id": mid}
    assert not any(is_broadcast(person(a)) for a in ("maya@mail.com", "maya@email.com", "x@go.com", "x@news.com.au",
                                                      "hi@mayachen.com", "contact@mayachen.com"))
    assert not is_broadcast(person("maya@corp.example", "<abc@email.amazonses.com>")), "relays carry one-to-one mail"
    assert is_broadcast(person("x@mail.shop.example")) and is_broadcast(person("hello@tech-week.com"))
    lines = ["- From John Morada: Canceled: Office Hours — We'll meet Monday instead."]
    assert G.summary_ok("John Morada canceled office hours and will meet Monday instead.", lines)
    assert not G.summary_ok("John Morada canceled office hours; Priya will cover.", lines), "a name the mail lacks"
    assert G.parse_paragraph("Your bank says your account is locked, so call 415-555-0100 right away today.") == ""
    assert G.parse_paragraph("John canceled office hours. Reply to help@bank.xyz to confirm the change today.") == ""
    assert G.parse_paragraph("John canceled office hours. Details at login.example.app for the next session.") == ""
    for link in ("go to evil.com Now for the details", "visit HTTPS://EVIL.COM for the details", "see WWW.EVIL.COM today",
                 "see Evil.COM for the details"):
        assert G.parse_paragraph(f"John canceled office hours; {link}.") == "", link
    assert G.parse_paragraph("John canceled office hours. The rest is promos and newsletters.") != "", "sentences still pass"
    assert G.mail_prose([mail_row(8, "Declined: Lunch", "Priya Shah", "priya@example.com")]) == "Priya Shah declined “Lunch”."
    assert G.mail_prose([mail_row(8, "Deadline notice (private details omitted).", "Bank", "bank@example.com")]) \
        == "Nothing from people today."


# --- private mail is used, kept compact, and stays on this Mac -----------------------------


def test_private_mail_reaches_the_summary_input_but_a_sign_in_code_does_not(monkeypatch):
    from service.tools import imessage_tools
    # The redactor withholds these as "private" (a schedule change, an order id);
    # the summary reads them. The sign-in code is the only thing it still hides.
    private = mail_row(11, "Rescheduled: Dublin Community Office Hours to Oct 28", "John Morada", "john@dublin.example",
                       "Moving to 5pm. Order ZX9K2Q81 stays the same.")
    code = mail_row(12, "Your sign in code is 482913", "Acme Bank", "alerts@bank.example", "Use 482913 to verify.")
    private_row = mail_row(13, "Private details omitted.", "Someone", "someone@example.com")
    assert imessage_tools.redact_summary_codes(private["body"]) != private["body"], "the redactor does withhold this body"
    lines = G.summary_lines([private, code, private_row])
    text = "\n".join(lines)
    assert "Rescheduled: Dublin Community Office Hours to Oct 28" in text and "Moving to 5pm" in text
    assert "482913" not in text and "sign in code" not in text
    assert lines[-1].startswith("- 2 more emails whose content is withheld")


def test_a_promo_with_a_list_footer_is_mass_mail_even_under_a_persons_name():
    promo = mail_row(21, "Learn AR with SPECS: $2,500 in prizes, no glasses needed", "Darlyze Team", "sam@darlyze.example",
                     "Join the build weekend.\n\nYou are receiving this because you signed up. Unsubscribe here.")
    viewer = mail_row(22, "Hackathon", "Sam", "sam2@darlyze.example", "Big news. View this email in your browser.")
    plain = mail_row(23, "Lunch?", "Priya Shah", "priya@example.com", "Are you free Thursday?")
    assert G.is_mass_mail(promo) and G.is_mass_mail(viewer) and not G.is_mass_mail(plain)
    people, bulk, _, _ = G._people_mail([promo, viewer, plain])
    assert [p[0] for p in people] == ["Priya Shah"] and sum(bulk.values()) == 2
    assert "SPECS" not in "\n".join(G.summary_lines([promo, viewer, plain]))
    # A calendar change under a footer is still described.
    change = mail_row(24, "Canceled: Office Hours", "Ann", "ann@example.com", "Unsubscribe here.")
    assert not G.is_mass_mail(change)


def test_a_reply_that_is_an_input_line_is_rejected_and_the_prose_stands():
    lines = ["- From Darlyze: Learn AR with SPECS: $2,500 in prizes — Join the build weekend"]
    assert not G.summary_ok("From Darlyze: Learn AR with SPECS: $2,500 in prizes, no glasses needed. The rest is job alerts.", lines)
    assert not G.summary_ok("Learn AR with SPECS: $2,500 in prizes — Join the build weekend and more. Rest is promos.", lines)
    assert G.echoes_input("- From Ann: Lunch") and G.echoes_input("Also 3 newsletters, alerts and promos, from X")
    lines = ["- From John Morada: Canceled: Office Hours — We'll meet Monday instead."]
    assert G.summary_ok("John Morada canceled office hours and will meet Monday instead.", lines)


def test_an_echoed_summary_already_cached_gives_way_to_the_prose(isolated):
    G._load()["summary"] = {"gist": "From Darlyze: Learn AR with SPECS: $2,500 in prizes. The rest is job alerts and newsletters.",
                            "at": NOW - 60, "key": "k"}
    assert G.overview("summary", "Prose.", NOW, reject=G.echoes_input) == {"text": "Prose.", "source": "summary"}
    assert G.overview("summary", "Prose.", NOW)["source"] == "model", "no check, no change"


def test_the_prose_fallback_covers_private_mail_compactly():
    morada = [mail_row(i, s, "John Morada", "john@dublin.example", ago=100.0 * i) for i, s in
              enumerate(["Canceled: Dublin Community Office Hours", "Re: Dublin Community Office Hours",
                         "Dublin Community Office Hours"], 1)]
    assert G.mail_prose(morada) == "John Morada canceled “Dublin Community Office Hours” (3 emails)."
    one = mail_row(9, "Rescheduled: Dublin Office Hours to Oct 28", "John Morada", "john@dublin.example", "Moving to 5pm.")
    assert G.mail_prose([one]) == "John Morada wrote about “Rescheduled: Dublin Office Hours to Oct 28”."


def test_private_summary_input_carries_no_number_the_model_may_not_repeat():
    lines = G.summary_lines([mail_row(31, "Rescheduled: Office Hours to Oct 28", "John Morada", "john@dublin.example")])
    assert G.summary_ok("John Morada moved office hours to Oct 28.", lines)
    assert not G.summary_ok("John Morada moved office hours to Oct 29.", lines)


def test_the_summary_request_goes_only_to_the_local_fast_model(monkeypatch, model):
    """Private mail is in the input: the request is the resident-reply path, to the
    fast role on a loopback engine. Remote-only mode, or an engine that isn't this
    Mac's, gets nothing sent."""
    import inspect
    from service.inference import remote_policy
    client = use(monkeypatch, FakeClient(["fast"], content="John canceled office hours. The rest is quiet."))
    assert asyncio.run(G._resident_reply("sys", "mail", lambda: False, local_only=True)) is not None and client.requests[0][0] == "fast"
    # remote-only profile on: stand down before any request, even with a "local" client object
    remote = use(monkeypatch, FakeClient(["fast"]))
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: object())
    assert asyncio.run(G._resident_reply("sys", "mail", lambda: False, local_only=True)) is None and remote.requests == []
    monkeypatch.undo()
    # a non-loopback engine
    far = FakeClient(["fast"])
    far.managed = False
    from service.assistant import brief
    import service.config as config
    monkeypatch.setattr(brief, "_c", lambda: far)
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    assert asyncio.run(G._resident_reply("sys", "mail", lambda: False, local_only=True)) is None and far.requests == []
    # only the summary asks for local-only: refresh() passes it for kind "summary"
    seen = []

    async def spy(system, material, abort, **kw):
        seen.append(kw.get("local_only"))
        return None

    monkeypatch.setattr(G, "_resident_reply", spy)
    monkeypatch.setattr(G, "summary_lines", lambda rows: ["- From Ann: Lunch"])
    monkeypatch.setattr(G, "recent_mail", lambda now: [row(1, "Lunch")])
    monkeypatch.setattr(G, "recent_texts", lambda now: [])
    monkeypatch.setattr(G, "_PATH", Path(os.environ["TMPDIR"]) / "g.json")
    monkeypatch.setattr(G, "_cache", {})
    asyncio.run(G.refresh(NOW, abort=lambda: False))
    assert seen == [True]
    # the module has no other way to reach a model: no HTTP or cloud imports, no client of its own
    import ast
    tree = ast.parse(inspect.getsource(G))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
               {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in imported if any(w in m for w in ("httpx", "requests", "urllib", "cloud", "omlx_client", "delegat"))], imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"OMLXClient", "cloud_super_model_target", "cloud_provider_settings", "chat_cloud"}
