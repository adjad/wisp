"""demo/seed_mac_persona.py: AppleScript generation for seeding the fictional
persona into the Mac's real Calendar/Reminders/Notes. No osascript runs here;
subprocess is always mocked."""
from __future__ import annotations

import importlib.util
import io
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def smp():
    spec = importlib.util.spec_from_file_location("seed_mac_persona_test", ROOT / "demo/seed_mac_persona.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Safe by default even on a macOS runner: tests opt in to "macOS" and to a
    # fake subprocess explicitly, so nothing can reach the real osascript.
    module.is_macos = lambda: False
    module.subprocess = SimpleNamespace(run=no_subprocess, TimeoutExpired=TimeoutError)
    return module


BASE = datetime(2026, 10, 12, 7, 0).timestamp()


def fake_persona(n_events=3, n_reminders=2, n_notes=2):
    return SimpleNamespace(
        calendar_events=[{"demo_id": f"cal-{i}", "title": f"Event {i}", "start_ts": BASE + i * 3600,
                          "end_ts": BASE + i * 3600 + 1800, "location": "Cedarloop HQ",
                          "notes": "", "calendar": "Wisp Demo"} for i in range(n_events)],
        reminders=[{"demo_id": f"rem-{i}", "title": f"Reminder {i}", "due_ts": BASE + i * 86400,
                    "completed": i % 2 == 0, "list": "Wisp Demo", "notes": ""} for i in range(n_reminders)],
        notes=[{"demo_id": f"note-{i}", "title": f"Note {i}", "body": "line one\nline two",
                "folder": "Wisp Demo"} for i in range(n_notes)],
        observations=[], upcoming=[], expected={},
    )


class FakeRun:
    def __init__(self, stdout="created 1 skipped 0", returncode=0, stderr=""):
        self.calls = []
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input", "")))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def no_subprocess(*a, **kw):
    raise AssertionError("osascript must not run")


# --- escaping ---------------------------------------------------------------

def test_as_string_escapes_quotes_and_backslashes(smp):
    assert smp.as_string('say "hi"') == '"say \\"hi\\""'
    assert smp.as_string("a\\b") == '"a\\\\b"'
    assert smp.as_string("") == '""'
    assert smp.as_string(None) == '""'


def test_as_string_newlines_tabs_and_control_chars(smp):
    assert smp.as_string("a\nb") == '("a" & linefeed & "b")'
    assert smp.as_string("a\r\nb") == '("a" & linefeed & "b")'
    assert smp.as_string("a\tb") == '("a" & tab & "b")'
    assert smp.as_string("a\x00\x07b") == '"ab"'


def test_as_string_unicode_is_ascii_safe(smp):
    out = smp.as_string("café — 😀")
    assert out.isascii()
    assert "(character id 233)" in out
    assert "(character id 128512)" in out
    assert smp.as_string("ü—") == "(string id {252, 8212})"


def test_injection_attempt_stays_inside_string(smp):
    evil = '" & (do shell script "rm -rf ~") & "'
    out = smp.as_string(evil)
    # Every quote in the payload is escaped, so the literal never closes early.
    assert re.fullmatch(r'"(?:[^"\\]|\\.)*"', out)


def test_marker_is_bracketed_and_sanitized(smp):
    assert smp.marker("cal-1") == "[wisp-demo-id:cal-1]"
    assert smp.marker("cal-1") not in smp.marker("cal-10")
    assert smp.marker('x" & y') == "[wisp-demo-id:x----y]"


def test_as_date_uses_local_fields_not_locale_strings(smp):
    ts = datetime(2026, 2, 28, 18, 45, 30).timestamp()
    assert smp.as_date(ts) == "my wispDate(2026, 2, 28, %d)" % (18 * 3600 + 45 * 60 + 30)
    assert "set day of dt to 1" in smp.DATE_HANDLER   # avoids month overflow


# --- chunking / idempotency ---------------------------------------------------

def test_chunking_splits_into_fifty(smp):
    scripts = smp.build_seed_scripts(fake_persona(n_events=120, n_reminders=0, n_notes=0), ("calendar",))
    assert [count for _, count, _ in scripts] == [50, 50, 20]
    assert smp.chunked(list(range(5)), 2) == [[0, 1], [2, 3], [4]]
    seen = []
    for _, _, script in scripts:
        seen += re.findall(r'does not contain "\[wisp-demo-id:(cal-\d+)\]"', script)
    assert sorted(seen) == sorted(f"cal-{i}" for i in range(120))


def test_every_item_is_guarded_by_its_marker(smp):
    scripts = smp.build_seed_scripts(fake_persona(), smp.KINDS)
    by_kind = {k: s for k, _, s in scripts}
    assert set(by_kind) == {"calendar", "reminders", "notes"}
    for kind, prefix, n in (("calendar", "cal", 3), ("reminders", "rem", 2), ("notes", "note", 2)):
        s = by_kind[kind]
        for i in range(n):
            m = f"[wisp-demo-id:{prefix}-{i}]"
            assert s.count(f'does not contain "{m}"') == 1
            assert s.count(m) == 2      # guard + the marker written into the item
        assert "existingText" in s and 'return "created "' in s
        assert s.isascii()


def test_calendar_script_targets_wisp_demo_only(smp):
    s = smp.calendar_script(smp.normalize_events(fake_persona().calendar_events))
    assert 'exists calendar "Wisp Demo"' in s
    assert 'make new calendar with properties {name:"Wisp Demo"}' in s
    assert "whose start date >= rangeStart and start date <= rangeEnd" in s
    assert "tell wispCal to make new event" in s
    assert "Fictional Wisp demo data" in s


def test_reminders_script_due_dates_and_completed(smp):
    rows = smp.normalize_reminders([
        {"demo_id": "a", "title": "Done one", "due_ts": BASE, "completed": True, "notes": ""},
        {"demo_id": "b", "title": "No due", "due_ts": None, "completed": False, "notes": ""},
    ])
    s = smp.reminders_script(rows)
    assert 'make new list with properties {name:"Wisp Demo"}' in s
    assert s.count("due date:dd") == 1
    assert s.count("set completed of newItem to true") == 1


def test_notes_html_is_escaped(smp):
    html = smp.note_html("A <b> & C", 'x "y"\n\nz', "n1")
    assert "&lt;b&gt; &amp; C" in html and "&quot;y&quot;" in html
    assert "<div><br></div>" in html and "[wisp-demo-id:n1]" in html
    s = smp.notes_script(smp.normalize_notes(fake_persona().notes))
    assert 'make new folder at wispAccount with properties {name:"Wisp Demo"}' in s
    assert "make new note at wispFolder" in s


def test_normalize_skips_bad_rows_and_fixes_end(smp):
    rows = smp.normalize_events([
        {"demo_id": "ok", "title": "T", "start_ts": BASE, "end_ts": BASE - 5},
        {"demo_id": "", "title": "T", "start_ts": BASE},
        {"demo_id": "x", "title": "T", "start_ts": "nan?"},
        SimpleNamespace(demo_id="obj", title="O", start_ts=BASE + 1, end_ts=None, location=None, notes=None),
    ])
    assert [r["demo_id"] for r in rows] == ["ok", "obj"]
    assert rows[0]["end_ts"] == BASE + 1800


# --- CLI -------------------------------------------------------------------

def test_dry_run_prints_scripts_without_running(smp, monkeypatch):
    monkeypatch.setattr(smp, "load_persona", lambda *a, **k: fake_persona())
    monkeypatch.setattr(smp.subprocess, "run", no_subprocess)
    monkeypatch.setattr(smp, "is_macos", lambda: False)
    buf = io.StringIO()
    args = smp.build_parser().parse_args(["seed", "--dry-run"])
    assert smp.cmd_seed(args, out=buf) == 0
    out = buf.getvalue()
    for app in ("Calendar", "Reminders", "Notes"):
        assert f'tell application "{app}"' in out
    assert "dry run, nothing written" in out


def test_seed_refuses_off_macos(smp, monkeypatch):
    monkeypatch.setattr(smp, "is_macos", lambda: False)
    monkeypatch.setattr(smp.subprocess, "run", no_subprocess)
    monkeypatch.setattr(smp, "load_persona", lambda *a, **k: pytest.fail("must not generate"))
    assert smp.cmd_seed(smp.build_parser().parse_args(["seed"]), out=io.StringIO()) == 2


def test_seed_runs_one_osascript_per_chunk_and_tallies(smp, monkeypatch):
    run = FakeRun(stdout="created 2 skipped 1")
    monkeypatch.setattr(smp, "is_macos", lambda: True)
    monkeypatch.setattr(smp.subprocess, "run", run)
    monkeypatch.setattr(smp, "load_persona", lambda *a, **k: fake_persona(n_events=60))
    buf = io.StringIO()
    assert smp.cmd_seed(smp.build_parser().parse_args(["seed", "--calendar"]), out=buf) == 0
    assert len(run.calls) == 2
    assert all(argv == ["osascript", "-"] for argv, _ in run.calls)
    assert "60 item(s): 4 created, 2 already present" in buf.getvalue()


def test_kind_flags_select_kinds(smp, monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(smp, "is_macos", lambda: True)
    monkeypatch.setattr(smp.subprocess, "run", run)
    monkeypatch.setattr(smp, "load_persona", lambda *a, **k: fake_persona())
    smp.cmd_seed(smp.build_parser().parse_args(["seed", "--notes"]), out=io.StringIO())
    assert len(run.calls) == 1 and 'tell application "Notes"' in run.calls[0][1]


def test_seed_reports_osascript_failure(smp, monkeypatch):
    monkeypatch.setattr(smp, "is_macos", lambda: True)
    monkeypatch.setattr(smp.subprocess, "run", FakeRun(returncode=1, stderr="execution error: Not authorized (-1743)"))
    monkeypatch.setattr(smp, "load_persona", lambda *a, **k: fake_persona())
    buf = io.StringIO()
    assert smp.cmd_seed(smp.build_parser().parse_args(["seed", "--calendar"]), out=buf) == 1
    assert "Automation" in buf.getvalue()


def test_persona_generator_contract_args(smp, monkeypatch):
    seen = {}

    def gen(now, tz, weeks_back, weeks_ahead, seed):
        seen.update(tz=tz, weeks_back=weeks_back, weeks_ahead=weeks_ahead, seed=seed)
        return fake_persona()
    fake_mod = SimpleNamespace(generate_persona=gen)
    monkeypatch.setitem(__import__("sys").modules, "service.behavior.persona", fake_mod)
    monkeypatch.setattr(smp, "is_macos", lambda: False)
    args = smp.build_parser().parse_args(["seed", "--dry-run", "--weeks-back", "3", "--weeks-ahead", "1",
                                          "--tz", "America/Los_Angeles"])
    assert smp.cmd_seed(args, out=io.StringIO()) == 0
    assert seen == {"tz": "America/Los_Angeles", "weeks_back": 3, "weeks_ahead": 1, "seed": 11}


# --- remove ------------------------------------------------------------------

def _deletes(script):
    return [line.strip() for line in script.splitlines() if line.strip().startswith("delete")]


def test_remove_script_only_deletes_wisp_demo_containers(smp):
    script = smp.remove_script(smp.KINDS)
    deletes = _deletes(script)
    assert deletes == [
        'delete (first calendar whose name is "Wisp Demo")',
        'delete (first list whose name is "Wisp Demo")',
        'delete folder "Wisp Demo" of a',
    ]
    assert "every event" not in script and "every reminder" not in script and "every note" not in script
    assert "delete" not in smp.count_script(smp.KINDS)


def test_remove_dry_run_runs_nothing(smp, monkeypatch):
    monkeypatch.setattr(smp.subprocess, "run", no_subprocess)
    buf = io.StringIO()
    assert smp.cmd_remove(smp.build_parser().parse_args(["remove", "--dry-run", "--reminders"]), out=buf) == 0
    assert 'delete (first list whose name is "Wisp Demo")' in buf.getvalue()
    assert "calendar" not in _deletes(buf.getvalue())[0]


def test_remove_requires_confirmation(smp, monkeypatch):
    run = FakeRun(stdout="calendar 1 12")
    monkeypatch.setattr(smp, "is_macos", lambda: True)
    monkeypatch.setattr(smp.subprocess, "run", run)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert smp.cmd_remove(smp.build_parser().parse_args(["remove"]), out=io.StringIO()) == 1
    assert len(run.calls) == 1 and not _deletes(run.calls[0][1])   # only the read-only count


def test_remove_with_yes_deletes_containers(smp, monkeypatch):
    run = FakeRun(stdout="calendar removed 1")
    monkeypatch.setattr(smp, "is_macos", lambda: True)
    monkeypatch.setattr(smp.subprocess, "run", run)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("no prompt with --yes"))
    assert smp.cmd_remove(smp.build_parser().parse_args(["remove", "--yes", "--calendar"]), out=io.StringIO()) == 0
    assert len(run.calls) == 2
    assert _deletes(run.calls[1][1]) == ['delete (first calendar whose name is "Wisp Demo")']


def test_remove_refuses_off_macos(smp, monkeypatch):
    monkeypatch.setattr(smp, "is_macos", lambda: False)
    monkeypatch.setattr(smp.subprocess, "run", no_subprocess)
    assert smp.cmd_remove(smp.build_parser().parse_args(["remove", "--yes"]), out=io.StringIO()) == 2


# --- the real generator, when present ------------------------------------------

def test_real_persona_generates_ascii_scripts(smp):
    persona_mod = pytest.importorskip("service.behavior.persona")
    persona = persona_mod.generate_persona(BASE, tz="America/Los_Angeles", weeks_back=2, weeks_ahead=1, seed=11)
    scripts = smp.build_seed_scripts(persona, smp.KINDS)
    assert scripts and all(script.isascii() for _, _, script in scripts)
    assert sum(count for kind, count, _ in scripts if kind == "calendar") > 0
