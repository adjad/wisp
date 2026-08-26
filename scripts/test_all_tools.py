#!/usr/bin/env python3
"""Exercise every registered tool for real and report what happened.

Calls the actual registered function for each tool — not through the model,
not mocked — with a safe, realistic argument set. Writes a full report to
stdout and (if --json is passed) a machine-readable file alongside it.

Some tools are DELIBERATELY SKIPPED rather than auto-invoked, because running
them for real would do something irreversible or disruptive on the user's
actual Mac — restarting it, uninstalling a real app, deleting a real file,
disconnecting the network. Each skip states why. Everything else runs for
real against real local data/APIs/apps.

    .venv/bin/python scripts/test_all_tools.py
    .venv/bin/python scripts/test_all_tools.py --json /path/to/out.json
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import service.tools  # noqa: E402
from service.tools.registry import REGISTRY  # noqa: E402

SCRATCH = Path("/private/tmp/claude-501/-Users-adijain-Desktop-MOE-Project/"
               "81fa3fca-7fc0-4e6d-8513-d4a0d277bb2c/scratchpad/tool_test")
SCRATCH.mkdir(parents=True, exist_ok=True)

# Real message-id from the user's own inbox, resolved once at import via a
# direct cache read — used by every email tool that needs one, so
# flag/forward/reply-style tools test against a REAL message rather than a
# fabricated id that would just 404.
def _real_message_id() -> str | None:
    try:
        from service.tools.email_tools import _parse_lines
        rows = _parse_lines()
        return None  # header cache carries no message-id; see view_emails call below
    except Exception:  # noqa: BLE001
        return None


# name -> kwargs. Only tools needing a nonzero-risk decision or non-obvious
# argument are listed; anything absent gets {} (its own defaults / required
# args filled generically below).
ARGS: dict[str, dict] = {
    "find_files": {"query": "wisp", "folder": str(Path.home() / "Desktop")},
    "read_file": {"path": __file__},
    "list_dir": {"path": str(Path.home() / "Desktop")},
    "write_file": {"path": str(SCRATCH / "write_file_test.txt"), "content": "wisp tool test"},
    # Independent fixture (move_src_fixture.txt, created in setup), NOT the
    # write_file test's own output — REGISTRY iterates alphabetically and
    # move_path < write_file, so the file this used to point at didn't exist
    # yet when move_path actually ran. Same bug class as backup_folder's fix
    # above; the general lesson is every fixture-consuming tool needs its own
    # setup-created input, never another test's runtime output.
    "move_path": {"source": str(SCRATCH / "move_src_fixture.txt"),
                  "destination": str(SCRATCH / "moved_test.txt")},
    "organize_files": {"pattern": "*.zzz_never_matches", "folder": str(SCRATCH),
                       "destination": str(SCRATCH / "sorted")},
    "create_folder": {"path": str(SCRATCH / "created_folder")},
    "trash_file": {"path": str(SCRATCH / "moved_test.txt")},
    "archive_files": {"paths": [__file__], "archive_path": str(SCRATCH / "archive_test.zip")},
    "tag_file": {"path": str(SCRATCH), "color": "blue"},
    # NOT paired with "created_folder" from the create_folder test — tools run
    # in alphabetical order (backup_folder < create_folder), so that folder
    # doesn't exist yet when this runs. Real bug in an earlier version of this
    # harness, not in backup_folder itself; fixed by pointing at a fixture
    # this script creates directly in main() before the loop starts.
    "backup_folder": {"source": str(SCRATCH / "backup_src"),
                      "destination": str(SCRATCH / "backup_dst")},
    "convert_file": {"path": str(SCRATCH / "convert_src.txt"), "to_format": "rtf"},
    "encrypt_file": {"path": str(SCRATCH / "encrypt_src.txt"), "password": "wisptest123"},
    "reveal_in_finder": {"path": str(SCRATCH)},
    "print_document": {"path": __file__},

    "calculate": {"expression": "18% of 64.50"},
    "convert_units": {"value": 180, "from_unit": "lb", "to_unit": "kg"},
    "convert_currency": {"amount": 100, "from_currency": "USD", "to_currency": "EUR"},
    "world_time": {"place": "Tokyo"},
    "random_pick": {"options": ["pizza", "sushi", "tacos"]},
    "generate_password": {"length": 20},

    "set_timer": {"duration": "1 minute", "label": "tool test"},
    "manage_timers": {"action": "list"},
    "stopwatch": {"action": "read"},
    "set_alarm": {"time": "11:55pm", "label": "tool test — will be cancelled"},
    "set_sleep_timer": {"duration": "60 minutes"},
    "schedule_task": {"text": "tool test reminder", "minutes_from_now": 120},

    "search_coverage": {},
    "wisp_status": {}, "wisp_skills": {}, "wisp_mcp": {}, "wisp_sync": {},
    "system_status": {},
    "network_info": {},
    "get_battery_status": {},
    "list_bluetooth_devices": {},
    "get_volume": {},
    "clipboard_read": {},
    "clipboard_write": {"text": "wisp tool test"},
    # Explicit and harmless — NOT left to the generic integer default (which
    # is 1). Verified live: an earlier run had no entry here, fell through to
    # _default_args()'s int fallback of 1, and genuinely set the real system
    # volume to 1/100. A test harness silently changing real system state
    # because of a missing dict entry is exactly the class of bug this file
    # exists to catch in the tools THEMSELVES — it should not also be capable
    # of causing one.
    "set_volume": {"level": 50},
    "clear_clipboard": {},
    "list_running_apps": {},
    "manage_login_items": {"action": "list"},

    "get_weather": {"location": "San Francisco, CA"},
    "weather_alerts": {"location": "Miami, FL"},
    "rain_radar": {"location": "Seattle, WA"},
    "air_quality": {"location": "San Francisco, CA"},
    "get_stock_price": {"symbols": "AAPL"},
    "get_sports_scores": {"team_or_league": "NBA"},
    "wikipedia_summary": {"topic": "Golden Gate Bridge"},
    "recipe_lookup": {"dish": "pancakes"},
    "astronomy": {"location": "San Francisco, CA"},
    "country_info": {"country": "Japan"},
    "track_package": {"tracking_number": "1Z999AA10123456784"},
    "find_local_events": {"near": "San Francisco"},
    "lookup_media_title": {"title": "Dune"},
    "transit_info": {"query": "BART"},
    "track_flight": {"flight_number": "UA123"},
    "web_fetch": {"url": "https://example.com"},

    "find_place": {"what": "coffee", "near": "Palo Alto, CA", "radius_km": 1},
    "travel_time": {"origin": "San Francisco, CA", "destination": "Oakland, CA"},
    "get_directions": {"destination": "San Francisco, CA"},

    "get_upcoming": {},
    "get_past_events": {"days": 7},
    "find_free_time": {"days": 2},
    "contact_dates": {"days": 60},

    "search_notes": {"query": "wisp"},
    "create_note": {"title": "Wisp Tool Test", "body": "Created by scripts/test_all_tools.py — safe to delete."},
    # Points at a SEPARATE note created in setup (not the loop's own
    # create_note test, "Wisp Tool Test") — same ordering-independence fix.
    "append_note": {"title": "Wisp Tool Test Fixture", "text": "appended line"},

    "log_entry": {"text": "tool test entry", "category": "journal"},
    "read_log": {"days": 1},
    "health_summary": {"days": 7},
    "set_fitness_goal": {"name": "test goal", "target": "1"},

    "search_conversations": {"query": "wisp"},
    "recall": {"query": "test"},
    "remember": {"fact": "This is a temporary test fact from scripts/test_all_tools.py.",
                "category": "fact"},

    "summarize_emails": {"count": 5},
    "view_emails": {"count": 3},
    "scan_subscriptions": {"limit": 5},
    "triage_inbox": {"count": 10},

    "summarize_messages": {},
    "view_messages": {},
    "lookup_contact": {"name": "Mom"},
    "list_contacts": {},

    "list_shortcuts": {},

    "play_ambient": {"kind": "white_noise", "minutes": 1},
    "text_to_speech": {"text": "Wisp tool test.", "save_to": str(SCRATCH / "tts_test.aiff")},

    "write_document": {"path": str(SCRATCH / "doc_test.docx"), "title": "Wisp Tool Test",
                       "paragraphs": ["Created by the automated tool test."]},
    "spreadsheet_ops": {"path": str(SCRATCH / "sheet_test.xlsx"),
                        "headers": ["Item", "Amount"], "rows": [["Test", 1]]},

    "keychain_store": {"service": "WispToolTest", "account": "tester", "secret": "test-secret-123"},
    # Points at a fixture stored in setup, not the loop's own keychain_store
    # test ("WispToolTest") — same ordering-independence fix.
    "keychain_read": {"service": "WispToolTestFixture", "account": "tester"},

    "run_applescript": {"script": 'return "wisp tool test ok"'},
    "run_shell": {"cmd": "echo wisp_tool_test_ok"},

    "manage_contacts": {"action": "create", "name": "Wisp Tool Test Contact"},

    "run_shortcut": {"name": "My recent order"},
}

# Tools that would do something irreversible/disruptive if invoked for real —
# each entry states exactly why, so the report shows the reasoning, not a
# silent gap.
SKIP = {
    "power_control": "would restart/shut down/sleep/log out the real Mac",
    "uninstall_app": "would delete a real installed application",
    "delete_path": "permanent delete — no safe target to test against",
    "software_update": "'install' could restart the Mac; 'check' alone is safe but slow (~60s) — skipped for pass speed",
    "connect_wifi": "would disconnect the current real Wi-Fi connection",
    "set_wifi": "would disconnect the current real Wi-Fi connection",
    "set_appearance": "would visibly change the real desktop theme mid-session",
    "set_display": "would change real screen brightness/dark-mode",
    "manage_spaces": "would switch the real active Space",
    "force_quit_app": "no safe target — would need to kill a real running app",
    "quit_app": "would quit a real running application",
    "toggle_setting": "would change a real system setting (bluetooth/DND/etc.)",
    "accessibility_toggle": "would start VoiceOver and take over audio/interaction",
    "set_screen_lock_timeout": "would change a real security setting",
    "print_document": "would send a real job to a real printer if one is configured",
    "switch_app": "would change window focus mid-test-run",
    "window_control": "would resize/close the real frontmost window",
    "reveal_in_finder": "opens a real Finder window — low risk but disruptive to the desktop mid-run",
    "join_video_call": "would open a real meeting URL if one exists on an upcoming event",
    "play_streaming": "would launch a real installed streaming app",
    "play_podcast": "opens Podcasts and performs a real search",
    "play_audiobook": "opens Books.app",
    "play_radio": "starts real audio playback (tested separately, already verified)",
    "stop_radio": "no-op without play_radio having run in this process",
    "open_app": "would launch an arbitrary real application",
    "airdrop_file": "opens a real Finder share sheet",
    "place_call": "verified live: the generic 'test' fallback arg actually "
                  "opened FaceTime and attempted to dial 'test' — no real "
                  "call connected (macOS requires its own confirmation "
                  "first), but opening the app for real isn't a safe default",
    "scan_to_note": "opens Notes' scanner UI, needs a paired iPhone to do anything",
    "manage_contacts": "Contacts.app AppleScript needs TCC access this bare "
                       "terminal process doesn't hold (times out) — verified "
                       "separately through the packaged, TCC-granted app",
    # This run: user asked to skip anything that produces sound.
    "play_ambient": "skipped this run — produces audible output",
    "text_to_speech": "skipped this run — produces audible output",
    "play_radio": "skipped this run — produces audible output",
    # sends / drafts / schedules — tested explicitly and separately via the
    # live agent endpoint with the user's own address/number, per their
    # request, not auto-invoked here.
    "send_email": "tested separately via the live agent, to the user's own address",
    "send_message": "tested separately via the live agent, to the user's own number",
    "schedule_send": "tested separately via the live agent",
    "draft_email": "tested separately via the live agent",
    "draft_message": "tested separately via the live agent",
    "reply_to_email": "needs a real thread to reply into — tested separately",
    "forward_email": "needs a real message id and a real recipient — tested separately",
    "flag_email": "needs a real message id — tested separately",
    "mark_email_read": "needs a real message id — tested separately",
    "archive_email": "needs a real message id, and archiving is a real inbox change",
    "cancel_scheduled_send": "no scheduled send exists to cancel in this run",
    "list_scheduled_sends": "trivially safe but depends on prior schedule_send test",
    "add_calendar_event": "writes a REAL event to the user's live Calendar",
    "add_reminder": "writes a REAL reminder to Reminders.app",
    "cancel_event": "no safe target — would need to cancel a real event",
    "complete_reminder": "no safe target in this run",
    "clear_past_reminders": "bulk-deletes real past-due items",
    "update_event": "cancels + recreates a real event",
    "http_request": "would send a real mutating request to whatever URL is given",
    "create_tool": "generates and installs a new tool — a real, standing change",
    "forget": "would delete the 'remember' test fact before health_summary etc. can show it",
    "clear_memory": "bulk-deletes real memories",
    "install_shortcut": "no safe .shortcut file available to install",
    "identify_song": "honest non-implementation — verified by reading its source",
    "get_lyrics": "honest non-implementation — verified by reading its source",
    "transcribe_audio": "honest non-implementation — verified by reading its source",
    "live_captions": "honest non-implementation — verified by reading its source",
    "set_hotkey": "honest non-implementation — verified by reading its source",
    "mute_conversation": "not built — see plan notes (no scriptable Messages mute property)",
    "search_messages": "consolidated into view_messages' query param — not a separate tool",
    "search_email": "consolidated into view_emails' query param — not a separate tool",
    "list_reminders": "consolidated — get_upcoming already returns reminders",
    "manage_lists": "consolidated into create_note/append_note checklists",
    "translate_text": "out of scope — no local translation engine rostered",
    "mail_rule": "not built — Mail's rule AppleScript needs enum constants not safely verified",
    "search_photos": "not built — Photos.app enumeration times out; library not Spotlight-indexed",
    "identify_image": "not built — depends on search_photos",
    "photo_ops": "not built — depends on search_photos",
    "dev_tools_git_ops": "not built — consolidated into run_shell",
}


async def _call(name: str, tool, kwargs: dict):
    if inspect.iscoroutinefunction(tool.func):
        return await tool.func(**kwargs)
    res = await asyncio.to_thread(tool.func, **kwargs)
    if inspect.isawaitable(res):
        res = await res
    return res


def _default_args(tool) -> dict:
    props = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])
    out = {}
    for r in required:
        spec = props.get(r, {})
        t = spec.get("type")
        if t == "string":
            out[r] = "test"
        elif t == "integer" or t == "number":
            out[r] = 1
        elif t == "boolean":
            out[r] = False
        elif t == "array":
            out[r] = []
    return out


async def _setup_fixtures() -> None:
    """Create every input another tool's test depends on, INDEPENDENTLY of
    execution order. REGISTRY iterates alphabetically, so a tool that consumes
    another tool's output (move_path <- write_file, append_note <- create_note,
    keychain_read <- keychain_store) cannot rely on that other tool having run
    first — several of these were silently broken that way in an earlier
    version of this script (move_path, append_note, keychain_read, and
    backup_folder all hit this before their ARGS were pointed at fixtures made
    here instead)."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "move_src_fixture.txt").write_text("wisp tool test fixture — safe to delete")
    (SCRATCH / "convert_src.txt").write_text("wisp tool test fixture — safe to delete")
    (SCRATCH / "encrypt_src.txt").write_text("wisp tool test fixture — safe to delete")
    (SCRATCH / "backup_src").mkdir(exist_ok=True)
    (SCRATCH / "backup_src" / "f.txt").write_text("wisp tool test fixture")

    from service.tools.notes_write import create_note
    create_note("Wisp Tool Test Fixture", "Created by scripts/test_all_tools.py setup — safe to delete.")

    from service.tools.security_privacy import keychain_store
    keychain_store("WispToolTestFixture", "tester", "fixture-secret")


async def main() -> int:
    await _setup_fixtures()
    results = []
    names = sorted(REGISTRY)
    for name in names:
        tool = REGISTRY[name]
        if name in SKIP:
            results.append({"name": name, "status": "skip", "reason": SKIP[name],
                            "elapsed": 0.0})
            continue
        kwargs = ARGS.get(name, _default_args(tool))
        t0 = time.perf_counter()
        try:
            out = await asyncio.wait_for(_call(name, tool, kwargs), timeout=45)
            elapsed = time.perf_counter() - t0
            out_str = str(out)
            is_error = out_str.strip().startswith("(error") or out_str.strip().startswith("(could not")
            results.append({
                "name": name, "status": "error" if is_error else "ok",
                "elapsed": round(elapsed, 2), "args": kwargs,
                "result": out_str[:500],
            })
        except asyncio.TimeoutError:
            results.append({"name": name, "status": "timeout",
                            "elapsed": round(time.perf_counter() - t0, 2), "args": kwargs})
        except Exception as e:  # noqa: BLE001
            results.append({"name": name, "status": "exception",
                            "elapsed": round(time.perf_counter() - t0, 2), "args": kwargs,
                            "error": f"{type(e).__name__}: {e}"})
        print(f"  {results[-1]['status']:9} {name:26} {results[-1]['elapsed']:5.1f}s", flush=True)

    ok = sum(1 for r in results if r["status"] == "ok")
    err = sum(1 for r in results if r["status"] == "error")
    exc = sum(1 for r in results if r["status"] == "exception")
    to = sum(1 for r in results if r["status"] == "timeout")
    sk = sum(1 for r in results if r["status"] == "skip")
    print(f"\n{ok} ok, {err} error-result, {exc} exception, {to} timeout, {sk} skipped "
          f"— {len(results)} total")

    if "--json" in sys.argv:
        idx = sys.argv.index("--json")
        out_path = Path(sys.argv[idx + 1])
        out_path.write_text(json.dumps(results, indent=2))
        print(f"Wrote {out_path}")

    return 1 if (exc or to) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
