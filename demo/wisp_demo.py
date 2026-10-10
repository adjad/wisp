#!/usr/bin/env python3
"""Wisp Catch: synthetic Messages with local commitment extraction.

Nothing here writes to Calendar, Reminders, Mail, Messages or Notes. Synthetic
data lives only inside Wisp's own store, tagged so it is hidden unless the app is
launched in demo mode and removable in one command:

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
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
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
    drop.add_argument("--preset", choices=sorted(PRESETS), default="natural")
    drop.add_argument("--sender")
    drop.add_argument("--text")
    drop.add_argument("--minutes", type=int, default=120, help="how far ahead the stated time is (default 120)")
    drop.set_defaults(fn=cmd_drop)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
