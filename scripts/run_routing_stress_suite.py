#!/usr/bin/env python3
"""Real Wisp router/model/agent/policy, exclusively synthetic tool implementations.

Never calls /agent. No native bridge, original tool body, shell, external URL,
timer, or actual send is reachable. Outputs are resumable per-case JSON files.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextvars
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ACTIVE = contextvars.ContextVar("stress_case")
CLOCK = datetime(2026, 8, 31, 10, 0)
PHONE = "+1-202-555-0104"
EMAIL = "johnstandark@gmail.com"
UNAVAILABLE = set("set_hotkey transit_info track_flight country_info track_package find_local_events identify_song get_lyrics lookup_media_title transcribe_audio live_captions set_keyboard_backlight".split())
RUNTIME = {}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=list) + "\n")
    temp.replace(path)


def safe_audit(event, args):
    if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "pty.spawn"}:
        raise RuntimeError("STRESS SAFETY: process execution forbidden")
    if event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"} or address[1] != 8000:
            raise RuntimeError("STRESS SAFETY: only local model port 8000 permitted")
    if event == "open" and len(args) > 2:
        path, mode, flags = args[:3]
        writes = isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        if writes and isinstance(path, (str, bytes)):
            actual = Path(os.fsdecode(path)).resolve()
            if not actual.is_relative_to(RUNTIME["output"].resolve()):
                raise RuntimeError(f"STRESS SAFETY: write outside run directory: {actual}")


def fixture(name, args):
    """Data results, never expected-tool lists or evaluator instructions."""
    state = ACTIVE.get()
    case = state["case"]
    root = f"/tmp/wisp-routing-fixtures/{case['id'].lower()}"
    condition = case.get("condition") or {}
    if condition.get("source_tool") == name:
        return condition["source_result"]
    if name in UNAVAILABLE:
        return "Unavailable: this feature has no working provider or native bridge. No action was performed."
    if case["group_number"] == 18:
        title = case["title"]
        if name == "lookup_contact" and "Two matching contacts" in title:
            return "Two contacts: Mom East +1-202-555-0111; Mom West +1-202-555-0112."
        if name == "get_upcoming" and "Two matching calendar" in title:
            return "Calendar: Route review, tomorrow 10:00, id review-a. Calendar: Route review, tomorrow 15:00, id review-b."
        if name == "search_notes" and "Two matching notes" in title:
            return "Two notes titled Route packing: Personal (id note-1), Shared (id note-2). Neither is preferred."
        if name == "list_scheduled_sends" and "Two queued sends" in title:
            return "Pending: Route text 14:00 id route-q1; Route text 18:00 id route-q2."
    if name == "lookup_contact":
        return f"Mom: phone {PHONE}; email mom.fixture@example.test. One contact matched."
    if name == "list_contacts":
        return "Mom; Route East; Route West; Route Reviewer."
    if name in {"view_emails", "summarize_emails", "triage_inbox", "summarize_thread"}:
        names = ["delivery", "archive", "read-state", "followup", "forwarding", "lease", "candidate"]
        bodies = {"delivery": "Delivery code BOX-4821. Pickup at 4 PM. Bring ID.", "lease": "Please review the lease by Friday.", "forwarding": "Four samples are ready."}
        return "\n\n".join(f"Message-ID: <route-{n}@example.test>\nAccount: Work\nFrom: route.sender@example.test\nSubject: Route {n if n == 'delivery' else n + ' test'}\nDate: 2026-08-30 13:00\nUnread: true\nBody: {bodies.get(n, 'Automated receipt. No reply needed.')}" for n in names)
    if name in {"view_messages", "summarize_messages"}:
        return f"Conversation: Mom ({PHONE})\n2026-08-30 12:00 | Mom [received]: Pickup is at 42 Example Lane.\n2026-08-30 12:05 | You [sent]: Thanks, I will bring ID."
    if name == "search_notes":
        return "Title: Route packing; id packing-1; folder Personal\nBody: Jacket; charger; passport.\nTitle: Route groceries; id groceries-1; folder Personal\nBody: Bread."
    if name in {"get_upcoming", "get_past_events"}:
        return "\n".join(f"{x['source']}: {x['title']} | {datetime.fromtimestamp(x['when_ts']).isoformat()} | {x.get('location','')} | id {x['id']}" for x in calendar_rows())
    if name == "find_free_time":
        return "2026-09-01 free 10:00-11:00 and 16:00-17:00. Times are local; 45-minute slots fit."
    if name == "read_file":
        path = str(args.get("path", ""))
        if path.endswith("status.txt"):
            return state["files"].get(path, "(error: file not found)")
        return "Route sample costs 18 dollars each; quantity 4."
    if name == "find_files":
        return root + "/Route proposal.txt"
    if name == "list_dir":
        return root + "/\n  Route proposal.txt\n  input/a.png\n  input/b.png\n  input/keep.pdf\n  input/receipt.txt\n  input/disposable.tmp\n  input/report.txt\n  keep/share.pdf\n  output/ (empty)"
    if name == "write_file":
        state["files"][str(args.get("path"))] = str(args.get("content", ""))
    if name == "organize_files":
        return ("Moved" if args.get("confirm") else "Preview only; no files moved:") + f" {root}/input/a.png -> {root}/images/a.png; {root}/input/b.png -> {root}/images/b.png. PDF unchanged."
    if name == "clear_memory":
        return ("Deleted two matching facts." if args.get("confirm") else "Preview only: Route old job office was Room 3; Route old job commute was 20 minutes. No deletion.")
    if name == "clear_reminders":
        return "Deleted matching reminders only; Calendar events unchanged."
    if name == "clear_past_reminders":
        return "Deleted matching past-due reminder and past Calendar event; future items unchanged."
    if name == "recall":
        return "Saved fact id bicycle-1: Route bicycle is blue. Saved fact id locker-1: old Route locker is number 12."
    if name == "use_skill":
        requested = str(args.get("name", ""))
        skill = RUNTIME["skills"].get(requested)
        return skill.body if skill else "(error: no installed skill matches that name)"
    if name in {"send_message", "send_email", "reply_to_email", "forward_email"}:
        return "Sent successfully: " + json.dumps(args)
    if name == "add_reminder":
        return "Reminder set: " + json.dumps(args)
    if name in {"draft_email", "draft_message"}:
        return "Draft prepared, not sent: " + json.dumps(args)
    if name == "schedule_send":
        return "Scheduled id route-new-ping: " + json.dumps(args)
    if name in {"airdrop_file", "unsubscribe", "scan_to_note"}:
        return "Manual handoff ready. You must complete the final step yourself; transfer/subscription/scan not completed."
    if name == "run_shell":
        return root if args.get("cmd") == "pwd" else "(error: only the fixture pwd response is available; command was not executed)"
    if name == "run_applescript":
        return "5" if "return 2 + 3" in str(args) else "(error: unsupported fixture script; nothing executed)"
    if name == "count_vowels":
        return str(sum(x in "aeiou" for x in str(args.get("word", "")).lower()))
    if name == "human_shape":
        return " O\n/|\\\n/ \\"
    if name == "ascii_art_generator":
        return " /\\\n/__\\" if "triangle" in str(args).lower() else "+---+\n|   |\n+---+"
    if name == "business_days_between":
        try:
            start = datetime.fromisoformat(args["start"])
            end = datetime.fromisoformat(args["end"])
            return str(sum((start + timedelta(days=i)).weekday() < 5 for i in range((end-start).days+1)))
        except (KeyError, ValueError):
            return "(error: specify valid start and end dates)"
    if name in READ_RESULTS:
        return READ_RESULTS[name].replace("{model}", RUNTIME["model"])
    tool = RUNTIME["registry"][name]
    if tool.category.endswith("read") or name in {"calculate", "convert_units", "convert_currency", "world_time"}:
        state["fixture_gaps"].append(name)
        return "SOURCE_UNAVAILABLE: no configured synthetic result for this capability. No facts verified."
    return "Completed in fixture: " + name + " " + json.dumps(args, ensure_ascii=False)


READ_RESULTS = {
    "calculate": "11.61", "convert_units": "180 lb = 81.6466 kg; 68 F = 20 C.",
    "convert_currency": "2026-08-31 rate: 1 USD = 0.90 EUR; 100 USD = 90 EUR.",
    "world_time": "2026-08-31: Tokyo UTC+9, London UTC+1; Tokyo is eight hours ahead.",
    "get_volume": "Output volume: 40%; muted: false", "clipboard_read": "Route clipboard fixture",
    "get_battery_status": "charge: 35%; health: 92%; cycle count: 210",
    "list_bluetooth_devices": "Route Headphones (connected)",
    "system_status": "Free disk 120 GB; memory pressure normal; CPU 12%; uptime 6 hours.",
    "network_info": "Local IP 192.0.2.10; public IP 198.51.100.10; Wi-Fi RouteLab",
    "software_update": "Available: Route test security update. No updates installed.",
    "manage_login_items": "Route Helper (enabled)",
    "wisp_status": "Current model: {model}; loaded: {model}. Inference local; some tools use external services.",
    "wisp_skills": "Enabled: interview-me, ascii-art-generator, business-days-between, count-vowels, human-shape. interview-me instructions not loaded.",
    "wisp_mcp": "No configured MCP servers; zero MCP tools.",
    "wisp_sync": "Mail, Messages, Notes synced 2026-08-31 09:55 local.",
    "show_profile": "Fixture user: Adi Jain. No saved location or reminder-time preference.",
    "search_coverage": "Email retained 90 days; Messages 30 days; browser history 30 days. Older records unavailable.",
    "search_browser_history": "2026-08-30 09:30 Safari | Route project | https://project.example.test/route",
    "get_recent_activity": "Last six hours: Route delivery email received; Route packing note updated; Route review Calendar event added.",
    "daily_brief": "Today: Route review at 14:00. Oakland: clear, 22 C. Route delivery needs attention.",
    "search_conversations": "Session route-renovation-1: You discussed painting the Route renovation room blue.",
    "read_log": "2026-08-30 workout: Walked 3 miles. 2026-08-28 workout: Walked 2 miles.",
    "health_summary": "Manually logged: two walks in seven days. No wearable data or clinical conclusions.",
    "list_scheduled_sends": "[route-old-ping] Pending text Route old ping tomorrow 18:00; [route-keep-ping] Pending text Keep me tomorrow 19:00.",
    "manage_timers": "route-old-timer: 60 seconds remaining; route-keep-timer: 120 seconds remaining.",
    "contact_dates": "Mom: September 12. No birth year recorded.",
    "scan_subscriptions": "Weekly Route: 12 messages; Route Receipts: 4 messages.",
    "web_search": "Route Museum visitor policy: https://museum.example.test/visitors — open Tuesday-Sunday, 10 AM-5 PM; bring ID.",
    "web_fetch": "Route Museum visitor policy. Open Tuesday-Sunday 10 AM-5 PM. Bring ID. Source: https://museum.example.test/visitors",
    "get_weather": "Dublin, California: tomorrow 22 C, clear, rain chance 10%. Oakland, California: tomorrow rain chance 60%.",
    "weather_alerts": "Miami, Florida: active severe thunderstorm warning, issued 2026-08-31 10:00 local.",
    "air_quality": "Oakland AQI 42, good. Measured 2026-08-31 10:00 local.",
    "rain_radar": "Oakland next six hours precipitation chance: 10%, 10%, 20%, 20%, 30%, 30%. Text forecast, not radar imagery.",
    "get_stock_price": "NVDA: three-week start 2026-08-10 $170, end 2026-08-31 $180; change +$10 (+5.88%). Synthetic market fixture.",
    "wikipedia_summary": "Golden Gate Bridge: suspension bridge connecting San Francisco and Marin County, opened in 1937.",
    "define_word": "serendipity: finding something valuable or pleasant by chance.",
    "get_sports_scores": "NBA fixture scoreboard: Oakland 101, New York 99 (final). Synthetic teams and scores.",
    "recipe_lookup": "Carbonara: pasta, egg, pecorino, pepper, guanciale. Cook pasta; render guanciale; combine off heat with egg and cheese.",
    "astronomy": "Seattle 2026-08-31 sunrise 06:27, sunset 19:52 PDT (fixture).",
    "find_place": "Route Coffee, 42 Example Lane, 0.8 km from Oakland City Hall; Route Cafe, 12 Example Street, 1.4 km.",
    "travel_time": "San Francisco to Oakland: 20 km, 25 minutes driving. Free-flow estimate; traffic not included.",
    "list_shortcuts": "Route Focus; Wisp DND On; Wisp DND Off",
    "list_apps": "Safari; Calculator; Music; Spotify; Netflix; Route Demo App",
    "list_windows": "Safari: Route project (id 101); Notes: Route packing (id 102)",
    "app_status": "Safari running; Calculator stopped.",
    "now_playing": "Music: Route Morning by Fixture Artist; playing.",
    "search_podcasts": "Route Science, https://podcast.example.test/route-science/feed.xml",
    "get_podcast_episodes": "Route Science episode 1: Sample day, https://podcast.example.test/episode-1.mp3",
    "keychain_read": "dummy-existing-route-secret",
    "generate_password": "Dummy!RoutePassword24#XYZ",
    "join_video_call": "Opened stored URL https://meet.example.test/route-standup; joining the call still requires the user.",
    "wisp_capabilities": "Wisp can send texts after confirmation. It cannot interpret screenshots or images.",
}


def calendar_rows():
    today = CLOCK.replace(hour=14)
    entries = [
        ("Design review", "calendar", today + timedelta(days=1), "Room 4"),
        ("Atlas retrospective", "calendar", today - timedelta(days=7), "Room 4"),
        ("Route rent", "reminder", today + timedelta(days=1), ""),
        ("Route dentist", "calendar", today + timedelta(days=1), "Clinic 2"),
        ("Route obsolete lunch", "calendar", today + timedelta(days=1), "Cafe 4"),
        ("Route cleanup one", "reminder", today, ""),
        ("Route cleanup two", "reminder", today + timedelta(hours=1), ""),
        ("Route cleanup tomorrow", "reminder", today + timedelta(days=1), ""),
        ("Route cleanup Calendar", "calendar", today, ""),
        ("Route expired reminder", "reminder", today - timedelta(days=2), ""),
        ("Route expired event", "calendar", today - timedelta(days=2), ""),
        ("Route expired future", "calendar", today + timedelta(days=2), ""),
        ("Route dentist booking", "reminder", today + timedelta(days=1), ""),
        ("Route standup", "calendar", today, "https://meet.example.test/route-standup"),
    ]
    return [dict(id=f"fixture-{i}", title=t, source=s, when_ts=d.timestamp(), location=l, duration_min=30, status="active") for i, (t,s,d,l) in enumerate(entries)]


def bootstrap(output, model):
    output.mkdir(parents=True, exist_ok=True)
    home = output / "isolated_home"
    home.mkdir(exist_ok=True)
    os.environ["WISP_HOME"] = str(home)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    # Copy configuration, not user stores. No keys or settings are printed.
    overlay = Path.home() / ".moe/config.yaml"
    if overlay.exists():
        shutil.copyfile(overlay, home / "config.yaml")
    skills_dir = Path.home() / ".moe/skills"
    for source in skills_dir.glob("*/SKILL.md"):
        destination = home / "skills" / source.parent.name / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    RUNTIME.update(output=output, model=model)
    sys.addaudithook(safe_audit)
    from service import config, skills
    from service.tools import registry, assistant_tools, tool_authoring
    from service.agent import loop
    from service.memory import identity, prompt_blocks
    from service.safety import policy
    from service.router.router import route
    from service.inference.omlx_client import OMLXClient
    import service.router.router as router_module
    import service.tools.timeranges as timeranges
    class FixtureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOCK.astimezone(tz) if tz else CLOCK
    loop.datetime = router_module.datetime = timeranges.datetime = FixtureDatetime
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    effective = config.models_config()
    # Preserve Wisp's actual role assignments, including misconfigured roles.
    # The inference server's /health default is NOT Wisp's assistant role.
    loaded = skills.load()
    # Original functions are discarded, including installed skill runners.
    async def intercepted(tool, args):
        state = ACTIVE.get()
        error = registry._validate_args(tool, args)
        if error:
            state["schema_errors"].append({"tool": tool.name, "args": args, "error": error})
            return error
        value = fixture(tool.name, args)
        state["dispatches"].append({"name": tool.name, "args": args, "result": value, "at_s": time.monotonic()-state["started"]})
        return value
    for tool in registry.REGISTRY.values():
        name = tool.name
        async def replacement(_name=name, **kwargs):
            return fixture(_name, kwargs)
        tool.func = replacement
    registry.run_tool = intercepted
    loop.run_tool = intercepted
    # These helpers run before run_tool in production, so intercept them too.
    assistant_tools.reminders_matching = lambda scope, query="": [r for r in calendar_rows() if r["source"] == "reminder" and query.lower() in r["title"].lower() and (scope == "all" or (scope == "today" and datetime.fromtimestamp(r["when_ts"]).date() == CLOCK.date()) or (scope == "past_due" and r["when_ts"] < CLOCK.timestamp()) or (scope == "tomorrow" and datetime.fromtimestamp(r["when_ts"]).date() == (CLOCK+timedelta(days=1)).date()) or (scope == "upcoming" and r["when_ts"] >= CLOCK.timestamp()))]
    assistant_tools.past_due_matching = lambda query="", days=365: [r for r in calendar_rows() if r["when_ts"] < CLOCK.timestamp() and query.lower() in r["title"].lower()]
    async def prepared(args):
        return ""
    tool_authoring.prepare_draft = prepared
    tool_authoring.draft_code = lambda name: "# Synthetic code preview only; installation is intercepted."
    tool_authoring.draft_scope_line = lambda name: "No host access in this test."
    tool_authoring.draft_warning = lambda name: ""
    identity.user_name = lambda: "Adi Jain"
    identity.user_emails = lambda: [EMAIL] if "self-send" in ACTIVE.get({"case": {"title": ""}})["case"].get("title", "").lower() else ["adijain888@gmail.com"]
    prompt_blocks.memory_block = lambda: ""
    prompt_blocks.now_line = lambda **kwargs: "\nThe current date and time is Monday, August 31, 2026 at 10:00 AM, America/Los_Angeles. Resolve relative dates against this."
    RUNTIME.update(registry=registry.REGISTRY, loop=loop, policy=policy, route=route, client_type=OMLXClient, skills=loaded)
    inventory = json.loads((ROOT / "docs/WISP_TOOL_ACTIVATION_INVENTORY.json").read_text())
    missing = {x["name"] for x in inventory["tools"]} - set(registry.REGISTRY)
    if missing:
        raise RuntimeError(f"Cannot test tools absent from isolated registry: {sorted(missing)}")
    dump(output / "registry_snapshot.json", [{"name": t.name, "category": t.category, "schema": t.schema()} for t in registry.REGISTRY.values()])
    dump(output / "run_metadata.json", {"model":model, "clock": CLOCK.isoformat(), "test_email":EMAIL, "test_contact":"Mom", "phone":PHONE, "policy_full_access":policy.full_access(), "policy_read_only":policy.read_only(), "isolation":"all tool functions replaced; only localhost:8000 socket connections permitted; subprocess forbidden; writes restricted to run directory", "suite_sha256":hashlib.sha256((ROOT / "test_fixtures/routing_stress/suite.json").read_bytes()).hexdigest(), "source_sha256":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT / "service/agent/loop.py", ROOT / "service/router/router.py", ROOT / "service/safety/policy.py"]}})


class Approver:
    def __init__(self, verdict="approve"):
        self.verdict = verdict

    async def confirm(self, action):
        from service.agent.approver import InteractiveApprover
        state = ACTIVE.get()
        state["approvals"].append({**action, "simulated_response":self.verdict, "at_s":time.monotonic()-state["started"]})
        async def emit(event):
            state["events"].append({**event,"at_s":time.monotonic()-state["started"]})
            if event["type"] == "confirm" and self.verdict != "timeout":
                interactive.resolve(action["id"], self.verdict == "approve", "once")
        interactive = InteractiveApprover(emit, timeout=0.02 if self.verdict == "timeout" else 2)
        return await interactive.confirm(action)


class MeasuredClient:
    def __init__(self, real):
        self.real = real

    async def ensure_only(self, *args, **kwargs):
        # Warm resident model established once. Avoid unloading another test's embedder.
        return None

    async def stream_events(self, model, messages, **kwargs):
        state = ACTIVE.get()
        started = time.monotonic()
        step = {"index":len(state["model_steps"])+1, "offered_tools":[s["function"]["name"] for s in kwargs.get("tools", [])], "max_tokens":kwargs.get("max_tokens"), "input_chars":len(json.dumps(messages)), "reasoning_chars":0, "content_chars":0, "calls":[], "first_token_s":None}
        state["model_steps"].append(step)
        stream = self.real.stream_events(model, messages, **kwargs)
        try:
            async for event in stream:
                if event["kind"] in {"reasoning", "content"}:
                    if step["first_token_s"] is None:
                        step["first_token_s"] = time.monotonic()-started
                    step[event["kind"]+"_chars"] += len(event.get("text", ""))
                if event["kind"] == "final":
                    msg = event["message"]
                    step["calls"] = msg.get("tool_calls") or []
                    step["answer"] = msg.get("content", "")
                    step["finish_reason"] = event.get("finish_reason")
                yield event
        finally:
            await stream.aclose()
            step["elapsed_s"] = time.monotonic()-started


def new_state(case):
    return dict(case=case, started=time.monotonic(), approvals=[], dispatches=[], events=[], model_steps=[], schema_errors=[], fixture_gaps=[], files={})


def grade(case, state, route, answer, error):
    raw = []
    for step in state["model_steps"]:
        for call in step["calls"]:
            raw.append(call.get("function", {}).get("name", ""))
    direct = [x[0] for x in route.get("direct_calls") or []]
    called = [e["name"] for e in state["events"] if e.get("type") == "tool_call"]
    emitted = raw + direct
    dispatched = [d["name"] for d in state["dispatches"]]
    required = set(case["required_tools"])
    missing = required - set(emitted)
    equivalents = []
    if "use_skill" in missing:
        from service.skills import skills_context_block
        block = skills_context_block(case["prompt"], "")
        skill = RUNTIME["skills"].get("interview-me")
        if skill and skill.body.strip() in block:
            missing.remove("use_skill")
            equivalents.append("interview-me skill context injected before calls; see skill context behavior")
    if "set_display" in missing and "set_appearance" in emitted and "dark mode" in case["prompt"]:
        missing.remove("set_display")
        equivalents.append("set_appearance for dark-mode request")
    if "set_appearance" in missing and "set_display" in emitted and "automatic" not in case["prompt"]:
        missing.remove("set_appearance")
        equivalents.append("set_display candidate equivalent; argument review needed")
    forbidden = sorted(set(case["forbidden_tools"]) & set(emitted))
    offered = set(route.get("tool_subset") or []) if route.get("needs_tools") else set()
    menu_missing = sorted(required-offered)
    sequence = called
    ordering = [f"{a} before {b}" for a,b in case["ordering_edges"] if a in sequence and b in sequence and sequence.index(a)>sequence.index(b)]
    unauthorized = []
    for d in state["dispatches"]:
        decision = RUNTIME["policy"].decide(RUNTIME["registry"][d["name"]].category, d["args"], tool=d["name"])
        if decision.tier.value == "deny":
            unauthorized.append(d["name"]+": dispatched despite deny")
        elif decision.tier.value == "confirm" and not any(a["simulated_response"] == "approve" and (a["tool"] == d["name"] or a["tool"] == "calendar_changes" and d["name"] in {"cancel_event","add_calendar_event"}) for a in state["approvals"]):
            unauthorized.append(d["name"]+": dispatched without approval")
    arg_findings = []
    for d in state["dispatches"]:
        name,args=d["name"],d["args"]
        if name in {"send_email","draft_email","forward_email"} and EMAIL in case["prompt"] and str(args.get("to","")).lower()!=EMAIL:
            arg_findings.append(f"{name}: test email recipient mismatch")
        if name in {"send_message","draft_message","place_call"} and "Mom" in case["prompt"]:
            if str(args.get("to","")).lower() not in {PHONE, "mom", "+1-202-555-0110"}:
                arg_findings.append(f"{name}: unexpected Mom recipient {args.get('to')!r}")
        if name == "organize_files" and "do not move" in case["prompt"] and args.get("confirm"):
            arg_findings.append("organize_files: moved despite preview-only instruction")
    clarify_warning = bool(case["clarification_expected"]) and "?" not in answer
    issues = bool(missing or forbidden or ordering or unauthorized or arg_findings or state["schema_errors"] or error)
    return {"automated_routing_pass":not issues, "missing_required_emissions":sorted(missing), "missing_required_dispatches":sorted(required-set(dispatched)), "missing_from_router_menu":menu_missing, "forbidden_emissions":forbidden, "ordering_violations":ordering, "permission_violations":unauthorized, "argument_findings":arg_findings, "clarification_review_flag":clarify_warning, "accepted_equivalents":equivalents, "raw_model_calls":raw, "direct_calls":direct, "policy_evaluated_calls":called, "dispatched_tools":dispatched, "semantic_argument_and_final_claim_grading":"limited deterministic checks plus report review; not a complete semantic oracle"}


async def run_case(case, client, setup, timeout, verdict="approve"):
    state = new_state(case)
    token = ACTIVE.set(state)
    route = {}
    answer = error = ""
    route_s = 0.0
    try:
        async with asyncio.timeout(timeout):
            history = case.get("context_turns", [])
            last_assistant = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), None)
            start=time.monotonic()
            decision = await RUNTIME["route"](case["prompt"], last_assistant=last_assistant, last_tools=None)
            route_s = time.monotonic()-start
            route = dataclasses.asdict(decision)
            async def emit(event):
                if event.get("type") in {"tool_call", "tool_result", "error", "forced_step", "retry"}:
                    state["events"].append({**event,"at_s":time.monotonic()-state["started"]})
            messages = [{"role":"user","content":setup}, *history, {"role":"user","content":case["prompt"]}]
            if decision.needs_tools:
                kwargs = {k:getattr(decision,k) for k in ["force_first_tool","expect_tool_first","multi_round","narration_after","direct_calls","required_tool_groups","forbidden_tools","conditional_tools","reminder_action"]}
                hint = ""
                if decision.clarify_channel:
                    hint += "Ask whether the user means text or email before sending."
                if decision.clarify_target:
                    hint += "Ask the user which target before acting."
                answer = await RUNTIME["loop"].run_agent(client, decision.model, messages, emit, Approver(verdict), tools=decision.tool_subset, short_circuit_tools={"summarize_emails","summarize_messages"}, style_hint=hint or None, test_mode=False, debug=False, **kwargs)
            else:
                from service.memory import prompt_blocks
                msgs=[{"role":"system","content":"You are Wisp, a private assistant. Answer concisely."+prompt_blocks.now_line()}, *messages]
                async for ev in client.stream_events(decision.model,msgs,tools=[],max_tokens=3000):
                    if ev["kind"] == "final":
                        answer=ev["message"].get("content", "")
    except TimeoutError:
        error=f"CASE_TIMEOUT: {timeout}s"
    except Exception as exc:
        error=f"{type(exc).__name__}: {exc}"
    elapsed=time.monotonic()-state["started"]
    result={"id":case["id"],"category":case["category"],"title":case["title"],"prompt":case["prompt"],"prompt_sha256":case["prompt_sha256"],"model":RUNTIME["model"],"approval_mode":verdict,"elapsed_s":elapsed,"route_s":route_s,"route":route,"answer":answer,"error":error,"grading":grade(case,state,route,answer,error), **{k:state[k] for k in ["model_steps","events","approvals","dispatches","schema_errors","fixture_gaps"]}}
    ACTIVE.reset(token)
    return result


async def main_async(args):
    corpus=json.loads((ROOT / "test_fixtures/routing_stress/suite.json").read_text())
    bootstrap(Path(args.output).resolve(),args.model)
    actual=RUNTIME["client_type"](timeout=args.timeout)
    print("Loading Wisp assistant model " + args.model, flush=True)
    await actual.ensure_only(args.model)
    print("Model ready; starting selected cases", flush=True)
    client=MeasuredClient(actual)
    cases=corpus["cases"]
    if args.ids:
        wanted=set(args.ids.split(",")); cases=[c for c in cases if c["id"] in wanted]
    if args.id_from:
        cases=[c for c in cases if int(c["id"].split("-")[1]) >= args.id_from]
    if args.id_to:
        cases=[c for c in cases if int(c["id"].split("-")[1]) <= args.id_to]
    if args.limit:
        cases=cases[:args.limit]
    semaphore=asyncio.Semaphore(args.concurrency)
    completed=0
    started=time.monotonic()
    async def work(case):
        nonlocal completed
        path=RUNTIME["output"] / "cases" / (case["id"]+".json")
        if args.resume and path.exists():
            if args.retry_error_substring:
                try:
                    previous=json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    previous={}
                if args.retry_error_substring not in str(previous.get("error", "")):
                    completed+=1
                    return
            else:
                completed+=1
                return
        async with semaphore:
            result=await run_case(case,client,corpus["protocol"]["test_setup_user_instruction"],args.timeout)
            dump(path,result)
            completed+=1
            print(json.dumps({"completed":completed,"total":len(cases),"id":case["id"],"seconds":round(result["elapsed_s"],2),"routing_pass":result["grading"]["automated_routing_pass"],"missing":result["grading"]["missing_required_emissions"],"error":result["error"]}),flush=True)
            dump(RUNTIME["output"] / "progress.json",{"completed":completed,"total":len(cases),"elapsed_s":time.monotonic()-started,"last_case":case["id"]})
    try:
        await asyncio.gather(*(work(c) for c in cases))
    finally:
        await actual.aclose()
    print("ALL_SELECTED_CASES_COMPLETE",flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model",required=True)
    p.add_argument("--output",required=True)
    p.add_argument("--ids",default="")
    p.add_argument("--id-from",type=int,default=0)
    p.add_argument("--id-to",type=int,default=0)
    p.add_argument("--limit",type=int,default=0)
    p.add_argument("--timeout",type=float,default=90)
    p.add_argument("--concurrency",type=int,default=2)
    p.add_argument("--resume",action="store_true")
    p.add_argument("--retry-error-substring",default="",help="With --resume, rerun only existing cases whose error contains this text")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
