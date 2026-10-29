"""One-line tool descriptions for the opt-in terse tool-schema mode.

Tool schemas are the largest single consumer of the local model's context
window, and most of that is description prose. Once the model is fine-tuned to
know the tools, it only needs a short reminder of what each one is for, so
`registry.tool_schemas` swaps these in when WISP_TERSE_TOOL_SCHEMAS=1.

Each line must still separate the tool from its nearest rival (raw vs summary,
past vs upcoming, search vs read) and keep the constraint a model cannot infer
from the name (exact ID required, only on explicit request). Never claim a
tool is "confirm-gated": full-access mode and standing grants can allow it.

These lines are NEVER indexed: semantic.py and reranker.py embed the full
`description` / `retrieval_description`, so retrieval is identical in both modes.
Tools without an entry (skills, MCP, use_skill, user-created tools) keep their
full schema, parameter prose included. Keep each line at 90 characters or fewer.
"""
from __future__ import annotations

MAX_LEN = 90

SHORT: dict[str, str] = {
    # calendar and reminders
    "add_calendar_event": "Create a real Calendar event (not a reminder); needs title and start time.",
    "add_reminder": "Create a Wisp reminder that notifies at a set time; not a calendar event or alarm.",
    "cancel_event": "Delete an upcoming event or reminder by title.",
    "clear_past_reminders": "Bulk-delete past-due reminders and events; never touches upcoming.",
    "clear_reminders": "Bulk-delete active reminders in one scope; scope=all only if the user asks for all.",
    "complete_reminder": "Mark a reminder or task done by title; does not delete it.",
    "find_free_time": "Find open gaps in the schedule for a meeting or call.",
    "get_past_events": "Read calendar events from the PAST (up to a year); counterpart to get_upcoming.",
    "get_upcoming": "Read upcoming events, due dates and reminders; use for any schedule question.",
    "get_my_day_plan": "Read the prioritized plan for today or tomorrow: events, reminders, overdue, to-dos.",
    "plan_meeting": "Read-only private Calendar block proposal; never infer an event's end yourself.",
    "commit_private_meeting": "Create the exact block from plan_meeting, only after explicit user confirmation.",
    "search_reminders": "The user's to-dos, tasks and reminders (open, overdue); empty query lists all.",
    "update_event": "Move an existing Calendar event to a new time, keeping its details (confirmed).",
    "update_reminder": "Reschedule or rename an existing reminder, matched by title.",
    "contact_dates": "List upcoming birthdays from Contacts, soonest first.",
    # timers and clock
    "set_timer": "Start a countdown timer, clock alarm or sleep timer (not a dated reminder).",
    "manage_timers": "List or cancel running timers and alarms, or run the stopwatch.",
    "world_time": "Current date and time here or in another city; also time differences.",
    # notes, knowledge, memory
    "append_note": "Add lines to the end of an EXISTING note, found by title.",
    "create_note": "Create a NEW note in Notes.app; not write_file.",
    "search_notes": "Raw verbatim text of Notes.app notes (not a summary); search by keyword or date.",
    "read_knowledge": "Read explicitly saved knowledge snippets (not Notes.app); exact ID optional.",
    "save_knowledge": "Save explicitly user-selected text as a knowledge record; needs explicit instruction.",
    "edit_knowledge": "Replace a saved knowledge record; exact ID and current revision required.",
    "delete_knowledge": "Delete one saved knowledge record; exact ID and revision, explicit request only.",
    "remember": "Save a fact verbatim, only when the user explicitly asks to remember it.",
    "recall": "Retrieve saved memories and past conversation passages; empty query lists facts.",
    "forget": "Delete saved memories matching all query words; no fuzzy matching.",
    "search_conversations": "Search what the USER said in past conversations by keyword; dated.",
    # learned patterns
    "describe_me": "Describe the routines and habits Wisp has learned about the user; read-only.",
    "my_patterns": "Search learned routines for one question, e.g. when the user usually works out.",
    "predict_my_day": "Guess today or tomorrow from learned habits only; not the calendar or a plan.",
    "correct_pattern": "Confirm or reject one learned pattern when the user says it is right or wrong.",
    # email
    "view_emails": "RAW verbatim email text and Message-IDs; for exact details, not overviews.",
    "summarize_emails": "Per-sender overview of Mail headers only; never reads bodies.",
    "triage_inbox": "Rank recent inbox senders by importance from headers only.",
    "scan_subscriptions": "List newsletter and marketing senders ranked by volume.",
    "email_triage": "Archive, flag or mark read/unread one email by Message-ID; not a delete.",
    "draft_email": "Open a NEW Mail draft without sending; user reviews and sends.",
    "send_email": "Send a NEW email now; use reply_to_email for replies in a thread.",
    "reply_to_email": "Reply in-thread by Message-ID; reply_all only if the user asks for it.",
    "forward_email": "Forward an email by Message-ID to someone.",
    # messages and contacts
    "view_messages": "RAW verbatim iMessage/SMS text; for exact details, not summaries.",
    "summarize_messages": "Digest of IMPORTANT texts only; for who texted / latest texts use view_messages.",
    "draft_message": "Prepare an editable text draft in Wisp without sending it.",
    "send_message": "Send an iMessage/SMS now; recipient can be a contact name.",
    "schedule_send": "Send a NEW email or text LATER; `when` in the user's own words.",
    "list_scheduled_sends": "Only messages scheduled to send later (not to-dos); queue by exact ID.",
    "cancel_scheduled_send": "Cancel a still-pending scheduled send; exact ID from list_scheduled_sends.",
    "lookup_contact": "Get ONE contact's phone or email by name; use before sending to a name.",
    "list_contacts": "List saved contact names, optionally filtered; no numbers.",
    "manage_contacts": "Create a contact or add a phone or email to an existing one.",
    "get_recent_activity": "What's new across texts, email, reminders and notes, merged newest-first.",
    # files
    "find_files": "Spotlight search for files by name or content; use instead of shell find.",
    "list_dir": "List a folder's contents; use ~ paths; recursive for nested trees.",
    "read_file": "Read a file's text, including PDF/docx/xlsx/pptx; not scanned images.",
    "write_file": "Create or overwrite a plain text file.",
    "move_path": "Move or rename a file or folder, one item per call; creates folders as needed.",
    "organize_files": "Preview, then move glob-matched files into folders; by name only, not content.",
    "write_document": "Create a Word .docx with title, paragraphs and optional bullets.",
    "spreadsheet_ops": "Create an Excel .xlsx with a header row and data rows.",
    # web and places
    "web_search": "Search the public web; returns titles, URLs and snippets.",
    "web_fetch": "GET one URL's text; for crypto, news and public pages (not stocks or weather).",
    "get_stock_price": "Stock/ETF price or history over a period; not crypto.",
    "get_weather": "Weather and 3-day forecast for a named place; ask if location unknown.",
    "wikipedia_summary": "Short Wikipedia summary of a person, place or thing.",
    "find_place": "Find nearby places around a named location; ask if it is unknown.",
    "travel_time": "Driving distance and free-flow time between two places; no live traffic.",
    "route_to_arrive": "When to leave to arrive by a deadline (traffic ETA); never invent an origin.",
    "search_browser_history": "Search recent Safari/Chrome history (site and title only) by keyword or date.",
    # system and apps
    "clipboard_read": "Read the clipboard text.",
    "clipboard_write": "Replace the clipboard text.",
    "screen_capture": "Take a screenshot of the screen, window or a selection to a file.",
    "toggle_setting": "Turn a macOS setting on or off (wifi, bluetooth, dark_mode...); not volume.",
    "system_status": "Mac battery, disk, memory, CPU, network or Bluetooth status.",
    "list_running_apps": "List running apps and which is frontmost.",
    "open_app": "Open or focus a macOS app, optionally with a file or URL.",
    "quit_app": "Quit a macOS app by name.",
    "window_control": "Close, minimize, zoom, fullscreen or tile the frontmost window.",
    "text_to_speech": "Speak text aloud, or save it as an audio file.",
    "calculate": "Exact arithmetic and percentages; use for any calculation.",
    "convert_units": "Convert a value between units (length, weight, temperature, data...).",
    # admin and escape hatches
    "wisp_capabilities": "List Wisp's real capabilities from the full tool registry.",
    "wisp_mcp": "List connected MCP servers and their tool counts.",
    "wisp_skills": "List installed skills and whether each is enabled.",
    "wisp_status": "Wisp's own state: loaded models and memory, or last sync times.",
    "run_applescript": "Raw AppleScript for one-off app automation; only when no dedicated tool fits.",
    "run_shell": "LAST RESORT: run a shell command; use a dedicated tool when one exists.",
    "create_tool": "Build a reusable new tool when nothing covers the capability; not one-offs.",
    # internal-only (reached by name from deterministic routes, never retrieved)
    "collect_task_evidence": "Internal: bounded cached evidence for priorities across calendar, mail, notes.",
    "daily_brief": "Briefing of today's calendar, reminders, mail, messages and weather.",
    "search_coverage": "Internal: how far back Wisp can search each source.",
}


# Parameters whose prose survives terse mode: a safety rule or a format the
# model cannot infer from the name and type alone. Kept short on purpose —
# every entry costs tokens on every turn that offers the tool.
KEEP_PARAMETER_PROSE: dict[str, frozenset[str]] = {
    "send_email": frozenset({"confirmed_self_send"}),
    "send_message": frozenset({"confirmed_self_send"}),
    "get_upcoming": frozenset({"include_holidays"}),
    "get_past_events": frozenset({"include_holidays"}),
    "plan_meeting": frozenset({"when_iso", "event_period", "offset_minutes"}),
    "route_to_arrive": frozenset({"origin"}),
    "find_place": frozenset({"near"}),
    "add_reminder": frozenset({"when_iso"}),
    "add_calendar_event": frozenset({"when_iso"}),
    "update_reminder": frozenset({"when_iso"}),
    "schedule_send": frozenset({"when"}),
}


def short_description(name: str, full: str) -> str:
    """The terse line for `name`, or `full` when the tool has no entry."""
    return SHORT.get(name, full)
