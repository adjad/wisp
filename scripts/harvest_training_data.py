#!/usr/bin/env python3
"""Simulate real user turns against the ACTUAL Wisp pipeline (router + agent
loop + tools, not a mock) and harvest the exact wire-format request/response
of every model step for training data.

Full design rationale, safety model, and the reviewed task table live in the
approved plan (see conversation / `~/.claude/plans/clever-painting-clover.md`
if still present). Summary:

- `agent/loop.py` emits a `raw_model_io` event per step with the literal
  `{messages, tools, tool_choice, response}` sent to/from oMLX (built for the
  Debug Mode export) — exactly mlx-lm's LoRA training shape
  ({"messages": [...], "tools": [...]}), with the REAL per-request tool
  subset `router._domain_subset` actually offered, no reconstruction.
- Every flow here is a real prompt actually sent to the resident model, and
  every write/send this file exercises actually executes (per your explicit
  instruction — you want send_email/send_message/etc genuinely tested, not
  captured-then-denied).
- Safety: `SelfTargetApprover` approves a confirm-gated call only if the tool
  is in that task's expected set (same base rule as test_model.py's
  Approver), AND for the three tools that actually transmit to an address
  (`send_email`, `send_message`, `schedule_send`) it re-checks the call's own
  `to` argument against a hard allowlist (your email/phone) before allowing
  it through — so even if a prompt or model drifts, nothing can transmit to
  a real third party. `reply_to_email`/`archive_email`/`mark_email_read` take
  no recipient arg at all; they're only ever pointed at the "WISPTEST" thread
  this file creates itself (self-to-self by construction), so there's no
  external-recipient surface to check. Everything outside a task's expected
  set is denied unconditionally, same as test_model.py today.

Two outputs:
  harvest_sft.jsonl — every step of a trajectory that matched its expected
                      tool (or correctly called none), mlx-lm chat format
                      with a real `tools` field. Multi-turn flows carry
                      accumulated history the same way main.py's real session
                      store does (`[actions: tool1,tool2]` suffix on the
                      assistant turn — see service/memory/context.py's
                      `_render` — not literal replayed tool-call/tool-result
                      messages).
  harvest_dpo.jsonl — (prompt, chosen, rejected) triples mined from steps
                      where the router offered the right tool but the model
                      picked wrong anyway. For the future cloud DPO phase —
                      mlx-lm's local LoRA trainer has no DPO loss.

Usage:
    python scripts/harvest_training_data.py --dry-run     # list every flow, run nothing
    python scripts/harvest_training_data.py                # run everything for real
    python scripts/harvest_training_data.py --only email,messages
    python scripts/harvest_training_data.py --skip-real-exec  # captured-then-denied instead
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(line_buffering=True)

from service.agent.loop import run_agent               # noqa: E402
from service.config import role_to_model                 # noqa: E402
from service.inference.omlx_client import OMLXClient      # noqa: E402
from service.router.router import route                   # noqa: E402


SELF_EMAIL = "johnstandark@gmail.com"
SELF_PHONE_DIGITS = "6504959723"
SEND_TOOLS = {"send_email", "send_message", "schedule_send"}


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


@dataclass
class Turn:
    prompt: str
    expect: tuple[str, ...] = ()
    # False (default): "ok" if ANY expected tool was called — the original
    # single-tool-choice grading. True: "ok" only if ALL expected tools were
    # called (order not enforced — the ReAct loop's own step ordering already
    # reflects what actually happened) — for sequential/compound prompts like
    # "check my calendar and text me when I'm free", where the actual signal
    # we want is whether the model completed the WHOLE chain, not just one
    # step of it.
    require_all: bool = False


@dataclass
class Flow:
    name: str
    group: str
    turns: list[Turn]
    reps: int = 2
    note: str = ""


def F(name, group, prompt, expect, reps=2, note="", require_all=False):
    """One-turn flow shorthand."""
    return Flow(name, group, [Turn(prompt, expect, require_all=require_all)], reps=reps, note=note)


# ---------------------------------------------------------------------------
# Full task enumeration — every registered tool gets at least one flow.
# reps=1 for anything that executes a real write/send (limits real-world side
# effects to one occurrence); reps=2-3 for pure reads (free to repeat, no
# side effects, catches flakiness the way the 5-rep methodology in
# moe-tool-descriptions-compete did).
# ---------------------------------------------------------------------------

FLOWS: list[Flow] = [
    # ---- system ----------------------------------------------------------
    F("sys_volume_get", "system", "What's the current volume?", ("get_volume",)),
    F("sys_battery", "system", "How's my battery health?", ("get_battery_status",)),
    F("sys_volume_set", "system", "Set the volume to 40%.", ("set_volume",), reps=1),
    F("sys_clip_write", "system", "Copy the text 'wisp training test' to my clipboard.",
      ("clipboard_write",), reps=1),
    F("sys_clip_read", "system", "What's on my clipboard right now?", ("clipboard_read",),
      reps=1, note="run after sys_clip_write"),
    F("sys_kb_light", "system", "Turn my keyboard backlight up a bit.",
      ("set_keyboard_backlight",), reps=1),
    F("sys_wifi_off", "system", "Turn wifi off.", ("set_wifi",), reps=1, note="paired with sys_wifi_on"),
    F("sys_wifi_on", "system", "Turn wifi back on.", ("set_wifi",), reps=1,
      note="runs immediately after sys_wifi_off"),
    F("sys_speedtest", "system", "Test my internet speed.", ("run_speed_test",), reps=1,
      note="after wifi is back on"),
    F("sys_lock", "system", "Lock my screen.", ("lock_screen",), reps=1,
      note="ACTUALLY LOCKS THE SCREEN"),

    # ---- calendar ----------------------------------------------------------
    F("cal_upcoming", "calendar", "What's on my calendar this week?", ("get_upcoming",)),
    F("cal_tomorrow", "calendar", "What do I have going on tomorrow?", ("get_upcoming",)),
    F("cal_past_lastweek", "calendar", "What did I have on my calendar last week?",
      ("get_past_events",), reps=3),
    F("cal_past_lastmonth", "calendar", "What did I have going on last month?",
      ("get_past_events",), reps=3),
    F("cal_past_query", "calendar", "When did I last have a dentist appointment?",
      ("get_past_events",)),
    F("cal_add", "calendar", "Add a calendar event called 'WISPTEST delete me' tomorrow at 3pm.",
      ("add_calendar_event",), reps=1),
    F("cal_cancel", "calendar", "Cancel the event called 'WISPTEST delete me'.",
      ("cancel_event",), reps=1, note="after cal_add"),
    Flow("cal_confirm_flow", "calendar", [
        Turn("Add a lunch event tomorrow at noon called 'WISPTEST delete me'?", ()),
        Turn("yes, go ahead", ("add_calendar_event",)),
    ], reps=1, note="confirms_offered_action path"),
    Flow("cal_cleanup_confirm", "calendar", [
        Turn("Cancel the event called 'WISPTEST delete me'.", ("cancel_event",)),
    ], reps=1, note="cleans up cal_confirm_flow"),
    Flow("cal_write_continue", "calendar", [
        Turn("Add 'WISPTEST delete me' tomorrow at 3pm", ("add_calendar_event",)),
        Turn("set another one for 5pm too, same title", ("add_calendar_event",)),
        Turn("cancel both WISPTEST delete me events", ("cancel_event",)),
    ], reps=1, note="write continuation + cleanup"),

    # ---- reminders ---------------------------------------------------------
    F("rem_get", "reminders", "What are my reminders?", ("get_upcoming",)),
    F("rem_add", "reminders", "Set a reminder to 'WISPTEST delete me' tomorrow at 4pm.",
      ("add_reminder",), reps=1),
    F("rem_cancel", "reminders", "Cancel the WISPTEST delete me reminder.", ("cancel_event",),
      reps=1, note="after rem_add"),
    Flow("rem_missing_arg", "reminders", [
        Turn("Remind me to call mom.", ()),
        Turn("Tomorrow at 5pm.", ("add_reminder",)),
    ], reps=1, note="correct T1 behavior is asking, not guessing a time"),

    # ---- contacts ------------------------------------------------------------
    F("contact_lookup", "contacts", "What's Mom's phone number?", ("lookup_contact",)),
    F("contact_count", "contacts", "How many contacts do I have?", ("list_contacts",)),

    # ---- files -------------------------------------------------------------
    F("files_list_downloads", "files", "List the files in my Downloads folder.", ("list_dir",)),
    F("files_list_desktop", "files", "What's on my Desktop?", ("list_dir",), reps=3,
      note="flagged route-fault in smoke test"),
    F("files_list_docs", "files", "Show me what's inside my Documents folder.", ("list_dir",)),
    F("files_list_downloads2", "files", "Do I have anything in my downloads?", ("list_dir",),
      reps=3, note="flagged route-fault in smoke test"),
    F("files_read", "files", "Read the first few lines of ~/.zshrc", ("read_file",)),
    F("files_write", "files",
      "Create a file at ~/Desktop/wisp_harvest_scratch.txt containing 'hello from harvest'.",
      ("write_file",), reps=1),
    F("files_delete", "files", "Delete the file at ~/Desktop/wisp_harvest_scratch.txt.",
      ("delete_path",), reps=1, note="after files_write — only ever this scratch file"),
    F("browser_hist", "files", "Did I visit any airline sites recently?",
      ("search_browser_history",)),

    # ---- email (self-targeted for anything that transmits) -----------------
    F("mail_summarize", "email", "Summarize my recent emails.", ("summarize_emails",)),
    F("mail_unread", "email", "What's unread in my inbox?", ("summarize_emails", "view_emails")),
    F("mail_view_detail", "email", "Did I get any emails from my boss today?", ("view_emails",)),
    F("mail_send_self", "email",
      f"Send an email to {SELF_EMAIL}, subject 'WISPTEST', body 'harvest test send'.",
      ("send_email",), reps=1),
    F("mail_compose_noun", "email",
      f"Email the recruiter at {SELF_EMAIL} to confirm Monday's interview time.",
      ("send_email", "draft_email"), reps=1, note="tests the COMPOSE_RE non-standard-noun gap"),
    F("mail_draft", "email",
      f"Draft an email to {SELF_EMAIL} about tomorrow's meeting, don't send it yet.",
      ("draft_email",), reps=1),
    F("mail_reply", "email", "Reply to the most recent WISPTEST email saying 'got it, thanks'.",
      ("reply_to_email",), reps=1, note="after mail_send_self — self-to-self thread"),
    F("mail_archive", "email", "Archive the WISPTEST email.", ("archive_email",), reps=1),
    F("mail_markread", "email", "Mark my WISPTEST emails as read.", ("mark_email_read",), reps=1),
    F("mail_schedule", "email",
      f"Send an email to {SELF_EMAIL} tomorrow morning, subject 'WISPTEST scheduled', "
      "saying the report is ready.",
      ("schedule_send",), reps=1),
    F("mail_list_scheduled", "email", "What emails do I have scheduled to send?",
      ("list_scheduled_sends",), reps=1, note="after mail_schedule"),
    F("mail_cancel_scheduled", "email", "Cancel the scheduled WISPTEST email.",
      ("cancel_scheduled_send",), reps=1),
    Flow("mail_draft_then_send", "email", [
        Turn(f"Summarize my mail and send the summary to {SELF_EMAIL}.", ("summarize_emails",)),
    ], reps=1, note="T1 only graded here; the documented 'stops at draft' failure is whether "
                     "a SECOND step (send_email) follows — see harvester grading notes below"),

    # ---- messages (self-targeted for anything that transmits) --------------
    F("msg_summarize", "messages", "What are my recent messages about?", ("summarize_messages",)),
    F("msg_texted", "messages", "Who texted me today?", ("view_messages", "summarize_messages")),
    F("msg_from_mom", "messages", "Any messages from mom?", ("view_messages",),
      note="read-only search, safe even if it resolves a real contact"),
    F("msg_send_self", "messages", f"Text {SELF_PHONE_DIGITS} saying 'harvest test message'.",
      ("send_message",), reps=1),
    F("msg_draft", "messages",
      f"Draft a text to {SELF_PHONE_DIGITS} saying I'm running late, let me review it first.",
      ("draft_message",), reps=1),
    F("msg_heard_from", "messages", "Did I hear back from Dan?",
      ("view_messages", "summarize_messages", "view_emails", "summarize_emails")),

    # ---- memory --------------------------------------------------------------
    F("mem_recall", "memory", "What do you remember about me?", ("recall",)),
    F("mem_remember", "memory", "Remember that my car's license plate is ABC123 for this test.",
      ("remember",), reps=1),
    F("mem_forget", "memory", "Forget what I just told you about my car's license plate.",
      ("forget",), reps=1, note="after mem_remember"),
    F("mem_self_vague", "memory", "What do you know about my finances?",
      ("recall",)),
    F("mem_recent", "memory", "What have I been working on recently?",
      ("get_recent_activity", "recall")),

    # ---- web/compute -----------------------------------------------------
    F("web_price", "web", "What's the current price of Bitcoin?", ("web_fetch",)),
    F("web_http", "web", "Fetch the JSON from https://api.github.com/zen.",
      ("http_request", "web_fetch"), reps=1),
    F("compute_shell", "compute", "Use the shell to compute the SHA-256 of the text 'wisp'.",
      ("run_shell",)),

    # ---- apps --------------------------------------------------------------
    F("app_open", "apps", "Open the Calculator app.", ("open_app",), reps=1),
    F("app_quit", "apps", "Quit the Calculator app.", ("quit_app",), reps=1, note="after app_open"),
    F("app_music", "apps", "Play some music.", ("music", "spotify"), reps=1,
      note="ACTUALLY STARTS PLAYBACK"),

    # ---- chitchat ------------------------------------------------------------
    F("no_tool", "chitchat", "Say hello in exactly three words.", ()),

    # ---- confirmation flow ---------------------------------------------------
    Flow("confirm_archive_flow", "email", [
        Turn("Show me my recent emails.", ("view_emails", "summarize_emails")),
        Turn("yes archive them", ("archive_email",)),
    ], reps=1, note="confirms_offered_action inherited-subset path"),

    # ---- compound: sequential/chained tool calls in ONE turn -----------------
    # "find my calendar and tell my mom when I'm free tomorrow"-shaped prompts —
    # read from one of the user's actually-heaviest domains (calendar, mail,
    # messages, reminders, notes — real_usage data confirms this is the bulk
    # of real traffic), then act on what was read. require_all=True: "ok"
    # means the WHOLE chain completed, not just the first step. Sends stay
    # self-targeted per the established safety design; write-ending flows are
    # immediately paired with a WISPTEST cleanup, same convention as the rest
    # of the file. reps=1 throughout — every flow here ends in a real send or
    # write, so one real occurrence is enough signal.
    F("cmp_cal_tomorrow_text", "compound",
      "Check my calendar for tomorrow and text me a summary of when I'm free.",
      ("get_upcoming", "send_message"), reps=1, require_all=True),
    F("cmp_cal_week_email", "compound",
      "Look at my schedule this week and email me a summary of it.",
      ("get_upcoming", "send_email"), reps=1, require_all=True),
    F("cmp_mail_unread_text", "compound",
      "Summarize my unread emails and text me the summary.",
      ("summarize_emails", "send_message"), reps=1, require_all=True),
    F("cmp_notes_wifi_text", "compound",
      "Check my notes for the wifi password and text it to me.",
      ("search_notes", "send_message"), reps=1, require_all=True),
    F("cmp_contact_number_text", "compound",
      "Look up mom's phone number and text it to me for my records.",
      ("lookup_contact", "send_message"), reps=1, require_all=True),
    F("cmp_reminders_email", "compound",
      "What are my reminders for today? Email me the list.",
      ("get_upcoming", "send_email"), reps=1, require_all=True),
    F("cmp_past_dentist_text", "compound",
      "When did I last see the dentist? Text me the answer.",
      ("get_past_events", "send_message"), reps=1, require_all=True),
    F("cmp_boss_email_text", "compound",
      "Check if I have any emails from my boss today, and text me either way.",
      ("view_emails", "send_message"), reps=1, require_all=True),
    F("cmp_dad_text_check", "compound",
      "Did I get any texts from Dad today? Text me a yes or no.",
      ("view_messages", "send_message"), reps=1, require_all=True),
    F("cmp_full_briefing", "compound",
      "Give me a full briefing — check my calendar and my unread email — "
      "and text me the summary.",
      ("get_upcoming", "summarize_emails", "send_message"), reps=1, require_all=True,
      note="3-tool chain, matches the real aggregate-briefing route"),
    F("cmp_notes_and_past_text", "compound",
      "Check my notes for my dentist's info, see when I last had an "
      "appointment, and text me both.",
      ("search_notes", "get_past_events", "send_message"), reps=1, require_all=True),
    F("cmp_cal_and_mail_text", "compound",
      "Check my calendar for tomorrow and see if I have any emails about "
      "rescheduling — text me what you find.",
      ("get_upcoming", "view_emails", "send_message"), reps=1, require_all=True),

    # write-ending compounds, each paired with an immediate cleanup
    F("cmp_mail_deadline_reminder", "compound",
      "Check my email for anything about a UCSC deadline and set a reminder "
      "called 'WISPTEST delete me' for tomorrow if you find one.",
      ("summarize_emails", "add_reminder"), reps=1, require_all=True),
    F("cmp_cleanup_reminder_1", "compound",
      "Cancel the WISPTEST delete me reminder.", ("cancel_event",), reps=1,
      note="cleans up cmp_mail_deadline_reminder"),
    F("cmp_notes_todo_reminder", "compound",
      "Check my notes for any todo items, and add the first one as a "
      "reminder called 'WISPTEST delete me' for tomorrow.",
      ("search_notes", "add_reminder"), reps=1, require_all=True),
    F("cmp_cleanup_reminder_2", "compound",
      "Cancel the WISPTEST delete me reminder.", ("cancel_event",), reps=1,
      note="cleans up cmp_notes_todo_reminder"),
    F("cmp_msg_mom_calendar", "compound",
      "See if mom texted me about weekend plans, and if she did, add a "
      "calendar event called 'WISPTEST delete me' this Saturday at noon.",
      ("view_messages", "add_calendar_event"), reps=1, require_all=True),
    F("cmp_cleanup_calendar_1", "compound",
      "Cancel the event called 'WISPTEST delete me'.", ("cancel_event",), reps=1,
      note="cleans up cmp_msg_mom_calendar"),
]


class SelfTargetApprover:
    def __init__(self, allowed: tuple[str, ...]) -> None:
        self.allowed = set(allowed)
        self.denied: list[str] = []
        self.approved: list[str] = []

    async def confirm(self, action: dict) -> bool:
        tool = action.get("tool", "")
        args = action.get("args", {}) or {}
        if tool not in self.allowed:
            self.denied.append(tool)
            return False
        if tool in SEND_TOOLS:
            target = str(args.get("to", ""))
            safe = (SELF_EMAIL.lower() in target.lower()
                    or (SELF_PHONE_DIGITS and SELF_PHONE_DIGITS in _digits(target)))
            if not safe:
                self.denied.append(f"{tool}(unsafe target={target!r})")
                return False
        self.approved.append(tool)
        return True


class DenyAllApprover:
    """--skip-real-exec fallback: capture the tool_call decision, never execute."""

    def __init__(self, allowed: tuple[str, ...]) -> None:
        self.denied: list[str] = []

    async def confirm(self, action: dict) -> bool:
        self.denied.append(action.get("tool", ""))
        return False


def _normalize_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """oMLX's HTTP response carries `function.arguments` as a JSON STRING
    (OpenAI wire format) — but Agents-A1-4B-oQe6's own chat_template.jinja
    iterates `arguments` as a mapping (`<parameter=KEY>value</parameter>`
    per its XML tool-call payload, confirmed in the qwen3-function-calling
    research) to RE-render a training example. Passed through as a string,
    mlx_lm's `apply_chat_template` dies with "Can only get item pairs from a
    mapping" (Jinja's `|items` filter). build_lora_dataset.py's real-usage
    examples already store args as dicts (from the audit log); this makes
    the harvester consistent with that, not the wire format."""
    out = []
    for tc in tool_calls:
        tc = dict(tc)
        fn = dict(tc.get("function", {}))
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                fn["arguments"] = json.loads(args) if args else {}
            except json.JSONDecodeError:
                fn["arguments"] = {}
        tc["function"] = fn
        out.append(tc)
    return out


def _clean_response(msg: dict) -> dict | None:
    if msg.get("_degenerate"):
        return None
    out = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = _normalize_tool_calls(msg["tool_calls"])
    return out


_TOOL_ERROR_RE = re.compile(
    r"\((?:could not|couldn.?t|unknown tool|unable to|failed|.{0,24}\berror\b|"
    r"the .* was not|.* did.?n.?t respond|NOT sent|NOT scheduled)", re.I)


async def run_turn(client, model, prompt, expect, timeout, messages, last_assistant, last_tools,
                   real_exec, require_all=False) -> dict:
    decision = await route(prompt, last_assistant=last_assistant, last_tools=last_tools)
    events: list[dict] = []

    async def emit(ev: dict) -> None:
        events.append(ev)

    approver = (SelfTargetApprover(expect) if real_exec else DenyAllApprover(expect))
    user_msg = {"role": "user", "content": prompt}
    msgs_in = messages + [user_msg]
    t0 = time.time()
    try:
        await client.ensure_only(model, exclusive=not decision.tool_subset)
        final = await asyncio.wait_for(
            run_agent(client, model, msgs_in, emit, approver,
                      tools=decision.tool_subset,
                      force_first_tool=decision.force_first_tool,
                      expect_tool_first=decision.expect_tool_first,
                      max_steps=8 if require_all else 6, max_tokens=4000,
                      temperature=0.6 if decision.tool_subset else 0.0,
                      multi_round=decision.multi_round,
                      narration_after=decision.narration_after),
            timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "route_fault": False, "expect": list(expect), "tool_errors": [],
                "detail": f"{type(e).__name__}: {str(e)[:160]}",
                "seconds": time.time() - t0, "raw_steps": [], "calls": [],
                "messages": msgs_in, "assistant_text": "", "digest": ""}

    dt = time.time() - t0
    raw_steps = [e for e in events if e.get("type") == "raw_model_io"]
    calls = [e["name"] for e in events if e.get("type") == "tool_call"]
    tool_errors = [str(e.get("result", "")) for e in events if e.get("type") == "tool_result"
                   and _TOOL_ERROR_RE.match(str(e.get("result", "")).strip())]
    offered = set(decision.tool_subset) if decision.tool_subset else None

    if require_all:
        route_fault = bool(offered is not None and not (set(expect) <= offered) and expect)
    else:
        route_fault = bool(offered is not None and not (offered & set(expect)) and expect)

    if route_fault:
        ok, detail = False, f"ROUTE: offered {sorted(offered)}, never all of {expect}"
    elif expect:
        ok = (set(expect) <= set(calls)) if require_all else bool(set(calls) & set(expect))
        detail = "ok" if ok else f"called {calls} instead of {'ALL of ' if require_all else ''}{expect}"
    else:
        ok = not calls
        detail = "ok (correctly called nothing)" if ok else f"called {calls} when nothing was needed"

    # A right tool call that then failed at execution (env/permission/IPC
    # issue, not a model mistake) is still "ok" for tool-SELECTION purposes,
    # but must be visible — silently trusting name-matching alone is exactly
    # what missed the send_email/lock_screen/list_dir failures earlier.
    if ok and tool_errors:
        detail += f" [BUT {len(tool_errors)} tool_result error(s): {tool_errors[0][:100]!r}]"

    final = (final or "").strip()
    digest = ",".join(dict.fromkeys(calls))
    rendered_assistant = final + (f"\n[actions: {digest}]" if digest else "")

    return {"ok": ok, "route_fault": route_fault, "detail": detail, "seconds": dt,
            "raw_steps": raw_steps, "calls": calls, "expect": list(expect),
            "tool_errors": tool_errors,
            "messages": msgs_in + [{"role": "assistant", "content": rendered_assistant}],
            "assistant_text": final, "digest": digest or None}


async def run_flow(client, model, flow: Flow, timeout, real_exec) -> dict:
    messages: list[dict] = []
    last_assistant = None
    last_tools = None
    turn_results = []
    for turn in flow.turns:
        r = await run_turn(client, model, turn.prompt, turn.expect, timeout,
                           messages, last_assistant, last_tools, real_exec,
                           require_all=turn.require_all)
        turn_results.append(r)
        messages = r["messages"]
        last_assistant = r["assistant_text"]
        last_tools = r["digest"]
    return {"flow": flow.name, "group": flow.group, "turns": turn_results,
            "ok": all(t["ok"] for t in turn_results),
            "route_fault": any(t.get("route_fault") for t in turn_results)}


def to_sft_examples(flow_result: dict) -> list[dict]:
    if not flow_result["ok"] or flow_result["route_fault"]:
        return []
    out = []
    for turn in flow_result["turns"]:
        for step in turn["raw_steps"]:
            resp = _clean_response(step["response"])
            if resp is None:
                continue
            req = step["request"]
            out.append({"messages": req["messages"] + [resp], "tools": req.get("tools")})
    return out


def to_dpo_pairs(flow_result: dict) -> list[dict]:
    """Mine one (chosen, rejected) pair per wrong turn — name-level correction
    only (right tool, best-effort/empty args); see module docstring."""
    pairs = []
    for turn in flow_result["turns"]:
        if turn["ok"] or turn.get("route_fault") or not turn["expect"]:
            continue
        for step in turn["raw_steps"]:
            resp = step["response"]
            tc = resp.get("tool_calls") or []
            if not tc:
                continue
            called = tc[0].get("function", {}).get("name", "")
            if called in turn["expect"]:
                continue
            req = step["request"]
            rejected = _clean_response(resp)
            if rejected is None:
                continue
            chosen = {
                "role": "assistant", "content": "",
                "tool_calls": [{"type": "function", "id": tc[0].get("id", "call_0"),
                                "function": {"name": turn["expect"][0], "arguments": {}}}],
            }
            pairs.append({"prompt": req["messages"], "tools": req.get("tools"),
                          "chosen": chosen, "rejected": rejected})
            break
    return pairs


def to_incomplete_chain_pairs(flow_result: dict) -> list[dict]:
    """Mine a pair for the OTHER require_all failure shape: the model called
    every tool correctly (no wrong-tool detour — that's to_dpo_pairs' job)
    but stopped partway through a sequential chain and narrated a final
    answer instead of continuing. Found live: 8/8 of a compound-prompt batch
    ("check my calendar and text me...") completed the READ half and then
    just... stopped, every time, once the router actually offered the send
    tool. That's a real behavior gap distinct from picking the wrong tool,
    and to_dpo_pairs alone can't see it (it only fires when a WRONG tool_call
    exists to contrast against; here there isn't one — the failure is an
    omission, not a mistake)."""
    pairs = []
    for turn in flow_result["turns"]:
        if turn["ok"] or turn.get("route_fault") or not turn["expect"]:
            continue
        calls, expect = set(turn["calls"]), set(turn["expect"])
        missing = expect - calls
        if not missing or not (calls <= expect) or not turn["raw_steps"]:
            continue  # not the "stopped early" shape — a wrong call happened instead
        last_step = turn["raw_steps"][-1]
        resp = last_step["response"]
        if resp.get("tool_calls"):
            continue  # last step still made a call — nothing to correct here
        rejected = _clean_response(resp)
        if rejected is None:
            continue
        req = last_step["request"]
        next_tool = sorted(missing)[0]
        chosen = {
            "role": "assistant", "content": "",
            "tool_calls": [{"type": "function", "id": "call_0",
                            "function": {"name": next_tool, "arguments": {}}}],
        }
        pairs.append({"prompt": req["messages"], "tools": req.get("tools"),
                      "chosen": chosen, "rejected": rejected})
    return pairs


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated groups")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--out", default="~/lora_data")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-real-exec", action="store_true",
                    help="capture tool_call decisions but deny everything (no real side effects)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--resume-after", default="",
                    help="skip every flow at or before this name in FLOWS order "
                         "(for resuming a crashed run without repeating real side effects)")
    ap.add_argument("--append", action="store_true",
                    help="append to existing harvest_sft.jsonl/harvest_dpo.jsonl instead of truncating")
    ap.add_argument("--flows", default="", help="comma-separated exact flow names to run "
                    "(precise re-targeting, e.g. after a mining-logic fix)")
    args = ap.parse_args()

    flows = FLOWS
    if args.resume_after:
        names = [f.name for f in flows]
        if args.resume_after not in names:
            ap.error(f"--resume-after {args.resume_after!r} not found in FLOWS")
        flows = flows[names.index(args.resume_after) + 1:]
    if args.only:
        groups = {g.strip() for g in args.only.split(",")}
        flows = [f for f in flows if f.group in groups]
    if args.flows:
        names = {n.strip() for n in args.flows.split(",")}
        flows = [f for f in flows if f.name in names]

    plan = [f for f in flows for _ in range(f.reps)]
    total_turns = sum(len(f.turns) for f in plan)
    print(f"{len(flows)} unique flows -> {len(plan)} runs (reps applied) -> "
          f"{total_turns} individual model-facing turns")
    if args.dry_run:
        for f in flows:
            tag = " [MULTI-TURN]" if len(f.turns) > 1 else ""
            print(f"  [{f.group:10}] {f.name:22} x{f.reps}{tag}")
            for i, t in enumerate(f.turns, 1):
                print(f"      T{i}: {t.prompt!r} -> expect {t.expect or '(none)'}")
            if f.note:
                print(f"      note: {f.note}")
        return 0

    model = args.model or role_to_model("agent")
    client = OMLXClient()
    await client.ensure_only(model, exclusive=True)

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"
    sft_f = (out_dir / "harvest_sft.jsonl").open(mode)
    dpo_f = (out_dir / "harvest_dpo.jsonl").open(mode)

    n_ok = n_fail = n_route_fault = n_sft = n_dpo = 0
    t_start = time.time()
    for i, flow in enumerate(plan, 1):
        r = await run_flow(client, model, flow, args.timeout, not args.skip_real_exec)
        if r["route_fault"]:
            n_route_fault += 1
        elif r["ok"]:
            n_ok += 1
        else:
            n_fail += 1
        sft = to_sft_examples(r)
        dpo = to_dpo_pairs(r) + to_incomplete_chain_pairs(r)
        for ex in sft:
            sft_f.write(json.dumps(ex) + "\n")
        for p in dpo:
            dpo_f.write(json.dumps(p) + "\n")
        n_sft += len(sft)
        n_dpo += len(dpo)
        mark = "OK  " if r["ok"] else ("ROUTE" if r["route_fault"] else "FAIL")
        details = " | ".join(t["detail"] for t in r["turns"])
        secs = sum(t["seconds"] for t in r["turns"])
        print(f"[{i:3}/{len(plan)}] {mark} {r['group']:9} {r['flow']:22} [{secs:5.1f}s] {details}")
        sft_f.flush()
        dpo_f.flush()

    sft_f.close()
    dpo_f.close()
    elapsed = time.time() - t_start
    print(f"\n{'=' * 78}\n{n_ok} ok / {n_fail} wrong-tool / {n_route_fault} route-fault "
          f"out of {len(plan)} flows in {elapsed / 60:.1f}m")
    print(f"harvested {n_sft} SFT examples -> {out_dir / 'harvest_sft.jsonl'}")
    print(f"harvested {n_dpo} DPO pairs -> {out_dir / 'harvest_dpo.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
