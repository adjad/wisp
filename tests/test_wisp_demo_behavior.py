"""demo/wisp_demo.py behavior-engine subcommands: HTTP calls against the
running backend (mocked), persona delegation and the drop presets. Nothing
here contacts a backend or runs osascript."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _no_osascript(*args, **kwargs):
    raise AssertionError("a test reached the real osascript")


@pytest.fixture()
def demo():
    spec = importlib.util.spec_from_file_location("wisp_demo_behavior_test", ROOT / "demo/wisp_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Every seed module the CLI loads is off-macOS and cannot run osascript
    # unless a test opts in (see _mac_seed). Without this, the macOS CI runner
    # took the real path and drove Calendar.app.
    real_seed_module = module._seed_module

    def guarded_seed_module():
        seed = real_seed_module()
        seed.is_macos = lambda: False
        seed.run_osascript = _no_osascript
        seed.subprocess = SimpleNamespace(run=_no_osascript, PIPE=None, TimeoutExpired=TimeoutError)
        return seed

    module._seed_module = guarded_seed_module
    return module


class FakeBackend:
    """Stands in for urllib.request.urlopen; records (method, path, body)."""

    def __init__(self, routes=None):
        self.calls = []
        self.routes = routes or {}

    def __call__(self, req, timeout=None):
        path = req.full_url.replace("http://127.0.0.1:8765", "")
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, body))
        status, payload = self.routes.get((req.get_method(), path.split("?")[0]), (200, {"ok": True}))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(json.dumps(payload).encode()))
        return _Resp(status, json.dumps(payload).encode())


class _Resp:
    def __init__(self, status, raw):
        self.status, self._raw = status, raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def backend(demo, monkeypatch):
    fake = FakeBackend()

    monkeypatch.setattr(demo.urllib.request, "urlopen", fake)
    return fake


def args(**kw):
    base = dict(action="status", json=False, confirm=None, reject=None, reactivate=None,
                feedback=None, undo=None, act=None)
    base.update(kw)
    return SimpleNamespace(**base)


# --- behavior subcommands -------------------------------------------------------

@pytest.mark.parametrize("action,path", [("status", "/behavior/status"), ("patterns", "/behavior/patterns"),
                                         ("predict", "/behavior/predict"), ("profile", "/behavior/profile"),
                                         ("nudges", "/behavior/nudges")])
def test_read_actions_use_get(demo, backend, action, path, capsys):
    demo.cmd_behavior(args(action=action))
    assert backend.calls == [("GET", path, None)]


def test_run_posts_and_summarizes(demo, backend, capsys):
    backend.routes[("POST", "/behavior/run")] = (200, {"ok": True, "published": 2,
                                                       "nudge_ids": ["n1", "n2"], "status": {}})
    demo.cmd_behavior(args(action="run"))
    assert backend.calls == [("POST", "/behavior/run", {})]
    assert "published 2 nudge(s): n1, n2" in capsys.readouterr().out


def test_patterns_print_statement_and_confidence(demo, backend, capsys):
    backend.routes[("GET", "/behavior/patterns")] = (200, {"patterns": [{
        "id": "p1", "kind": "habit", "subject": "fitness", "status": "active", "confidence": 0.81,
        "statement": "You lift Mon/Wed/Fri mornings", "support": 20, "opportunities": 24}]})
    demo.cmd_behavior(args(action="patterns"))
    out = capsys.readouterr().out
    assert "0.81" in out and "You lift Mon/Wed/Fri mornings" in out and "id=p1" in out


def test_pattern_status_changes(demo, backend):
    demo.cmd_behavior(args(action="patterns", confirm="p1"))
    demo.cmd_behavior(args(action="patterns", reject="p 2"))
    demo.cmd_behavior(args(action="patterns", reactivate="p3"))
    assert backend.calls == [("POST", "/behavior/patterns/p1", {"status": "confirmed"}),
                             ("POST", "/behavior/patterns/p%202", {"status": "rejected"}),
                             ("POST", "/behavior/patterns/p3", {"status": "active"})]


def test_nudge_feedback_undo_act(demo, backend):
    demo.cmd_behavior(args(action="nudges", feedback=["n1", "not_now"]))
    demo.cmd_behavior(args(action="nudges", undo="n1"))
    demo.cmd_behavior(args(action="nudges", act=["n1", "1"]))
    assert backend.calls == [("POST", "/behavior/nudges/n1/feedback", {"verdict": "not_now"}),
                             ("POST", "/behavior/nudges/n1/undo", {}),
                             ("POST", "/behavior/nudges/n1/act", {"action_index": 1})]


def test_bad_verdict_is_refused_locally(demo, backend):
    with pytest.raises(SystemExit):
        demo.cmd_behavior(args(action="nudges", feedback=["n1", "meh"]))
    assert backend.calls == []


def test_missing_endpoints_are_explained(demo, backend, capsys):
    backend.routes[("POST", "/behavior/clear")] = (404, {"detail": "Not Found"})
    backend.routes[("GET", "/behavior/nudges")] = (404, {"detail": "Not Found"})
    demo.cmd_behavior(args(action="clear"))
    demo.cmd_behavior(args(action="nudges"))
    out = capsys.readouterr().out
    assert "no /behavior/clear endpoint" in out and "behavior.db" in out
    assert "does not list nudges" in out


def test_unreachable_backend_exits_cleanly(demo, monkeypatch):
    def down(req, timeout=None):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(demo.urllib.request, "urlopen", down)
    with pytest.raises(SystemExit) as exc:
        demo.cmd_behavior(args(action="status"))
    assert "not reachable" in str(exc.value)


# --- persona delegation -------------------------------------------------------------

def test_persona_delegates_to_seed_script(demo, monkeypatch):
    seen = []
    monkeypatch.setattr(demo, "_seed_module", lambda: SimpleNamespace(main=lambda argv: seen.append(argv) or 0))
    demo.cmd_persona(SimpleNamespace(action="seed", rest=["--all", "--dry-run"]))
    demo.cmd_persona(SimpleNamespace(action="remove", rest=["--yes"]))
    assert seen == [["seed", "--all", "--dry-run"], ["remove", "--yes"]]


def test_persona_cli_parses_passthrough_flags(demo, monkeypatch):
    seen = []
    monkeypatch.setattr(demo, "_seed_module", lambda: SimpleNamespace(main=lambda argv: seen.append(argv) or 0))
    monkeypatch.setattr(sys, "argv", ["wisp_demo.py", "persona", "seed", "--calendar", "--dry-run"])
    demo.main()
    assert seen == [["seed", "--calendar", "--dry-run"]]


# --- drop presets ---------------------------------------------------------------------

def _day(weekday):
    """A noon timestamp on the next date with the given weekday (0=Mon)."""
    d = datetime(2026, 10, 5, 12, 0)   # a Monday
    return (d + timedelta(days=weekday)).timestamp()


def test_gym_conflict_tomorrow_when_gym_day(demo):
    items = demo.behavior_drop_items("gym-conflict", _day(1), (0, 2, 4))   # Tuesday -> Wednesday
    [event] = items["events"]
    start, end = datetime.fromtimestamp(event["start_ts"]), datetime.fromtimestamp(event["end_ts"])
    assert start.weekday() == 2 and (start.hour, start.minute) == (7, 0) and (end.hour, end.minute) == (7, 45)
    assert start.date() == datetime.fromtimestamp(_day(1)).date() + timedelta(days=1)


def test_gym_conflict_skips_to_next_gym_day(demo):
    items = demo.behavior_drop_items("gym-conflict", _day(4), (0, 2, 4))   # Friday -> Monday
    start = datetime.fromtimestamp(items["events"][0]["start_ts"])
    assert start.weekday() == 0 and (start.date() - datetime.fromtimestamp(_day(4)).date()).days == 3
    assert items["events"][0]["demo_id"] == f"drop-gym-conflict-{start:%Y%m%d}"


def test_backtoback_is_four_contiguous_meetings_tomorrow(demo):
    now = _day(0)
    events = demo.behavior_drop_items("backtoback", now)["events"]
    assert len(events) == 4
    starts = [datetime.fromtimestamp(e["start_ts"]) for e in events]
    assert [(s.hour, s.minute) for s in starts] == [(14, 0), (14, 30), (15, 0), (15, 30)]
    assert datetime.fromtimestamp(events[-1]["end_ts"]).hour == 16
    assert all(events[i]["end_ts"] == events[i + 1]["start_ts"] for i in range(3))
    assert {s.date() for s in starts} == {datetime.fromtimestamp(now).date() + timedelta(days=1)}
    assert len({e["demo_id"] for e in events}) == 4


def test_dev_preset_adds_nothing_to_the_mac(demo):
    items = demo.behavior_drop_items("dev", _day(2))
    assert items["events"] == [] and items["reminders"] == []


def test_gym_weekdays_from_learned_pattern(demo, backend):
    backend.routes[("GET", "/behavior/patterns")] = (200, {"patterns": [
        {"kind": "habit", "subject": "fitness", "statement": "Morning lift", "status": "active",
         "window": {"weekdays": [1, 3], "start_minute": 405, "end_minute": 465}}]})
    assert demo._gym_weekdays() == (1, 3)


def test_gym_weekdays_fallback_matches_persona(demo, monkeypatch):
    def down(req, timeout=None):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(demo.urllib.request, "urlopen", down)
    assert demo._gym_weekdays() == (0, 2, 4)


def test_drop_dry_run_prints_applescript_only(demo, monkeypatch, capsys):
    monkeypatch.setattr(demo.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no HTTP on dry run"))
    seed = demo._seed_module()
    monkeypatch.setattr(seed.subprocess, "run", lambda *a, **k: pytest.fail("no osascript on dry run"))
    monkeypatch.setattr(demo, "_seed_module", lambda: seed)
    demo.cmd_drop(SimpleNamespace(preset="backtoback", dry_run=True, no_run=False))
    out = capsys.readouterr().out
    assert 'tell application "Calendar"' in out and out.count("make new event") == 4
    assert "[wisp-demo-id:drop-backtoback-" in out


def test_drop_writes_calendar_then_runs(demo, backend, monkeypatch, capsys):
    seed = demo._seed_module()
    scripts = []
    monkeypatch.setattr(seed, "is_macos", lambda: True)
    monkeypatch.setattr(seed, "run_osascript", lambda script, timeout=900: scripts.append(script) or
                        "created 1 skipped 0")
    monkeypatch.setattr(demo, "_seed_module", lambda: seed)
    backend.routes[("POST", "/behavior/run")] = (200, {"ok": True, "published": 1, "nudge_ids": ["nx"]})
    demo.cmd_drop(SimpleNamespace(preset="gym-conflict", dry_run=False, no_run=False))
    assert len(scripts) == 1 and 'calendar "Wisp Demo"' in scripts[0]
    assert backend.calls[-1] == ("POST", "/behavior/run", {"reason": "demo_drop", "preset": "gym-conflict"})
    assert "published 1 nudge(s): nx" in capsys.readouterr().out


def test_drop_refuses_off_macos_without_dry_run(demo, monkeypatch):
    seed = demo._seed_module()
    monkeypatch.setattr(seed, "is_macos", lambda: False)
    monkeypatch.setattr(demo, "_seed_module", lambda: seed)
    with pytest.raises(SystemExit):
        demo.cmd_drop(SimpleNamespace(preset="backtoback", dry_run=False, no_run=False))


def test_dev_drop_runs_and_shows_prediction(demo, backend, monkeypatch, capsys):
    seed = demo._seed_module()
    monkeypatch.setattr(seed, "is_macos", lambda: False)   # no Mac writes needed
    monkeypatch.setattr(demo, "_seed_module", lambda: seed)
    backend.routes[("POST", "/behavior/run")] = (200, {"ok": True, "published": 0, "nudge_ids": []})
    backend.routes[("GET", "/behavior/predict")] = (200, {"predictions": [
        {"kind": "contact", "subject": "Dev Malik", "ts": _day(2), "probability": 0.8,
         "because": "You message Dev most weekday afternoons"}]})
    demo.cmd_drop(SimpleNamespace(preset="dev", dry_run=False, no_run=False))
    assert [c[:2] for c in backend.calls] == [("POST", "/behavior/run"), ("GET", "/behavior/predict")]
    assert "Dev Malik" in capsys.readouterr().out


def test_catch_presets_still_available(demo):
    for name in ("natural", "quad", "dinner", "known", "promo", "scam"):
        assert name in demo.PRESETS
    assert not set(demo.PRESETS) & set(demo.BEHAVIOR_PRESETS)


# --- rehearsal reset: drops leave the "Wisp Demo" calendar before the reset ----------

def _mac_seed(demo, monkeypatch, output="removed 2"):
    seed = demo._seed_module()
    scripts = []
    monkeypatch.setattr(seed, "is_macos", lambda: True)
    monkeypatch.setattr(seed, "run_osascript", lambda script, timeout=900: scripts.append(script) or output)
    monkeypatch.setattr(demo, "_seed_module", lambda: seed)
    return scripts


def test_cleanup_script_deletes_only_drop_events_in_wisp_demo(demo):
    script = demo.drop_cleanup_script()
    assert 'calendar "Wisp Demo"' in script and '"[wisp-demo-id:drop-"' in script
    assert "delete (first event of wispCal whose uid is" in script
    # Never the calendar itself, never another calendar.
    assert "delete (first calendar" not in script and "every calendar" not in script


def test_cleanup_script_bulk_fetches_instead_of_walking_event_references(demo):
    import re
    script = demo.drop_cleanup_script()
    # `repeat with ev in (every event ... whose ...)` re-runs the filtered query on
    # every property read (quadratic; timed out on ~200 events).
    assert not re.search(r"repeat with \w+ in \(every event", script)
    assert "set uidList to uid of (every event of wispCal whose" in script
    assert "set descList to description of (every event of wispCal whose" in script
    assert "repeat with i from 1 to (count of descList)" in script


@pytest.mark.parametrize("action", ["clear", "reset"])
def test_reset_removes_drops_waits_for_sync_then_clears(demo, backend, monkeypatch, action, capsys):
    scripts = _mac_seed(demo, monkeypatch)
    syncs = iter([100.0, 100.0, 9e12])            # baseline, stale, then a fresh sync
    monkeypatch.setattr(demo, "_calendar_last_sync", lambda: next(syncs))
    slept = []
    monkeypatch.setattr(demo.time, "sleep", slept.append)
    demo.cmd_behavior(args(action=action, wait=True, dry_run=False))
    assert len(scripts) == 1 and "[wisp-demo-id:drop-" in scripts[0]
    assert slept == [demo.SYNC_POLL_S]
    assert backend.calls == [("POST", "/behavior/clear", {})]
    out = capsys.readouterr().out
    assert "removed 2 rehearsal drop event(s)" in out and "Calendar synced" in out


def test_reset_without_wait_does_not_clear_while_calendar_is_stale(demo, backend, monkeypatch, capsys):
    _mac_seed(demo, monkeypatch, output="removed 1")
    demo.cmd_behavior(args(action="reset", wait=False, dry_run=False))
    assert backend.calls == []                       # clearing now would refire the rehearsal drop
    assert "behavior reset --wait" in capsys.readouterr().out


def test_reset_with_no_drops_clears_at_once(demo, backend, monkeypatch):
    _mac_seed(demo, monkeypatch, output="removed 0")
    demo.cmd_behavior(args(action="reset", wait=True, dry_run=False))
    assert backend.calls == [("POST", "/behavior/clear", {})]


def test_reset_dry_run_and_off_macos(demo, backend, monkeypatch, capsys):
    seed = demo._seed_module()
    monkeypatch.setattr(seed, "run_osascript", lambda *a, **k: pytest.fail("no osascript"))
    monkeypatch.setattr(demo, "_seed_module", lambda: seed)
    demo.cmd_behavior(args(action="reset", wait=True, dry_run=True))
    assert backend.calls == [] and "[wisp-demo-id:drop-" in capsys.readouterr().out
    monkeypatch.setattr(seed, "is_macos", lambda: False)
    demo.cmd_behavior(args(action="reset", wait=False, dry_run=False))
    assert backend.calls == [("POST", "/behavior/clear", {})]
    assert "macOS only" in capsys.readouterr().out


def test_wait_falls_back_to_a_fixed_sleep_without_sync_status(demo, monkeypatch):
    monkeypatch.setattr(demo, "_calendar_last_sync", lambda: None)
    slept = []
    demo._wait_for_calendar_sync(0.0, sleep=slept.append)
    assert slept == [demo.SYNC_WAIT_S]


def test_redrop_still_runs_and_shows_the_existing_nudge(demo, backend, monkeypatch, capsys):
    _mac_seed(demo, monkeypatch, output="created 0 skipped 1")
    backend.routes[("POST", "/behavior/run")] = (200, {"ok": True, "published": 0, "nudge_ids": []})
    backend.routes[("GET", "/behavior/nudges")] = (200, {"nudges": [
        {"id": "n7", "kind": "habit_conflict", "title": "Foundry export check-in overlaps your workout",
         "body": "", "because": "", "delivered_at": _day(1), "verdict": None, "can_undo": True,
         "actions": [{"type": "add_reminder", "label": "Remind me", "auto": True}]},
        {"id": "n8", "kind": "cancel_likely", "title": "You might skip Board deck prep", "body": "",
         "because": "", "actions": []}]})
    demo.cmd_drop(SimpleNamespace(preset="gym-conflict", dry_run=False, no_run=False))
    assert [c[:2] for c in backend.calls][-2:] == [("POST", "/behavior/run"), ("GET", "/behavior/nudges")]
    out = capsys.readouterr().out
    assert "1 already there" in out and "its nudge" in out and "Undo available" in out
    assert "Foundry export check-in" in out and "Board deck prep" not in out


def test_run_reports_busy_and_calendar_wait(demo, backend, capsys):
    backend.routes[("POST", "/behavior/run")] = (200, {"ok": False, "busy": True, "published": 0,
                                                      "detail": "another behavior run is still in progress"})
    demo.cmd_behavior(args(action="run"))
    assert "busy" in capsys.readouterr().out
    backend.routes[("POST", "/behavior/run")] = (200, {"ok": True, "published": 0, "nudge_ids": [],
                                                      "status": {"waiting_for_calendar": True}})
    demo.cmd_behavior(args(action="run"))
    assert "first Calendar read" in capsys.readouterr().out
