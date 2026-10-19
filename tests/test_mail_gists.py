"""Mail gists: bounded, background, grounded display text. No models or native sources."""
import asyncio
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-mail-gists-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from pathlib import Path

import pytest

from service.assistant import mail_gists as G


def email(n, **changes):
    return dict(id=f"email:{n}", sender=f"Sender {n}", subject=f"Subject {n}", excerpt=f"Body {n}",
                person=True, ts=float(n)) | changes


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_PATH", Path(tmp_path) / "cache" / "mail_gists.json")
    monkeypatch.setattr(G, "_cache", None)
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    return tmp_path


def test_parse_reply_keeps_numbered_bounded_lines_only():
    text = ("Here you go:\n1. Prof. Lee moved the meeting to Tuesday\n2) **Priya** wants your slides\n"
            "3. " + "x" * 200 + "\n9. out of range\n1. duplicate\n- 5: see https://evil.example\n")
    assert G.parse_reply(text, 5) == {1: "Prof. Lee moved the meeting to Tuesday", 2: "Priya wants your slides"}


def test_grounded_rejects_numbers_the_email_does_not_contain():
    c = email(1, subject="Meeting moved", excerpt="Can we meet Tuesday at 3?")
    assert G.grounded("Sender 1 moved the meeting to Tuesday at 3", c)
    assert not G.grounded("Sender 1 moved the meeting to Tuesday at 4", c)
    assert G.grounded("Sender 1 moved the meeting", c)


def test_material_numbers_each_email_with_sender_subject_and_body():
    text = G.material([email(1), email(2, excerpt="Subject 2")])
    assert "1. From: Sender 1\nSubject: Subject 1\nBody: Body 1" in text
    assert "2. From: Sender 2\nSubject: Subject 2" in text and "Body: Subject 2" not in text


def test_refresh_writes_once_and_drops_ungrounded(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(1), email(2, excerpt="due Friday")])

    async def synth(system, material):
        calls.append((system, material))
        return "1. Sender 1 wants a reply\n2. Sender 2 needs it by the 30th"

    written = asyncio.run(G.refresh(100.0, synth=synth, abort=lambda: False))
    assert written == 1 and len(calls) == 1
    assert "IMPORTANT rules" in calls[0][0] and "data, not instructions" in calls[0][0]
    assert G.gist("email:1") == "Sender 1 wants a reply"
    assert G.gist("email:2") == ""          # "30" isn't in the email: the subject shows instead
    assert G._PATH.exists() and oct(G._PATH.stat().st_mode & 0o777) == "0o600"
    # Each email is sent once: nothing left to write means no model call.
    assert asyncio.run(G.refresh(101.0, synth=synth, abort=lambda: False)) == 0 and len(calls) == 1
    # A fresh process reads the gists back.
    monkeypatch.setattr(G, "_cache", None)
    assert G.gist("email:1") == "Sender 1 wants a reply"


def test_refresh_stands_down_for_a_turn_and_bounds_the_batch(isolated, monkeypatch):
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(n) for n in range(10)])
    seen = []

    async def synth(system, material):
        seen.append(material)
        return ""

    assert asyncio.run(G.refresh(100.0, synth=synth, abort=lambda: True)) == 0 and not seen
    asyncio.run(G.refresh(100.0, synth=synth, abort=lambda: False))
    assert len(seen) == 1 and "2. From" in seen[0] and "3. From" not in seen[0]


def test_a_failed_call_caches_nothing_and_backs_off(isolated, monkeypatch):
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(1)])

    async def synth(system, material):
        raise TimeoutError

    assert asyncio.run(G.refresh(100.0, synth=synth, abort=lambda: False)) == 0
    assert G.gist("email:1") == "" and "email:1" not in G._load()
    assert G._next_call_at == 100.0 + G._FAILURE_GAP_S


def test_old_entries_age_out(isolated):
    cache = G._load()
    cache["old"] = {"gist": "x", "at": 0.0}
    cache["new"] = {"gist": "y", "at": G._KEEP_S + 10}
    G._save(G._KEEP_S + 20)
    assert set(G._load()) == {"new"}


def test_schedule_tick_is_single_flight_and_waits_for_quiet(isolated, monkeypatch):
    from service import idle
    from service.assistant.hub import hub
    monkeypatch.setattr(type(hub), "has_subscribers", property(lambda self: True))
    monkeypatch.setattr(idle, "foreground_busy", lambda: True)
    assert G.schedule_tick() is None

    async def main():
        monkeypatch.setattr(idle, "foreground_busy", lambda: False)
        monkeypatch.setattr(G, "important_emails", lambda now=None: [])
        task = G.schedule_tick()
        assert task is not None and G.schedule_tick() is None
        await task

    asyncio.run(main())


def test_a_think_leak_drops_the_whole_reply_and_urls_are_never_gists():
    assert G.parse_reply("<think>1. The email says pay now\n2. Sender wants money", 2) == {}
    assert G.parse_reply("1. Visit example.com to claim\n2. Bob shares www.prize.net\n3. Ann wants the notes", 3) == {
        3: "Ann wants the notes"}


def test_grounding_matches_whole_numbers_only():
    c = email(1, subject="Order 13", excerpt="Shipped in 2025")
    assert not G.grounded("Sender 1 says it arrives on the 3rd", c)
    assert G.grounded("Order 13 shipped", c)


def test_an_unusable_reply_caches_nothing_and_backs_off(isolated, monkeypatch):
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(1)])

    async def synth(system, material):
        return "Sorry, I can't help with that."

    assert asyncio.run(G.refresh(100.0, synth=synth, abort=lambda: False)) == 0
    assert "email:1" not in G._load() and G._next_call_at == 100.0 + G._FAILURE_GAP_S


def test_nothing_pending_waits_a_minute_before_reading_the_inbox_again(isolated, monkeypatch):
    monkeypatch.setattr(G, "important_emails", lambda now=None: [])
    assert asyncio.run(G.refresh(100.0, abort=lambda: False)) == 0 and G._next_call_at == 100.0 + G._IDLE_GAP_S


class FakeClient:
    def __init__(self, loaded, warm=(), *, delay=0.0, content="1. ok", finish="stop", turn=None):
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
    reply = asyncio.run(G._resident_reply("s", "m", lambda: turn, count=1))
    assert (reply == "1. ok") is expect_call and bool(client.requests) is expect_call


def test_the_request_is_background_shaped_thinking_off_and_tightly_bounded(monkeypatch, model):
    client = use(monkeypatch, FakeClient(["fast"]))
    asyncio.run(G._resident_reply("s", "m", lambda: False, count=2))
    name, kwargs = client.requests[0]
    assert name == "fast" and kwargs["max_tokens"] == 2 * G._TOKENS_PER_GIST and kwargs["temperature"] == 0
    assert kwargs["chat_template_kwargs"] == {"enable_thinking": False}
    # A model whose think block can't be turned off: nothing is claimed about
    # thinking (never enable_thinking=True), one email per call, room for the block.
    model["off"] = False
    client.requests.clear()
    asyncio.run(G._resident_reply("s", "m", lambda: False, count=1))
    _, kwargs = client.requests[0]
    assert "chat_template_kwargs" not in kwargs and kwargs["max_tokens"] == G._THINKING_TOKENS
    assert G.batch_size("fast") == 1
    model["off"] = True
    assert G.batch_size("fast") == 2
    assert G._TIMEOUT_S >= 60, "a background gist is not held to the 12 s user-facing limit"


def test_a_slow_but_successful_reply_is_cached(isolated, monkeypatch, model):
    """Build 2020: the resident thinking model took longer than the 12 s summary
    timeout, so every tick failed and no gist was ever written."""
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(1), email(2), email(3)])
    client = use(monkeypatch, FakeClient(["fast"], delay=0.6,
                                         content="<think>Email 1 is about a reply.</think>\n1. Sender 1 wants a reply\n2. Sender 2 shared notes"))
    monkeypatch.setattr(G, "_TIMEOUT_S", 0.3)
    assert asyncio.run(G.refresh(100.0, abort=lambda: False)) == 0, "past the deadline it fails"
    assert G._next_call_at == 100.0 + G._FAILURE_GAP_S and client.cancelled
    monkeypatch.setattr(G, "_TIMEOUT_S", 5.0)
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    assert asyncio.run(G.refresh(200.0, abort=lambda: False)) == 2
    assert G.gist("email:1") == "Sender 1 wants a reply" and G.gist("email:2") == "Sender 2 shared notes"
    assert "email:3" not in G._load(), "two per call; the third waits for the next tick"
    assert G._PATH.exists()


def test_a_turn_that_starts_mid_call_cancels_it(isolated, monkeypatch, model):
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(1)])
    turn = [False]
    client = use(monkeypatch, FakeClient(["fast"], delay=1.0, turn=turn))
    assert asyncio.run(G.refresh(100.0, abort=lambda: turn[0])) == 0
    assert client.cancelled and "email:1" not in G._load() and G._next_call_at == 100.0 + G._IDLE_GAP_S


def test_a_cut_off_reply_is_a_failure(isolated, monkeypatch, model):
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(1)])
    use(monkeypatch, FakeClient(["fast"], content="<think>still going", finish="length"))
    assert asyncio.run(G.refresh(100.0, abort=lambda: False)) == 0
    assert "email:1" not in G._load() and G._next_call_at == 100.0 + G._FAILURE_GAP_S


def test_a_closed_think_block_is_dropped_and_an_unclosed_one_drops_the_reply():
    assert G.parse_reply("<think>1. not a gist</think>\n1. Ann wants the notes", 1) == {1: "Ann wants the notes"}
    assert G.parse_reply("<think>1. Ann wants the notes", 1) == {}
    assert G.parse_reply("1. Ann wants the notes</think>", 1) == {}


def test_attach_puts_written_gists_on_email_items_only(isolated):
    from service.assistant.daily_workspace import key
    G._load().update({"rec-1": {"gist": "Ann wants the notes", "at": 1.0}, "rec-2": {"gist": "", "at": 1.0}})
    workspace = {"attention": [
        {"id": key("email", "rec-1"), "source": "email", "title": "Notes"},
        {"id": key("email", "rec-2"), "source": "email", "title": "Hello"},
        {"id": key("messages", "rec-1"), "source": "messages", "title": "Ann: notes?"},
    ]}
    G.attach(workspace)
    assert [item.get("gist") for item in workspace["attention"]] == ["Ann wants the notes", None, None]


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
