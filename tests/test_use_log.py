"""Learn from my use (service/inference/use_log.py): the private local recorder.
Invented data only; the engine is faked."""
import asyncio
import copy
import json
import os
import stat
import time
from datetime import datetime, timedelta

import pytest

from service.inference import use_log
from service.inference.omlx_client import OMLXClient
from service.safety import cloud_mask

FAKE_KEY = "sk-test-FAKEKEY0123456789abcdefghijklmnop"       # invented, key-shaped
CRED_TEXT = "my token is ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"   # invented


@pytest.fixture(autouse=True)
def log(tmp_path, monkeypatch):
    folder = tmp_path / "state" / "use-log"
    monkeypatch.setattr(use_log, "log_dir", lambda: folder)
    monkeypatch.setattr(use_log, "_exclude_from_backup", lambda p: None)
    monkeypatch.setattr(use_log, "_enabled", True)
    monkeypatch.setattr(use_log, "_dir_ready", None)
    monkeypatch.setattr(use_log, "_last_prune", 0.0)
    use_log._last.clear()
    yield folder
    use_log.flush()
    use_log._last.clear()


def _client(*, managed=True, name="local", url="http://127.0.0.1:8000"):
    c = OMLXClient.__new__(OMLXClient)
    c.managed, c.endpoint_name, c.base_url = managed, name, url
    c.api_key = FAKE_KEY
    c.headers = {"Authorization": f"Bearer {FAKE_KEY}"}
    return c


def _fake_engine(monkeypatch, reply=None):
    reply = reply or {"role": "assistant", "content": "Hello Ada.", "tool_calls": None}

    async def fake_chat(self, model, msgs, **k):
        return {"choices": [{"message": copy.deepcopy(reply)}]}

    async def fake_events(self, model, msgs, **k):
        yield {"kind": "content", "text": "Hello"}
        yield {"kind": "final", "message": copy.deepcopy(reply)}

    monkeypatch.setattr(OMLXClient, "_chat", fake_chat)
    monkeypatch.setattr(OMLXClient, "_stream_events", fake_events)


def _ask(client, messages, conv="conv-1", stream=False, **kw):
    async def run():
        with cloud_mask.conversation(conv):
            if stream:
                return [e async for e in client.stream_events("gemma", messages, **kw)]
            return await client.chat("gemma", messages, **kw)
    out = asyncio.run(run())
    use_log.flush()
    return out


def _lines(folder):
    out = []
    for p in sorted(folder.glob("use-*.jsonl")) if folder.exists() else []:
        out += [json.loads(x) for x in p.read_text().splitlines()]
    return out


MSGS = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi there"}]


def test_on_by_default_and_records_a_managed_exchange(log, monkeypatch):
    monkeypatch.setattr(use_log, "_enabled", None)       # read the real default
    assert use_log.enabled() is True
    _fake_engine(monkeypatch)
    tools = [{"type": "function", "function": {"name": "calendar_list", "description": "x" * 50,
                                               "parameters": {"type": "object"}}}]
    _ask(_client(), MSGS, tools=tools)
    [rec] = _lines(log)
    assert rec["v"] == 1 and rec["kind"] == "exchange" and rec["conversation"] == "conv-1"
    assert rec["model"] == "gemma" and rec["endpoint"] == "local" and rec["id"]
    assert rec["messages"] == MSGS
    assert rec["tools"] == ["calendar_list"]
    assert rec["reply"] == {"content": "Hello Ada.", "tool_calls": None}


def test_stream_records_the_final_event_only(log, monkeypatch):
    _fake_engine(monkeypatch)
    events = _ask(_client(), MSGS, stream=True)
    assert [e["kind"] for e in events] == ["content", "final"]
    [rec] = _lines(log)
    assert rec["reply"]["content"] == "Hello Ada."


def test_off_stops_writes_immediately(log, monkeypatch):
    _fake_engine(monkeypatch)
    _ask(_client(), MSGS)
    assert len(_lines(log)) == 1
    monkeypatch.setattr(use_log, "_enabled", False)
    _ask(_client(), MSGS)
    _ask(_client(), MSGS, stream=True)
    assert len(_lines(log)) == 1


def test_switch_off_drops_lines_already_queued(log, monkeypatch):
    _fake_engine(monkeypatch)
    use_log._queue.put((use_log._epoch, {"kind": "exchange", "v": 1, "id": "q", "ts": 1,
                                         "conversation": "c", "reply": {}}))
    monkeypatch.setattr(use_log, "_enabled", False)
    use_log.flush()
    assert _lines(log) == []


@pytest.mark.parametrize("kw", [
    dict(managed=False, name="remote", url="https://openrouter.ai"),
    dict(managed=False, name="cloud", url="https://generativelanguage.googleapis.com"),
    dict(managed=False, name="local_provider", url="http://127.0.0.1:1234"),
    dict(managed=False, name="remote", url="http://localhost:9000"),
])
def test_cloud_and_loopback_provider_are_never_recorded(log, monkeypatch, kw):
    _fake_engine(monkeypatch)
    _ask(_client(**kw), MSGS)
    _ask(_client(**kw), MSGS, stream=True)
    assert _lines(log) == [] and not log.exists()


def test_no_conversation_means_no_record(log, monkeypatch):
    _fake_engine(monkeypatch)
    asyncio.run(_client().chat("gemma", MSGS))
    use_log.flush()
    assert _lines(log) == []


def test_key_and_credentials_never_reach_the_file(log, monkeypatch):
    _fake_engine(monkeypatch, {"role": "assistant", "content": "ok " + CRED_TEXT, "tool_calls": None})
    msgs = [{"role": "user", "content": CRED_TEXT},
            {"role": "user", "content": "api_key: " + FAKE_KEY}]
    _ask(_client(), msgs, extra_headers={"Authorization": f"Bearer {FAKE_KEY}"})
    text = "".join(p.read_text() for p in log.glob("use-*.jsonl"))
    assert text
    assert FAKE_KEY not in text and "ghp_a1B2" not in text and "Bearer" not in text
    assert "redacted credential" in text


def test_followup_is_linked_for_tool_results_and_next_user_message(log, monkeypatch):
    call = {"id": "call_1", "type": "function", "function": {"name": "calendar_list", "arguments": "{}"}}
    _fake_engine(monkeypatch, {"role": "assistant", "content": "", "tool_calls": [call]})
    _ask(_client(), MSGS)
    _fake_engine(monkeypatch, {"role": "assistant", "content": "You are free at 3.", "tool_calls": None})
    msgs2 = MSGS + [{"role": "assistant", "content": "", "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "Error: calendar is locked"},
                    {"role": "tool", "tool_call_id": "other", "content": "unrelated"}]
    _ask(_client(), msgs2)
    use_log.note_user_message("conv-1", "thanks, and tomorrow?")
    use_log.flush()
    rows = _lines(log)
    first, second = [r for r in rows if r["kind"] == "exchange"]
    tool_fu, user_fu = [r for r in rows if r["kind"] == "followup"]
    assert tool_fu["ref"] == first["id"]
    assert tool_fu["tool_results"] == [{"tool_call_id": "call_1",
                                        "content": "Error: calendar is locked", "error": True}]
    assert user_fu["ref"] == second["id"] and user_fu["user_message"] == "thanks, and tomorrow?"


def test_modes_are_private(log, monkeypatch):
    _fake_engine(monkeypatch)
    _ask(_client(), MSGS)
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o700
    [f] = log.glob("use-*.jsonl")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def _seed(folder, days_ago, size):
    folder.mkdir(parents=True, exist_ok=True)
    day = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    path = folder / f"use-{day}.jsonl"
    path.write_text("x" * size)
    return path


def test_age_cap_drops_old_files(log, monkeypatch):
    _fake_engine(monkeypatch)
    old, recent = _seed(log, 100, 10), _seed(log, 2, 10)
    monkeypatch.setattr(use_log, "MAX_AGE_DAYS", 90)
    _ask(_client(), MSGS)
    assert not old.exists() and recent.exists()


def test_size_cap_drops_oldest_first(log, monkeypatch):
    _fake_engine(monkeypatch)
    a, b, c = _seed(log, 30, 400), _seed(log, 20, 400), _seed(log, 10, 400)
    monkeypatch.setattr(use_log, "MAX_BYTES", 1000)
    _ask(_client(), MSGS)
    assert not a.exists() and b.exists() and c.exists()
    assert use_log.usage()["bytes"] <= 1000


def test_a_line_over_the_cap_is_dropped_not_written(log, monkeypatch):
    _fake_engine(monkeypatch)
    monkeypatch.setattr(use_log, "MAX_BYTES", 50)
    _ask(_client(), MSGS)
    assert use_log.usage()["bytes"] <= 50


def test_prune_runs_at_most_every_few_minutes(log, monkeypatch):
    _fake_engine(monkeypatch)
    _ask(_client(), MSGS)
    old = _seed(log, 200, 5)
    _ask(_client(), MSGS)                       # within PRUNE_EVERY_S of the last check
    assert old.exists()
    monkeypatch.setattr(use_log, "_last_prune", time.time() - 10_000)
    _ask(_client(), MSGS)
    assert not old.exists()


def test_delete_removes_everything_and_reports_bytes(log, monkeypatch):
    _fake_engine(monkeypatch)
    _ask(_client(), MSGS)
    _ask(_client(), MSGS)
    size = use_log.usage()["bytes"]
    assert size > 0
    assert use_log.delete_all() == size
    assert use_log.usage() == {"bytes": 0, "files": 0}
    use_log.note_user_message("conv-1", "after delete")    # no exchange left to link to
    use_log.flush()
    assert _lines(log) == []


def test_writer_failure_never_raises_into_the_ask(log, tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    blocker = tmp_path / "state"
    blocker.mkdir()
    blocker.chmod(0o500)                          # read-only: the folder cannot be created
    try:
        data = _ask(_client(), MSGS)
        events = _ask(_client(), MSGS, stream=True)
    finally:
        blocker.chmod(0o700)
    assert data["choices"][0]["message"]["content"] == "Hello Ada."
    assert events[-1]["kind"] == "final"
    assert use_log._thread.is_alive()


def test_queue_overflow_drops_instead_of_blocking(log, monkeypatch):
    monkeypatch.setattr(use_log, "_queue", use_log.queue.Queue(maxsize=1))
    monkeypatch.setattr(use_log, "_thread", type("T", (), {"is_alive": lambda s: True})())
    use_log._enqueue({"kind": "user", "conversation": "c", "ts": 1, "text": "a"})
    use_log._enqueue({"kind": "user", "conversation": "c", "ts": 1, "text": "b"})
    assert use_log._queue.qsize() == 1


def test_recording_leaves_request_and_response_untouched(log, monkeypatch):
    seen = {}
    reply = {"role": "assistant", "content": "Hello Ada.", "tool_calls": None}

    async def fake_chat(self, model, msgs, **k):
        seen["msgs"] = msgs
        return {"choices": [{"message": reply}]}

    async def fake_events(self, model, msgs, **k):
        seen["stream_msgs"] = msgs
        yield {"kind": "final", "message": reply}

    monkeypatch.setattr(OMLXClient, "_chat", fake_chat)
    monkeypatch.setattr(OMLXClient, "_stream_events", fake_events)
    msgs = copy.deepcopy(MSGS)
    before = copy.deepcopy(msgs)
    data = _ask(_client(), msgs)
    events = _ask(_client(), msgs, stream=True)
    assert seen["msgs"] is msgs and seen["stream_msgs"] is msgs and msgs == before
    assert data["choices"][0]["message"] is reply
    assert events[-1]["message"] is reply and reply["content"] == "Hello Ada."


def test_later_changes_to_messages_do_not_change_the_record(log, monkeypatch):
    _fake_engine(monkeypatch)
    msgs = copy.deepcopy(MSGS)
    _ask(_client(), msgs)
    msgs[1]["content"] = "changed"
    assert _lines(log)[0]["messages"][1]["content"] == "Hi there"


def test_failed_request_records_nothing(log, monkeypatch):
    async def boom(self, model, msgs, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr(OMLXClient, "_chat", boom)
    with pytest.raises(RuntimeError):
        _ask(_client(), MSGS)
    use_log.flush()
    assert _lines(log) == []


def test_images_are_not_stored(log, monkeypatch):
    _fake_engine(monkeypatch)
    msgs = [{"role": "user", "content": [{"type": "image_url",
                                          "image_url": {"url": "data:image/png;base64," + "A" * 500}},
                                         {"type": "text", "text": "what is this"}]}]
    _ask(_client(), msgs)
    [rec] = _lines(log)
    assert rec["messages"][0]["content"][0]["image_url"]["url"] == "[image omitted]"


# ---- the setting and its endpoints ----

def test_endpoints_toggle_and_delete(log, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.use_log_api import router

    app = FastAPI()
    app.include_router(router)
    api = TestClient(app)
    monkeypatch.setattr(use_log, "set_enabled", lambda on: setattr(use_log, "_enabled", bool(on)) or on)
    _fake_engine(monkeypatch)
    _ask(_client(), MSGS)
    got = api.get("/use_log").json()
    assert got["enabled"] is True and got["bytes"] > 0 and got["files"] == 1
    assert "messages" not in json.dumps(got)
    assert api.post("/use_log", json={"enabled": False}).json()["enabled"] is False
    assert api.post("/use_log", json={"enabled": "no"}).status_code == 422
    assert api.post("/use_log", json={"enabled": True}, headers={"origin": "https://x.test"}).status_code == 403
    out = api.post("/use_log/delete", json={}).json()
    assert out["deleted_bytes"] == got["bytes"] and out["bytes"] == 0


def test_setting_persists_in_the_overlay_default_on():
    from service import config
    assert config.get_learn_from_use_enabled() is True
    try:
        assert config.set_learn_from_use_enabled(False) is False
        assert config.get_learn_from_use_enabled() is False
        with pytest.raises(ValueError):
            config.set_learn_from_use_enabled("off")
    finally:
        config.set_learn_from_use_enabled(True)
    assert config.get_learn_from_use_enabled() is True


def test_a_broken_recorder_never_fails_or_changes_a_request(monkeypatch):
    """If the use log itself raises (or is missing from a trimmed service copy), the request
    goes ahead unrecorded."""
    from service.inference import omlx_client, use_log

    def boom(*a, **k):
        raise RuntimeError("recorder broke")
    monkeypatch.setattr(use_log, "start", boom)
    assert omlx_client._start_use_log(object(), "m", [{"role": "user", "content": "hi"}], {}) is None
