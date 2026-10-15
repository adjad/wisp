"""OpenAIPhraser / TemplatePhraser: payload contract, think stripping,
fail-closed behavior and categorize validation (no network)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from service.behavior.phraser import OpenAIPhraser, TemplatePhraser, strip_think
from service.behavior.types import CATEGORIES

ROOT = Path(__file__).resolve().parents[1]


def _reply(content, status=200):
    return httpx.Response(status, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


class Recorder:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if self.replies else _reply("")
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def payloads(self):
        return [json.loads(r.content) for r in self.requests]


def make(rec, **kw):
    kw.setdefault("model", "example-model")
    return OpenAIPhraser("https://phraser.example", "/v1", transport=httpx.MockTransport(rec), **kw)


FACTS = {"habit": "gym", "usual_time": "07:00", "conflict": "Team sync", "day": "Tuesday"}


def test_payload_keys_restricted_and_url():
    rec = Recorder(_reply("Your Team sync on Tuesday overlaps the gym at 07:00."))
    out = make(rec, token="secret-token").phrase("habit_conflict", FACTS)
    assert out == "Your Team sync on Tuesday overlaps the gym at 07:00."
    req = rec.requests[0]
    assert str(req.url) == "https://phraser.example/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer secret-token"
    body = rec.payloads[0]
    assert set(body) == {"model", "messages", "max_tokens", "stream", "temperature"}
    assert body["stream"] is False and body["model"] == "example-model" and body["max_tokens"] == 160
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "ONLY the facts" in body["messages"][0]["content"]


def test_payload_satisfies_remote_only_profile_contract():
    from tests.fixtures.remote_inference import example_profile
    rec = Recorder(_reply("ok"), _reply("{}"))
    p = make(rec)
    p.phrase("day_preview", FACTS)
    p.categorize(["Gym", "1:1 Sam"], CATEGORIES)
    for body in rec.payloads:
        example_profile().validate_payload(body)  # raises on violation


def test_callable_token_and_no_token():
    rec = Recorder(_reply("Gym at 07:00 on Tuesday."), _reply("Gym at 07:00 on Tuesday."))
    make(rec, token=lambda: "from-keychain").phrase("day_preview", FACTS)
    make(rec).phrase("day_preview", FACTS)
    assert rec.requests[0].headers["authorization"] == "Bearer from-keychain"
    assert "authorization" not in rec.requests[1].headers


def test_token_callable_failure_returns_none():
    def boom():
        raise RuntimeError("keychain locked")
    assert make(Recorder(_reply("x")), token=boom).phrase("day_preview", FACTS) is None


@pytest.mark.parametrize("raw,expected", [
    ("<think>long reasoning about Bob</think>Gym at 07:00 on Tuesday.", "Gym at 07:00 on Tuesday."),
    ("reasoning leaked</think>\n\nGym at 07:00.", "Gym at 07:00."),
    ("<think>never closed, truncated", None),
    ("<THINK>x</THINK>  ", None),
])
def test_think_blocks_stripped(raw, expected):
    assert make(Recorder(_reply(raw))).phrase("day_preview", FACTS) == expected


def test_strip_think_helper():
    assert strip_think("<think>a</think>b<think>c</think>d") == "bd"
    assert strip_think("answer<think>trailing") == "answer"


@pytest.mark.parametrize("reply", [
    httpx.ConnectTimeout("timed out"),
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("refused"),
    _reply("x", status=502),
    _reply("x", status=401),
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    _reply("   "),
])
def test_errors_return_none(reply):
    assert make(Recorder(reply)).phrase("day_preview", FACTS) is None


def test_rejects_new_facts():
    # new number, new name, new weekday -> template fallback
    assert make(Recorder(_reply("Gym at 08:15 on Tuesday."))).phrase("x", FACTS) is None
    assert make(Recorder(_reply("Your sync with Priya overlaps the gym."))).phrase("x", FACTS) is None
    assert make(Recorder(_reply("Gym on Friday at 07:00."))).phrase("x", FACTS) is None
    # names and numbers from the facts are fine, so is a sentence-initial word
    assert make(Recorder(_reply("Heads up: Team sync clashes with gym at 07:00."))).phrase("x", FACTS)


def test_truncates_to_two_sentences_and_cleans_quotes():
    rec = Recorder(_reply('"Gym at 07:00. Team sync is on Tuesday. Have fun. Really."'))
    assert make(rec).phrase("x", FACTS) == "Gym at 07:00. Team sync is on Tuesday."


def test_oversized_facts_skip_the_call():
    rec = Recorder(_reply("x"))
    assert make(rec).phrase("x", {"blob": "a" * 10000}) is None
    assert rec.requests == []


def test_categorize_validates_values_and_keys():
    titles = ["Gym", "1:1 with Sam", "Standup", "Dentist"]
    raw = ('<think>hmm</think>```json\n{"1": "fitness", "2": "one_on_one", "3": "BANANA", '
           '"Dentist": "health", "99": "meal", "Unknown title": "meal", "4": 7}\n```')
    rec = Recorder(_reply(raw))
    out = make(rec).categorize(titles, CATEGORIES)
    assert out == {"Gym": "fitness", "1:1 with Sam": "one_on_one", "Dentist": "health"}
    body = rec.payloads[0]
    assert body["temperature"] == 0.0 and set(body) == {"model", "messages", "max_tokens", "stream", "temperature"}
    assert "1. Gym" in body["messages"][1]["content"]


def test_categorize_failure_and_garbage_return_empty():
    assert make(Recorder(httpx.ReadTimeout("t"))).categorize(["Gym"], CATEGORIES) == {}
    assert make(Recorder(_reply("I think it is fitness"))).categorize(["Gym"], CATEGORIES) == {}
    assert make(Recorder(_reply('["fitness"]'))).categorize(["Gym"], CATEGORIES) == {}
    assert make(Recorder()).categorize([], CATEGORIES) == {}


def test_categorize_batches_large_inputs():
    titles = [f"Event {i}" for i in range(45)]
    rec = Recorder(_reply('{"1": "meeting"}'), _reply('{"5": "focus"}'))
    out = make(rec).categorize(titles, CATEGORIES)
    assert len(rec.requests) == 2
    assert out == {"Event 0": "meeting", "Event 44": "focus"}


def test_template_phraser_is_inert():
    t = TemplatePhraser()
    assert t.phrase("day_preview", FACTS) is None
    assert t.categorize(["Gym"], CATEGORIES) == {}


def test_constructor_validation():
    with pytest.raises(ValueError):
        OpenAIPhraser("http://127.0.0.1:11434", model="")
    with pytest.raises(ValueError):
        OpenAIPhraser("http://127.0.0.1:11434", model="m", max_tokens=0)


def test_behavior_modules_respect_import_boundary():
    forbidden = ("service.assistant", "service.tools", "service.main", "service.config", "service.inference")
    for name in ("phraser.py",):
        tree = ast.parse((ROOT / "service" / "behavior" / name).read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                assert not mod.startswith(forbidden), f"{name} imports {mod}"
                top = mod.split(".")[0]
                assert top in {"__future__", "httpx", "service"} or top in _stdlib(), f"{name} imports {mod}"
                if top == "service":
                    assert mod.startswith("service.behavior"), f"{name} imports {mod}"


def _stdlib():
    import sys
    return sys.stdlib_module_names


def test_real_engine_uses_phraser_and_payloads_fit_remote_profile(tmp_path):
    """BehaviorEngine + OpenAIPhraser end to end (mock HTTP): phrased bodies
    land on nudges, and every request passes the remote-only profile contract."""
    from service.behavior.engine import BehaviorEngine
    from tests.fixtures.remote_inference import example_profile
    try:
        from tests.fixtures.behavior_persona import generate_persona
        BehaviorEngine(tmp_path / "probe.db").close()
    except (NotImplementedError, ImportError):
        pytest.skip("BehaviorEngine not implemented yet")

    seen = []

    def handler(request):
        body = json.loads(request.content)
        example_profile().validate_payload(body)
        seen.append(body)
        user = body["messages"][1]["content"]
        if user.startswith("Notification kind:"):
            facts = json.loads(user.split("Facts (JSON): ", 1)[1].rsplit("\n/no_think", 1)[0])
            return _reply(f"<think>x</think>Heads up: {facts['body']}")
        return _reply("{}")

    now = 1_791_210_600.0  # Monday 07:30 America/Los_Angeles
    phraser = OpenAIPhraser("http://127.0.0.1:11434", "/v1", model="example-model",
                            transport=httpx.MockTransport(handler))
    eng = BehaviorEngine(tmp_path / "b.db", phraser=phraser, tz="America/Los_Angeles")
    persona = generate_persona(now, tz="America/Los_Angeles")
    eng.observe(persona.observations)
    eng.observe_snapshot("calendar", persona.upcoming, window_start=now - 86400,
                         window_end=now + 14 * 86400, taken_at=now)
    eng.mine(now=now)
    nudges = eng.generate_nudges(now=now)
    # Schedule-sensitive bodies remain deterministic. Exercise wording on an
    # observed-contact nudge instead, retaining the same remote payload checks.
    from service.behavior.types import Nudge, Observation, Pattern
    eng.observe([Observation("sam-observed", "message_in", now - 3 * 3600, subject="Sam", source="messages")])
    eng._store.replace_patterns(eng.patterns() + [Pattern(
        "sam-messages", "contact_rhythm", "Sam", "Recorded messages with Sam",
        evidence=("sam-observed",), stats={"messages": 1, "calls": 0})], now)
    contact = eng._phrase(Nudge(id="contact-probe",kind="contact_window",title="Reply to Sam?",
                                body="Sam messaged you 3h ago.",because="Observed messages",created_at=now,
                                pattern_ids=("sam-messages",)))
    eng.close()
    assert nudges and seen
    assert contact.body.startswith("Heads up:")
    assert all(not n.body.startswith("Heads up:") for n in nudges if n.kind != "contact_window")


@pytest.mark.parametrize("case", ["mixed", "calendar_only", "missing", "wrong_source", "rejected", "message_only"])
def test_contact_wording_requires_retained_message_only_evidence(tmp_path, case):
    from service.behavior.engine import BehaviorEngine
    from service.behavior.types import Nudge, Observation, Pattern
    rec = Recorder(_reply("Recorded message timing suggests this window."))
    phraser = OpenAIPhraser("http://127.0.0.1:11434", "/v1", model="example-model",
                            transport=httpx.MockTransport(rec))
    eng = BehaviorEngine(tmp_path / "b.db", phraser=phraser)
    observation = Observation("evidence", "event" if case == "calendar_only" else "message_in", 100,
                              subject="Sam", source="demo" if case == "wrong_source" else "messages")
    if case != "missing":
        eng.observe([observation])
    p = Pattern("contact", "contact_rhythm", "Sam", "Source timing",
                evidence=("evidence",), stats={"messages": 0 if case == "calendar_only" else 1,
                                              "calls": 1 if case in {"mixed", "calendar_only"} else 0},
                status="rejected" if case == "rejected" else "active")
    eng._store.replace_patterns([p], 200)
    if case == "rejected":
        eng.set_pattern_status(p.id, "rejected")
    n = Nudge("n", "contact_window", "Review window", "Source timing", "counts", 200, pattern_ids=(p.id,))
    result = eng._phrase(n)
    assert len(rec.requests) == (1 if case == "message_only" else 0)
    assert (result != n) == (case == "message_only")
    eng.close()


# ------------------------------------------------- review regressions (repair pass)

@pytest.mark.parametrize("calls,messages", [(0, True), (0, "8"), (0, -1), (0, 1.0),
                                           (0, float("inf")), (None, 1), (False, 1), ("0", 1)])
def test_invalid_contact_counts_cannot_authorize_optional_model_phrasing(tmp_path, calls, messages):
    from service.behavior.engine import BehaviorEngine
    from service.behavior.types import Nudge, Observation, Pattern
    rec = Recorder(_reply("Recorded message timing suggests this window."))
    phraser = OpenAIPhraser("http://127.0.0.1:11434", "/v1", model="example-model",
                            transport=httpx.MockTransport(rec))
    engine = BehaviorEngine(tmp_path / "counts.db", phraser=phraser)
    try:
        engine.observe([Observation("message", "message_in", 100, subject="Sam", source="messages")])
        p = Pattern("contact", "contact_rhythm", "Sam", "Older timing", evidence=("message",),
                    stats=dict(calls=calls, messages=messages, basis="observed", evidence_version=2))
        engine._store.replace_patterns([p], 200)
        n = Nudge("n", "contact_window", "Saved timing", "Unknown timing", "counts", 200, pattern_ids=(p.id,))
        assert not engine._can_phrase(n) and engine._phrase(n) == n
        assert rec.requests == []
    finally:
        engine.close()

def test_echoed_no_think_is_stripped():
    assert make(Recorder(_reply("Gym at 07:00 on Tuesday. /no_think"))).phrase("x", FACTS) == \
        "Gym at 07:00 on Tuesday."
    assert make(Recorder(_reply("/no_think\nTeam sync clashes with gym."))).phrase("x", FACTS) == \
        "Team sync clashes with gym."


@pytest.mark.parametrize("raw", ["Sure! Team sync clashes with gym.", "Here's a rewrite: Team sync clashes with gym.",
                                 "Here’s your note: gym at 07:00.", "Certainly. Gym at 07:00.",
                                 "OK, Team sync clashes with gym."])
def test_leading_filler_falls_back_to_template(raw):
    assert make(Recorder(_reply(raw))).phrase("x", FACTS) is None


def test_phrase_calls_are_rate_capped():
    """Finding 6: like the Mac WispPhraser, at most N phrase() calls per window."""
    clock = [0.0]
    rec = Recorder(*[_reply("Gym at 07:00.") for _ in range(10)])
    p = make(rec, max_phrase_calls=2, phrase_window_s=60.0, clock=lambda: clock[0])
    assert [p.phrase("x", FACTS) for _ in range(3)] == ["Gym at 07:00.", "Gym at 07:00.", None]
    assert len(rec.requests) == 2                                   # the third never hit the network
    clock[0] = 61.0
    assert p.phrase("x", FACTS) == "Gym at 07:00." and len(rec.requests) == 3
    # categorize is not capped (once per mine, cached)
    p.categorize(["Morning lift"], CATEGORIES)
    assert len(rec.requests) == 4


def test_default_phrase_cap_matches_mac_phraser():
    from service.behavior import phraser as ph
    assert ph.PHRASE_MAX_CALLS_PER_WINDOW == 4 and ph.PHRASE_WINDOW_S == 60.0
