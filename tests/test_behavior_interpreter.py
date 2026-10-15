"""Cross-topic semantic proposals using injected fakes only."""

import json
from datetime import datetime, timezone

import pytest

from service.assistant.behavior_interpreter import Interpreter, temporal_choices
from service.behavior.commitments import validate

NOW = datetime(2026, 10, 15, 8, tzinfo=timezone.utc).timestamp()


def record(text):
    return dict(
        id="source:arbitrary",
        source="messages",
        reference={"identity": "arbitrary"},
        ts=NOW,
        text=text,
        direction="incoming",
    )


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Your sculpture assessment is due tomorrow at 4pm UTC.", "preparation"),
        (
            "Please prepare the antenna calibration due tomorrow at 4pm UTC.",
            "preparation",
        ),
        ("Can we arrange a kiln inspection tomorrow at 4pm UTC?", "meeting_request"),
        ("You agreed to bring the spare harness tomorrow at 4pm UTC.", "task"),
    ],
)
def test_topics_are_supplied_by_interpretation_not_rules(text, kind):
    r = record(text)
    times = temporal_choices(text, NOW, "UTC")
    # Questions are deliberately unresolved by the temporal evidence boundary.
    raw = json.dumps(
        dict(
            demands=[
                dict(
                    source_id=r["id"],
                    quote=text,
                    kind=kind,
                    title="Possible preparation",
                    temporal_id=times[0]["id"] if times else None,
                    ownership="user",
                    importance="unknown",
                )
            ],
            patterns=[],
        )
    )
    result = Interpreter(lambda *a, **kw: raw).interpret(
        [r], [], now=NOW, timezone="UTC"
    )
    assert result["demands"][0]["kind"] == kind
    assert result["demands"][0]["importance"] == "unknown"
    assert result["demands"][0]["duration_basis"].startswith("adjustable default")


def test_time_is_anchored_to_original_source_date_not_scan_date():
    text = "The task is due tomorrow at 4pm UTC."
    choices = temporal_choices(text, NOW, "UTC")
    assert (
        choices
        and choices[0]["start"]
        == datetime(2026, 10, 16, 16, tzinfo=timezone.utc).timestamp()
    )
    assert temporal_choices("The task is due next Friday.", NOW, "UTC") == []


@pytest.mark.parametrize(
    "raw", ["", "not json", "[]", '{"actions":["send"]}', '{"demands": {}}']
)
def test_malformed_or_action_schema_returns_unknown(raw):
    result = Interpreter(lambda *a, **k: raw).interpret(
        [record("An arbitrary passage.")], [], now=NOW, timezone="UTC"
    )
    assert result["demands"] == [] and result["patterns"] == []


def test_hallucinated_reference_quote_or_date_never_schedules():
    r = record("The calibration is due tomorrow at 4pm UTC.")
    base = dict(
        source_id=r["id"],
        quote=r["text"],
        kind="preparation",
        title="Prepare",
        temporal_id="invented",
        ownership="user",
    )
    for change in ({"source_id": "fabricated"}, {"quote": "Invented obligation"}):
        assert validate([{**base, **change}], [r], now=NOW) == []
    demand = validate([base], [r], now=NOW)[0]
    assert demand["status"] == "unresolved" and demand["due_ts"] is None


def test_unknown_ownership_is_not_schedulable():
    r = record("Someone's preparation is due tomorrow at 4pm UTC.")
    r["temporal"] = temporal_choices(r["text"], NOW, "UTC")
    p = dict(
        source_id=r["id"],
        quote=r["text"],
        kind="task",
        title="Task",
        temporal_id="0",
        ownership="unknown",
    )
    assert validate([p], [r], now=NOW)[0]["status"] == "unresolved"


def test_model_cannot_publish_arbitrary_pattern_or_evidence():
    p = dict(id="p1", evidence=["o1"], descriptors={"app:arbitrary": 1})
    raw = json.dumps(
        dict(
            demands=[],
            patterns=[
                dict(
                    pattern_id="p1",
                    evidence=["invented"],
                    reason="busy",
                    availability="likely_occupied",
                )
            ],
        )
    )
    result = Interpreter(lambda *a, **k: raw).interpret(
        [], [p], now=NOW, timezone="UTC"
    )
    assert result["patterns"] == []


def test_source_generation_and_disable_invalidate_cached_planning(
    tmp_path, monkeypatch
):
    from service.assistant.behavior_runtime import BehaviorRuntime
    from service.assistant.behavior_sources import Batch
    from service.behavior.engine import BehaviorEngine

    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    current = [True]
    batch = Batch(
        [record("The calibration is due tomorrow at 4pm UTC.")],
        [],
        [lambda: current[0]],
        {},
    )

    class Fake:
        def interpret(self, *a, **k):
            return dict(
                state="tentative",
                patterns=[],
                demands=[{"id": "test", "status": "tentative"}],
            )

    runtime = BehaviorRuntime(
        tmp_path,
        local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: NOW),
        behavior_sources_fn=lambda _: batch,
        behavior_interpreter=Fake(),
        role_key_fn=lambda: "fake-role",
        clock=lambda: NOW,
    )
    try:
        runtime.call("configure_signals", {"email": True, "interpretation": True}, NOW)
        runtime._refresh_availability(NOW)
        assert runtime.planning_context("2026-10-15", "UTC", NOW)["ready"]
        assert batch.records == []
        current[0] = False
        assert not runtime.planning_context("2026-10-15", "UTC", NOW)["ready"]
        runtime.call("configure_signals", {"interpretation": False}, NOW)
        assert runtime._availability_cache is None
    finally:
        runtime.close()


def test_cancelled_admission_never_calls_completion():
    def prohibited(*a, **k):
        raise AssertionError("cancelled request reached completion")

    result = Interpreter(prohibited).interpret(
        [record("Some preparation.")], [], now=NOW, timezone="UTC", admit=lambda: False
    )
    assert result["state"] == "cancelled"


def test_revoke_during_completion_discards_output():
    current = [True]

    def complete(*a, **k):
        current[0] = False
        return '{"demands":[],"patterns":[]}'

    result = Interpreter(complete).interpret(
        [record("Some preparation.")],
        [],
        now=NOW,
        timezone="UTC",
        admit=lambda: current[0],
    )
    assert result["state"] == "cancelled" and not result["demands"]


def test_resident_check_cannot_dispatch_after_revocation(monkeypatch):
    from types import SimpleNamespace
    from service.assistant.behavior_phraser import WispPhraser
    from service.config import endpoints
    from service.inference import remote_policy, omlx_client
    from service import idle

    current, calls = [True], []
    target = SimpleNamespace(
        model="fixture-model",
        endpoint=SimpleNamespace(managed=True, base_url="http://127.0.0.1:8000"),
    )

    class FakeClient:
        managed = True

        def __init__(self, **kw):
            pass

        async def loaded_models(self):
            current[0] = False
            return ["fixture-model"]

        async def chat(self, *a, **kw):
            calls.append(1)
            raise AssertionError("revoked model dispatch")

        async def aclose(self):
            pass

    monkeypatch.setattr(endpoints, "role_target", lambda _: target)
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    monkeypatch.setattr(omlx_client, "OMLXClient", FakeClient)
    monkeypatch.setattr(WispPhraser, "_admit", lambda _: True)
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    result = Interpreter().interpret(
        [record("An arbitrary task.")],
        [],
        now=NOW,
        timezone="UTC",
        remote_allowed=True,
        admit=lambda: current[0],
    )
    assert not calls and result["state"] == "cancelled"


def test_cancelling_tick_cannot_publish_late_interpretation(tmp_path, monkeypatch):
    import asyncio
    import threading
    from service.assistant.behavior_runtime import BehaviorRuntime
    from service.assistant.behavior_sources import Batch
    from service.behavior.engine import BehaviorEngine

    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Fake:
        def interpret(self, *a, **kw):
            entered.set()
            assert release.wait(3)
            return dict(state="tentative", patterns=[], demands=[])

    runtime = BehaviorRuntime(
        tmp_path,
        local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: NOW),
        behavior_sources_fn=lambda _: Batch(
            [record("The calibration is due tomorrow.")], [], [], {}
        ),
        behavior_interpreter=Fake(),
        role_key_fn=lambda: "fake",
        clock=lambda: NOW,
    )
    runtime.call("configure_signals", {"email": True, "interpretation": True}, NOW)
    runtime.expire_events = lambda *a: None

    def work(*a, **kw):
        try:
            runtime._refresh_availability(NOW)
            return [], "local"
        finally:
            finished.set()

    runtime.work = work

    async def run():
        task = asyncio.create_task(runtime.tick(None, None, now=NOW))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)

    try:
        asyncio.run(run())
        assert runtime._availability_cache is None
    finally:
        release.set()
        runtime.close()
