#!/usr/bin/env python3
"""Wisp demo driver: Catch (synthetic Messages) and the behavior engine.

The Catch commands never write to Calendar, Reminders, Mail, Messages or Notes.
Their synthetic data lives only inside Wisp's own store, tagged so it is hidden
unless the app is launched in demo mode and removable in one command:

  * commitments   -> ~/.moe/assistant.db, source="wisp_seed" (visible only when the
                     backend runs with WISP_QA_SEED=1)
  * texts         -> ~/.moe/demo/messages.json (an overlay the attention detector
                     reads instead of live Messages; the Messages cache is untouched)

Commands (run with the repo's Python, e.g. .venv/bin/python demo/wisp_demo.py ...):

  preflight   what REAL items would show on screen in the next 48 hours
  setup       reset, then add the synthetic schedule
  launch      start Wisp.app in demo mode (quit Wisp first; --app PATH for a rebuilt app)
  drop        deliver a synthetic text on cue (default: a natural plan the rules miss)
  status      what is currently added
  clear       remove everything this tool added (your own data is never touched)

Behavior-engine demo (see docs/BEHAVIOR_DEMO.md). These DO touch the Mac's real
apps, but only inside containers named "Wisp Demo" (a calendar, a Reminders
list, a Notes folder) holding FICTIONAL data, removable with `persona remove`:

  persona seed|remove [...]   seed/remove the fictional persona (demo/seed_mac_persona.py)
  behavior ACTION             status | run | patterns | predict | profile | nudges | clear | reset
                              against the running backend (127.0.0.1:8765)
  behavior reset --wait       before the demo: remove rehearsal drops from "Wisp Demo",
                              wait for one Calendar sync, then reset learned state
  drop --preset gym-conflict  meeting on the next gym morning, then POST /behavior/run
  drop --preset backtoback    four back-to-back meetings tomorrow 14:00-16:00, then run
  drop --preset dev           no Mac change; run now and show Dev's predicted afternoon window
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as clock, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# The dry runs below should see the synthetic rows, exactly as the demo-mode backend does.
os.environ["WISP_QA_SEED"] = "1"

from service.assistant.store import QA_SEED_SOURCE, assistant_store  # noqa: E402
from service.assistant import attention_demo  # noqa: E402
from service.attention import detectors  # noqa: E402
from service.paths import MOE_DIR  # noqa: E402
from tests.fixtures import persona  # noqa: E402

APP = Path("/Applications/Wisp.app")


def _fmt_clock(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    hour = d.hour % 12 or 12
    return f"{hour}{'' if d.minute == 0 else f':{d.minute:02d}'}{'AM' if d.hour < 12 else 'PM'}"


def _next_half_hour(ts: float) -> float:
    d = datetime.fromtimestamp(ts).replace(second=0, microsecond=0)
    d += timedelta(minutes=30 - d.minute % 30)
    return d.timestamp()


def _real_rows(horizon_h: float = 48) -> list[dict]:
    now = time.time()
    with assistant_store._lock:
        rows = assistant_store._db.execute(
            "SELECT title, kind, source, when_ts FROM commitments WHERE status='active' AND source <> ? "
            "AND when_ts >= ? AND when_ts <= ? ORDER BY when_ts", (QA_SEED_SOURCE, now - 300, now + horizon_h * 3600)
        ).fetchall()
    return [dict(r) for r in rows]


def cmd_preflight(_: argparse.Namespace) -> None:
    rows = _real_rows()
    print(f"Wisp data dir: {MOE_DIR}")
    print(f"\nYour REAL commitments in the next 48 h ({len(rows)}). They will appear next to the demo ones:")
    for r in rows:
        print(f"  {datetime.fromtimestamp(r['when_ts']):%a %H:%M}  [{r['source']}]  {r['title']}")
    if not rows:
        print("  (none)")
    print("\nCatch reads synthetic Messages only. Other app features can still sync real sources and")
    print("show them in the brief/chat. WISP_HOME isolates storage; it does not disable native sync.")
    print("Use `replay` with an isolated WISP_HOME for a synthetic backend-only rehearsal.")


def cmd_setup(_: argparse.Namespace) -> None:
    cmd_clear(_, quiet=True)
    n = assistant_store.sync_source(QA_SEED_SOURCE, persona.commitment_rows(time.time()))
    print(f"added {n} synthetic commitments (source={QA_SEED_SOURCE!r}); they are hidden outside demo mode")
    print("next: quit Wisp, run `launch`, then use `drop` during the demo")


def cmd_clear(_: argparse.Namespace, quiet: bool = False) -> None:
    # Invalidate evidence before waiting for the write lock. A tick already in
    # its transaction is then deleted below; a later tick cannot resurrect it.
    overlay = attention_demo.clear_overlay()
    with assistant_store.transaction():
        ids = [r["id"] for r in assistant_store._db.execute(
            "SELECT id FROM commitments WHERE source=?", (QA_SEED_SOURCE,)).fetchall()]
        for cid in ids:
            assistant_store._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (cid,))
        assistant_store._db.execute("DELETE FROM commitments WHERE source=?", (QA_SEED_SOURCE,))
        dropped = 0
        for row in assistant_store._db.execute(
                "SELECT id, kind, payload, dedupe_key FROM assistant_events").fetchall():
            try:
                cid = json.loads(row["payload"]).get("commitment_id")
            except ValueError:
                cid = None
            if cid in ids or (row["dedupe_key"] or "").startswith(attention_demo.KEY_PREFIX + "demo:"):
                assistant_store._db.execute("DELETE FROM assistant_events WHERE id=?", (row["id"],))
                dropped += 1
    if not quiet:
        print(f"removed {len(ids)} synthetic commitments, {dropped} pending events, "
              f"overlay messages: {'yes' if overlay else 'none'}")


def cmd_status(_: argparse.Namespace) -> None:
    with assistant_store._lock:
        n = assistant_store._db.execute("SELECT COUNT(*) FROM commitments WHERE source=?",
                                        (QA_SEED_SOURCE,)).fetchone()[0]
    overlay = attention_demo.load_overlay()
    print(f"synthetic commitments: {n}")
    print(f"overlay messages:      {len(overlay)} ({attention_demo.overlay_path()})")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/mode", timeout=2) as r:
            print(f"backend on :8765:      reachable (HTTP {r.status})")
    except Exception as exc:  # noqa: BLE001
        print(f"backend on :8765:      not reachable ({type(exc).__name__}); launch Wisp in demo mode")
    print("demo flags in THIS shell: "
          f"WISP_ATTENTION_DEMO={os.environ.get('WISP_ATTENTION_DEMO', 'unset')} "
          "(`launch` sets seed, attention and synthetic-only flags for the app itself)")


PRESETS = {
    "natural": ("Mom", lambda t: f"I can make it to the Quad at {t}", "local model: firm plan missed by rules"),
    "quad": ("Mom", lambda t: f"Meet me in the Quad at {t}", "ALERT: stated, soon, not on file"),
    "dinner": ("Priya", lambda t: f"Dinner at {t}, I booked a table", "ALERT: stated, soon, not on file"),
    "known": ("Priya", lambda t: f"Dinner with Priya at {t}",
              "silent: already on your calendar (drop adds a matching synthetic event first)"),
    "promo": ("SHOP", lambda t: "50% OFF today only! Reply STOP to opt out", "silent: promotional"),
    "scam": ("Parcel", lambda t: f"Your package is on hold. Verify your address at https://parcel.example/v by {t}",
             "silent: scam shape"),
    "question": ("Mom", lambda t: f"Can you make it to the Quad at {t}?", "silent: question"),
    "hedge": ("Mom", lambda t: f"Maybe I can make it to the Quad at {t}", "silent: tentative"),
    "outgoing": ("Me", lambda t: f"I can make it to the Quad at {t}", "silent: outgoing"),
    "ambiguous": ("Mom", lambda t: "I can make it to the Quad at 6", "silent: ambiguous time"),
    "unsupported": ("Mom", lambda t: f"I can make it to the Quad on Friday at {t}", "silent: unsupported day"),
    "distant": ("Mom", lambda t: f"I can make it to the Quad at {t}", "silent: outside 24 hours"),
}


def cmd_drop(a: argparse.Namespace) -> None:
    if a.preset in BEHAVIOR_PRESETS:
        cmd_drop_behavior(a)
        return
    sender, make, _expect = PRESETS[a.preset]
    sender = a.sender or sender
    now = time.time()
    when = _next_half_hour(now + (1800 if a.preset == "distant" else a.minutes) * 60)
    offset = (datetime.fromtimestamp(when).date() - datetime.fromtimestamp(now).date()).days
    day = " tomorrow" if offset == 1 else f" on {datetime.fromtimestamp(when):%Y-%m-%d}" if offset > 1 else ""
    text = a.text or make(_fmt_clock(when) + day)
    if a.preset == "known" and not a.text:
        assistant_store.add_manual("Dinner with Priya", when, kind="event", source=QA_SEED_SOURCE)
    row = attention_demo.add_overlay_message(sender, text,
        direction="outgoing" if a.preset == "outgoing" else "incoming")
    item = [i for i in attention_demo.load_overlay() if i.id == attention_demo.OVERLAY_ID_PREFIX + row["id"]]
    hits = detectors.detect(item, now=time.time(), commitments=assistant_store.active_future(time.time(), 3))
    print(f'delivered: {sender}: "{text}"')
    print("expected: " + (f"rule alert within ~30 s ({_fmt_clock(hits[0].when_ts)} added to Wisp's schedule)"
                          if hits else PRESETS[a.preset][2] if not a.text
                          else "rules missed; local model may abstain"))


def cmd_launch(a: argparse.Namespace) -> None:
    app = Path(a.app).expanduser()
    if subprocess.run(["pgrep", "-x", "Wisp"], capture_output=True).returncode == 0:
        sys.exit("Wisp is already running. Quit it first (menu bar icon -> Quit), then run launch again.")
    if not (app / "Contents" / "Info.plist").exists():
        sys.exit(f"no Wisp.app at {app}; pass --app PATH (e.g. the one built under dist/)")
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    exe = app / "Contents" / "MacOS" / info["CFBundleExecutable"]
    env = {**os.environ, "WISP_QA_SEED": "1", "WISP_ATTENTION_DEMO": "1",
           "WISP_ATTENTION_SYNTHETIC_ONLY": "1"}
    print(f"launching {exe} in demo mode (leave this Terminal window open)")
    os.execve(str(exe), [str(exe)], env)


def cmd_replay(_: argparse.Namespace) -> None:
    """Exercise the real local extractor/store/hub without starting native readers."""
    if not os.environ.get("WISP_HOME"):
        sys.exit("replay requires an isolated WISP_HOME")
    from service.assistant.hub import Hub
    os.environ["WISP_ATTENTION_SYNTHETIC_ONLY"] = "1"

    async def replay():
        events = await attention_demo.tick(assistant_store, Hub(store=assistant_store))
        for event in events:
            print(json.dumps({k: event[k] for k in ("quote", "title", "context", "when_ts", "stage")}))
        print(f"published {len(events)} alert(s); live Messages reader bypassed")
    asyncio.run(replay())


# ---------------------------------------------------------------------------
# Behavior engine demo (docs/BEHAVIOR_DEMO.md)
# ---------------------------------------------------------------------------

BASE_URL = "http://127.0.0.1:8765"
BEHAVIOR_ACTIONS = ("status", "run", "patterns", "predict", "profile", "nudges", "clear", "reset")
DROP_MARKER = "[wisp-demo-id:drop-"   # seed_mac_persona.marker() of every drop preset event
SYNC_WAIT_S = 75.0                    # the app re-reads Calendar about once a minute
SYNC_POLL_S = 5.0
DEFAULT_GYM_WEEKDAYS = (0, 2, 4)   # persona "Morning lift" Mon/Wed/Fri; used until a habit is learned
BEHAVIOR_PRESETS = {
    "gym-conflict": "habit_conflict nudge: auto reminder (Undo available)",
    "backtoback": "cancel_likely nudge: 'Likely skip' tentative hold + 'Draft reschedule note'",
    "dev": "contact_window nudge when it is Dev's usual afternoon window (no Mac data is added)",
}


class BackendUnavailable(RuntimeError):
    pass


def _http(method: str, path: str, body: dict | None = None, timeout: float = 30.0) -> tuple[int, object]:
    """One JSON request to the running backend -> (status, decoded body)."""
    data = None
    headers = {}
    if body is not None or method != "GET":
        data = json.dumps(body or {}).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return exc.code, payload
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BackendUnavailable(f"Wisp backend not reachable at {BASE_URL} ({type(exc).__name__}); "
                                 "launch Wisp (see docs/BEHAVIOR_DEMO.md) and retry") from exc


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _rows(obj: object, key: str) -> list:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and isinstance(obj.get(key), list):
        return obj[key]
    return []


def _clock(ts: object) -> str:
    try:
        return f"{datetime.fromtimestamp(float(ts)):%a %H:%M}"
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _num(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _print_patterns(obj: object) -> None:
    rows = _rows(obj, "patterns")
    if not rows:
        print("no patterns learned yet (run `behavior run` after the persona is synced)")
    for p in rows:
        print(f"{_num(p.get('confidence')):.2f}  {p.get('status', ''):9}  {p.get('kind', ''):17}  "
              f"{p.get('statement', '')}  [id={p.get('id', '')}, "
              f"{p.get('support', 0)}/{p.get('opportunities', 0)}]")


def _print_predictions(obj: object) -> None:
    rows = _rows(obj, "predictions")
    if not rows:
        print("no predictions for the next day")
    for p in rows:
        print(f"{_clock(p.get('ts'))}  {p.get('kind', ''):14}  {p.get('subject', '')}  "
              f"p={_num(p.get('probability')):.2f}  {p.get('because', '')}")


def _print_nudges(obj: object) -> None:
    rows = _rows(obj, "nudges")
    if not rows:
        print("no nudges")
    for n in rows:
        print(f"[{n.get('kind', '')}] {n.get('title', '')}  (id={n.get('id') or n.get('nudge_id', '')})")
        if n.get("body"):
            print(f"    {n['body']}")
        if n.get("because"):
            print(f"    because: {n['because']}")
        for i, act in enumerate(n.get("actions") or []):
            flag = "AUTO, already done" if act.get("auto") is True else "offered"
            print(f"    action {i}: {act.get('type', '')} - {act.get('label', '')} ({flag})")


def _print_profile(obj: object) -> None:
    if isinstance(obj, dict):
        text = obj.get("profile") or obj.get("card") or obj.get("text")
        print(text if isinstance(text, str) and text else json.dumps(obj, indent=2, sort_keys=True))
    else:
        print(obj)


def _print_run(obj: object) -> None:
    if isinstance(obj, dict) and obj.get("busy"):
        print(f"behavior run: busy ({obj.get('detail') or 'another run is in progress'})")
        return
    if isinstance(obj, dict) and ((obj.get("status") or {}).get("waiting_for_calendar")):
        print("waiting for Wisp's first Calendar read (up to ~3 min after launch); nudges start after it")
    if isinstance(obj, dict) and "published" in obj:
        ids = [i for i in obj.get("nudge_ids") or [] if i]
        print(f"published {obj['published']} nudge(s)" + (f": {', '.join(ids)}" if ids else ""))
        if not ids:
            print("(no new nudge: already delivered, outside its window, or muted by feedback)")
    else:
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _show(obj: object, as_json: bool, printer=None) -> None:
    if as_json or printer is None:
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))
    else:
        printer(obj)


PRINTERS = {"patterns": _print_patterns, "predict": _print_predictions, "nudges": _print_nudges,
            "profile": _print_profile}


def cmd_behavior(a: argparse.Namespace) -> None:
    try:
        _behavior(a)
    except BackendUnavailable as exc:
        sys.exit(str(exc))


def _behavior(a: argparse.Namespace) -> None:
    action = a.action
    if action == "patterns" and (a.confirm or a.reject or a.reactivate):
        pid, status = ((a.confirm, "confirmed") if a.confirm else
                       (a.reject, "rejected") if a.reject else (a.reactivate, "active"))
        code, obj = _http("POST", f"/behavior/patterns/{_quote(pid)}", {"status": status})
        print(f"pattern {pid} -> {status}: HTTP {code}")
        _show(obj, True)
        return
    if action == "nudges" and (a.feedback or a.undo or a.act):
        if a.feedback:
            nid, verdict = a.feedback
            if verdict not in ("useful", "not_now", "wrong"):
                sys.exit("verdict must be useful, not_now or wrong")
            code, obj = _http("POST", f"/behavior/nudges/{_quote(nid)}/feedback", {"verdict": verdict})
        elif a.undo:
            code, obj = _http("POST", f"/behavior/nudges/{_quote(a.undo)}/undo", {})
        else:
            nid, index = a.act
            code, obj = _http("POST", f"/behavior/nudges/{_quote(nid)}/act", {"action_index": int(index)})
        print(f"HTTP {code}")
        _show(obj, True)
        return
    if action == "run":
        code, obj = _http("POST", "/behavior/run", {}, timeout=120)
        print(f"behavior run: HTTP {code}")
        _show(obj, a.json, _print_run)
        return
    if action in ("clear", "reset"):
        if not _remove_rehearsal_drops(a):
            return
        code, obj = _http("POST", "/behavior/clear", {})
        if code in (404, 405):
            print("this backend has no /behavior/clear endpoint. To reset learned behavior state, quit "
                  f"Wisp and delete {MOE_DIR / 'behavior.db'} (local brain). `persona remove` removes "
                  "the seeded Mac data.")
            return
        print(f"behavior clear: HTTP {code}")
        _show(obj, True)
        return
    path = "/behavior/nudges" if action == "nudges" else f"/behavior/{action}"
    code, obj = _http("GET", path)
    if code == 404 and action == "nudges":
        print("this backend does not list nudges (GET /behavior/nudges); they arrive as app "
              "notifications. Use --feedback/--undo/--act with a nudge id.")
        return
    if code >= 400:
        print(f"GET {path}: HTTP {code}")
        _show(obj, True)
        return
    _show(obj, a.json, PRINTERS.get(action))


def drop_cleanup_script(container: str = "Wisp Demo") -> str:
    """AppleScript deleting ONLY the events `drop` added to the ``container``
    calendar (description carries a ``[wisp-demo-id:drop-…]`` marker). Seeded
    persona events and every other calendar are left alone. Uids are collected
    first, so deleting never shifts the list being walked. Uids and descriptions
    are each fetched in ONE bulk Apple event and walked as plain lists; looping
    ``repeat with ev in (every event … whose …)`` would re-run the filtered query
    for every property read (quadratic, timed out on ~200 events)."""
    seed = _seed_module()
    name = seed.as_string(container)
    lo, hi = time.time() - 30 * 86400, time.time() + 30 * 86400
    return (seed.DATE_HANDLER + "\n"
            "set removedCount to 0\n"
            'tell application "Calendar"\n'
            "\tif exists calendar " + name + " then\n"
            "\t\tset wispCal to first calendar whose name is " + name + "\n"
            "\t\tset rangeStart to " + seed.as_date(lo) + "\n"
            "\t\tset rangeEnd to " + seed.as_date(hi) + "\n"
            "\t\tset doomed to {}\n"
            "\t\tset uidList to uid of (every event of wispCal whose start date >= rangeStart and start date <= rangeEnd)\n"
            "\t\tset descList to description of (every event of wispCal whose start date >= rangeStart and start date <= rangeEnd)\n"
            "\t\trepeat with i from 1 to (count of descList)\n"
            "\t\t\ttry\n"
            "\t\t\t\tset d to item i of descList\n"
            "\t\t\t\tif d is not missing value and d contains " + seed.as_string(DROP_MARKER) + " then\n"
            "\t\t\t\t\tset end of doomed to (item i of uidList)\n"
            "\t\t\t\tend if\n"
            "\t\t\tend try\n"
            "\t\tend repeat\n"
            "\t\trepeat with u in doomed\n"
            "\t\t\ttry\n"
            "\t\t\t\tdelete (first event of wispCal whose uid is (contents of u))\n"
            "\t\t\t\tset removedCount to removedCount + 1\n"
            "\t\t\tend try\n"
            "\t\tend repeat\n"
            "\tend if\n"
            "end tell\n"
            'return "removed " & removedCount\n')


def _calendar_last_sync() -> float | None:
    try:
        code, obj = _http("GET", "/assistant/status", timeout=5)
    except BackendUnavailable:
        return None
    cal = ((obj.get("connectors") or {}).get("calendar") or {}) if isinstance(obj, dict) and code == 200 else {}
    value = cal.get("last_sync")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _wait_for_calendar_sync(since: float, sleep=None, clock_fn=None) -> None:
    """Wait until the app re-reads Calendar after ``since`` (polls the backend's
    sync status), or SYNC_WAIT_S when that status is unavailable."""
    sleep, clock_fn = sleep or time.sleep, clock_fn or time.time
    deadline = clock_fn() + 2 * SYNC_WAIT_S
    print(f"waiting for Wisp's next Calendar sync (about {int(SYNC_WAIT_S)} s)...", flush=True)
    if _calendar_last_sync() is None:
        sleep(SYNC_WAIT_S)
        return
    while clock_fn() < deadline:
        last = _calendar_last_sync()
        if last is not None and last > since:
            print("Calendar synced.")
            return
        sleep(SYNC_POLL_S)
    print("no Calendar sync seen yet; resetting anyway (rerun `behavior reset` if a rehearsal nudge reappears)")


def _remove_rehearsal_drops(a: argparse.Namespace) -> bool:
    """Step 1 of `behavior clear|reset`: delete rehearsal drop events from the
    "Wisp Demo" calendar, otherwise they refire right after the reset and the
    real drop finds them "already there". True when the reset should proceed."""
    seed = _seed_module()
    script = drop_cleanup_script(seed.CONTAINER)
    if getattr(a, "dry_run", False):
        print(f"-- Calendar (remove rehearsal drops):\n{script}")
        print("-- then POST /behavior/clear" + (" after one Calendar sync" if getattr(a, "wait", False) else ""))
        return False
    if not seed.is_macos():
        print("skipping the 'Wisp Demo' calendar cleanup: it needs osascript (macOS only); "
              "only Wisp's learned state is reset")
        return True
    started = time.time()
    try:
        out = seed.run_osascript(script, timeout=300)
    except seed.OsaError as exc:
        sys.exit(f"Calendar cleanup failed: {exc}")
    removed = int(out.rsplit(" ", 1)[-1]) if out.startswith("removed ") and out.rsplit(" ", 1)[-1].isdigit() else 0
    print(f"Calendar: removed {removed} rehearsal drop event(s) from 'Wisp Demo'")
    if removed == 0:
        return True
    if getattr(a, "wait", False):
        _wait_for_calendar_sync(started)
        return True
    print(f"Wisp still has them until its next Calendar sync (~{int(SYNC_WAIT_S)} s). Rerun "
          "`behavior reset` in a minute (or use `behavior reset --wait`); resetting now would refire them.")
    return False


def _seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mac_persona", Path(__file__).resolve().with_name("seed_mac_persona.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_persona(a: argparse.Namespace) -> None:
    code = _seed_module().main([a.action, *a.rest])
    if code:
        sys.exit(code)


def _gym_weekdays() -> tuple[int, ...]:
    """Weekdays of the learned gym habit; falls back to weekday mornings."""
    try:
        code, obj = _http("GET", "/behavior/patterns", timeout=10)
    except BackendUnavailable:
        return DEFAULT_GYM_WEEKDAYS
    if code != 200:
        return DEFAULT_GYM_WEEKDAYS
    for p in _rows(obj, "patterns"):
        text = f"{p.get('subject', '')} {p.get('statement', '')}".lower()
        if p.get("kind") == "habit" and p.get("status") != "rejected" and any(
                word in text for word in ("fitness", "gym", "work out", "workout")):
            days = (p.get("window") or {}).get("weekdays") or []
            days = sorted({int(d) for d in days if isinstance(d, int) and 0 <= d <= 6})
            if days:
                return tuple(days)
    return DEFAULT_GYM_WEEKDAYS


def _at(day, hour: int, minute: int = 0) -> float:
    return datetime.combine(day, clock(hour, minute)).timestamp()


def behavior_drop_items(preset: str, now: float, gym_weekdays=DEFAULT_GYM_WEEKDAYS) -> dict:
    """The fictional calendar events / reminders a behavior preset adds to the
    "Wisp Demo" containers. Ids are date-stamped so re-dropping is idempotent."""
    today = datetime.fromtimestamp(now).date()
    tomorrow = today + timedelta(days=1)
    if preset == "gym-conflict":
        # Tomorrow if it is a gym day, else the next gym day.
        day = next((today + timedelta(days=k) for k in range(1, 8)
                    if (today + timedelta(days=k)).weekday() in gym_weekdays), tomorrow)
        return {"day": day, "reminders": [], "events": [{
            "demo_id": f"drop-gym-conflict-{day:%Y%m%d}", "title": "Foundry export check-in",
            "start_ts": _at(day, 7), "end_ts": _at(day, 7, 45), "location": "Zoom",
            "notes": "Dropped by wisp_demo.py to collide with the gym habit."}]}
    if preset == "backtoback":
        titles = ("Pipeline review", "Security packet sync", "Hiring debrief", "Board deck prep")
        events = []
        for i, title in enumerate(titles):
            start = _at(tomorrow, 14) + i * 1800
            events.append({"demo_id": f"drop-backtoback-{tomorrow:%Y%m%d}-{i + 1}", "title": title,
                           "start_ts": start, "end_ts": start + 1800, "location": "",
                           "notes": "Dropped by wisp_demo.py: four back-to-back meetings."})
        return {"day": tomorrow, "reminders": [], "events": events}
    if preset == "dev":
        # Dev's afternoon rhythm comes from the persona's (synthetic) message
        # history, which only lives inside Wisp; nothing is added to the Mac.
        return {"day": today, "events": [], "reminders": []}
    raise ValueError(f"unknown behavior preset {preset!r}")


def cmd_drop_behavior(a: argparse.Namespace) -> None:
    seed = _seed_module()
    dry = bool(getattr(a, "dry_run", False))
    writes = a.preset in ("gym-conflict", "backtoback")
    if writes and not dry and not seed.is_macos():
        sys.exit("this preset writes to the 'Wisp Demo' calendar via osascript (macOS only); "
                 "use --dry-run to print the AppleScript")
    weekdays = _gym_weekdays() if a.preset == "gym-conflict" and not dry else DEFAULT_GYM_WEEKDAYS
    items = behavior_drop_items(a.preset, time.time(), weekdays)
    scripts = []
    already = 0
    if items["events"]:
        scripts.append(("Calendar", seed.calendar_script(seed.normalize_events(items["events"]))))
    if items["reminders"]:
        scripts.append(("Reminders", seed.reminders_script(seed.normalize_reminders(items["reminders"]))))
    for app, script in scripts:
        if dry:
            print(f"-- {app} ({a.preset}):\n{script}")
            continue
        try:
            created, skipped = seed.parse_tally(seed.run_osascript(script))
        except seed.OsaError as exc:
            sys.exit(f"{app} write failed: {exc}")
        already += skipped
        print(f"{app}: {created} added to 'Wisp Demo', {skipped} already there")
    for e in items["events"]:
        print(f"  event     {_clock(e['start_ts'])}-{datetime.fromtimestamp(e['end_ts']):%H:%M}  {e['title']}")
    for r in items["reminders"]:
        print(f"  reminder  due {_clock(r['due_ts'])}  {r['title']}")
    if dry or getattr(a, "no_run", False):
        return
    try:
        code, obj = _http("POST", "/behavior/run", {"reason": "demo_drop", "preset": a.preset}, timeout=120)
    except BackendUnavailable as exc:
        sys.exit(str(exc))
    print(f"behavior run: HTTP {code}; expected: {BEHAVIOR_PRESETS[a.preset]}")
    _print_run(obj)
    if already and isinstance(obj, dict) and not obj.get("nudge_ids"):
        # Re-dropped: the event was already there, so show the nudge it made.
        _print_existing_nudges(items["events"])
        return
    if isinstance(obj, dict) and not obj.get("nudge_ids") and items["events"]:
        print("Wisp ingests Calendar after the app's next Calendar sync (every few minutes). "
              "Watch for the notification, or rerun `behavior run` in a minute.")
    if a.preset == "dev":
        try:
            code, preds = _http("GET", "/behavior/predict", timeout=30)
        except BackendUnavailable:
            return
        dev = [p for p in _rows(preds, "predictions") if "dev" in str(p.get("subject", "")).lower()]
        if dev:
            _print_predictions({"predictions": dev})
        else:
            print("no Dev prediction for the next day; the contact window nudge fires on weekday afternoons")


def _print_existing_nudges(events: list) -> None:
    try:
        code, obj = _http("GET", "/behavior/nudges", timeout=30)
    except BackendUnavailable:
        return
    titles = [str(e["title"]).casefold() for e in events]
    rows = [n for n in _rows(obj, "nudges") if code == 200 and any(
        t in f"{n.get('title', '')} {n.get('body', '')}".casefold() for t in titles)]
    if not rows:
        print("the drop was already on the calendar and no nudge for it is on file; "
              "run `behavior reset --wait` to rehearse it again")
        return
    print("the drop was already on the calendar; its nudge:")
    for n in rows:
        state = n.get("verdict") or ("auto action active (Undo available)" if n.get("can_undo") else "delivered")
        print(f"  delivered {_clock(n.get('delivered_at'))}  state: {state}")
    _print_nudges({"nudges": rows})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("preflight", cmd_preflight), ("setup", cmd_setup),
                     ("status", cmd_status), ("clear", cmd_clear), ("replay", cmd_replay)):
        sub.add_parser(name).set_defaults(fn=fn)
    launch = sub.add_parser("launch")
    launch.add_argument("--app", default=str(APP), help="Wisp.app to run (default /Applications/Wisp.app)")
    launch.set_defaults(fn=cmd_launch)
    drop = sub.add_parser("drop")
    drop.add_argument("--preset", choices=sorted(PRESETS) + sorted(BEHAVIOR_PRESETS), default="natural")
    drop.add_argument("--sender")
    drop.add_argument("--text")
    drop.add_argument("--minutes", type=int, default=120, help="how far ahead the stated time is (default 120)")
    drop.add_argument("--dry-run", action="store_true",
                      help="behavior presets: print the AppleScript, write nothing")
    drop.add_argument("--no-run", action="store_true",
                      help="behavior presets: add the items but skip POST /behavior/run")
    drop.set_defaults(fn=cmd_drop)
    behavior = sub.add_parser("behavior", help="inspect or drive the behavior engine on the running backend")
    behavior.add_argument("action", choices=BEHAVIOR_ACTIONS)
    behavior.add_argument("--json", action="store_true", help="print raw JSON")
    behavior.add_argument("--confirm", metavar="PATTERN_ID", help="patterns: mark a pattern confirmed")
    behavior.add_argument("--reject", metavar="PATTERN_ID", help="patterns: reject a pattern")
    behavior.add_argument("--reactivate", metavar="PATTERN_ID", help="patterns: set a pattern back to active")
    behavior.add_argument("--feedback", nargs=2, metavar=("NUDGE_ID", "VERDICT"),
                          help="nudges: useful | not_now | wrong")
    behavior.add_argument("--undo", metavar="NUDGE_ID", help="nudges: undo a nudge's auto action")
    behavior.add_argument("--act", nargs=2, metavar=("NUDGE_ID", "INDEX"),
                          help="nudges: get the prompt for an offered action")
    behavior.add_argument("--wait", action="store_true",
                          help="clear/reset: after removing rehearsal drops, wait for one Calendar sync")
    behavior.add_argument("--dry-run", action="store_true",
                          help="clear/reset: print the Calendar cleanup AppleScript, change nothing")
    behavior.set_defaults(fn=cmd_behavior)
    persona_cmd = sub.add_parser("persona", help="seed/remove the fictional persona in the Mac's real apps")
    persona_cmd.add_argument("action", choices=("seed", "remove"))
    persona_cmd.add_argument("rest", nargs=argparse.REMAINDER,
                             help="passed to demo/seed_mac_persona.py (e.g. --all --dry-run, --yes)")
    persona_cmd.set_defaults(fn=cmd_persona)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
