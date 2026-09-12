"""Data-only adversarial corpus for router evaluation.

Importing this module does not call the router, a model, or a tool. ``plan``
cases must stay intercepted in any future model-backed run.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptCase:
    id: str
    category: str
    prompt: str
    expected: str
    required: tuple[str, ...] = ()
    one_of: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    required_args: tuple[tuple[str, str, object], ...] = ()
    mode: str = "read"  # read | plan
    clarify_channel: bool | None = None
    clarify_target: bool | None = None
    last_assistant: str | None = None
    last_tools: str | None = None


def P(case_id: str, category: str, prompt: str, expected: str, **kw) -> PromptCase:
    return PromptCase(case_id, category, prompt, expected, **kw)


CASES: tuple[PromptCase, ...] = (
    # Compound and multi-tool requests.
    P("compound_action_items", "compound",
      "Summarize my unread emails, compare them with today's messages, and text me only the items that need action.",
      "Read both channels, synthesize actionable items, then text the result.",
      required=("summarize_emails", "summarize_messages", "draft_message"),
      forbidden=("send_message",), mode="plan"),
    P("compound_calendar_dad", "compound",
      "Check my calendar for next month and write my dad a message listing every weekend I'm free.",
      "Read the calendar, resolve Dad, and prepare a Messages delivery.",
      required=("get_upcoming", "find_free_time", "lookup_contact", "send_message"), mode="plan"),
    P("compound_wifi_email", "compound",
      "Find the Wi-Fi password in my notes and email it to me for my records.",
      "Search Notes, then prepare a self-addressed draft under the self-send safety policy.",
      required=("search_notes", "draft_email"), forbidden=("send_email",), mode="plan"),
    P("compound_launch_calendar", "compound",
      "Look up the next SpaceX launch, make sure I'm free, and add it to my calendar if I am.",
      "Fetch the current launch time, check availability, and conditionally create an event.",
      required=("web_fetch", "find_free_time", "add_calendar_event"), mode="plan"),
    P("compound_pdf_email", "compound",
      "Find the largest PDF in Downloads, summarize it, and draft an email about it to Sarah.",
      "Find and read the file, resolve Sarah, then draft without sending.",
      required=("find_files", "read_file", "lookup_contact", "draft_email"), mode="plan"),
    P("compound_all_sources", "compound",
      "Tell me what I missed across email, messages, notes, reminders, and calendar since yesterday morning.",
      "Read every named personal-data source in the requested window.",
      required=("summarize_emails", "summarize_messages", "search_notes", "get_upcoming")),
    P("compound_weather_reminder", "compound",
      "Check whether it will rain during my next outdoor calendar event and remind me to bring an umbrella if necessary.",
      "Read the event, check weather, and create a reminder only if rain is forecast.",
      required=("get_upcoming", "get_weather", "add_reminder"), mode="plan"),
    P("compound_message_reply", "compound",
      "Find the last message from Mom, check whether I replied, and draft a response if I didn't.",
      "Inspect the conversation and conditionally draft without sending.",
      required=("draft_message",), one_of=("search_conversations", "view_messages"), mode="plan"),

    # Read/write and conditional boundaries.
    P("readonly_schedule", "read_write",
      "Show me tomorrow's schedule, but don't add or change anything.",
      "Read only.", required=("get_upcoming",),
      forbidden=("add_calendar_event", "add_reminder", "cancel_event", "update_event")),
    P("draft_calendar_email", "read_write",
      "Draft an email summarizing my calendar, but do not send it.",
      "Read calendar and draft; never send.", required=("get_upcoming", "draft_email"),
      forbidden=("send_email",), mode="plan"),
    P("conditional_cancel_lunch", "read_write",
      "Cancel lunch tomorrow, unless it's the meeting with Alex.",
      "Inspect the event before conditionally cancelling.",
      required=("get_upcoming", "cancel_event"), mode="plan"),
    P("conditional_move_report", "read_write",
      "Move the report into the project folder after confirming that another copy already exists there.",
      "Inspect source and destination before moving.",
      required=("find_files", "move_path"), mode="plan"),
    P("conditional_mark_newsletters", "read_write",
      "Mark newsletters as read, but leave anything from a real person untouched.",
      "Identify subscriptions before marking only matching email.",
      required=("scan_subscriptions", "mark_email_read"), mode="plan"),
    P("conditional_low_power", "read_write",
      "If my battery is below 20%, turn on Low Power Mode; otherwise just tell me the percentage.",
      "Read battery state, then conditionally change the setting.",
      required=("get_battery_status", "toggle_setting"),
      required_args=(("toggle_setting", "setting", "low_power_mode"),
                     ("toggle_setting", "on", True)), mode="plan"),
    P("compound_reminder_email", "read_write",
      "Remind me to open the report tomorrow at 9, then email me once the reminder is created.",
      "Create the reminder and only then prepare a self-addressed confirmation draft.",
      required=("add_reminder", "draft_email"), forbidden=("send_email",), mode="plan"),

    # Ambiguous channels and recipients.
    P("channel_tell_mom", "ambiguity", "Tell Mom about my schedule this weekend.",
      "Ask whether to text or email.", required=("get_upcoming", "lookup_contact"),
      one_of=("send_message", "send_email"), mode="plan", clarify_channel=True),
    P("channel_boss_update", "ambiguity", "Send the latest update to my boss tonight.",
      "Ask which update and delivery channel.",
      one_of=("send_message", "send_email", "schedule_send"), mode="plan", clarify_channel=True),
    P("channel_forward_alex", "ambiguity", "Forward it to Alex.",
      "Use context or ask what 'it' and which channel mean.",
      one_of=("forward_email", "send_message", "send_email"), mode="plan", clarify_channel=True),
    P("channel_everyone", "ambiguity", "Let everyone know I'll be ten minutes late.",
      "Ask for the group and channel.", one_of=("send_message", "send_email"),
      mode="plan", clarify_channel=True),
    P("channel_send_me_summary", "ambiguity", "Send me a summary of what I missed.",
      "Clarify the source scope and delivery channel.",
      one_of=("send_message", "send_email"), mode="plan", clarify_channel=True),
    P("channel_message_jordan", "ambiguity", "Message Jordan about tomorrow.",
      "Use Messages but ask what should be said.", required=("lookup_contact", "send_message"),
      mode="plan", clarify_channel=False),
    P("channel_email_note_text", "ambiguity", "Email me the text from that note.",
      "Resolve the note, then prepare a self-addressed draft under the self-send policy.",
      required=("search_notes", "draft_email"), forbidden=("send_email",),
      mode="plan", clarify_channel=False),

    # Overloaded words and false-positive traps.
    P("overload_error_message", "overload",
      "Explain this error message: connection reset by peer.",
      "Treat message as developer prose, not SMS.", forbidden=("view_messages", "summarize_messages")),
    P("overload_commit_message", "overload", "Write a better commit message for these changes.",
      "Treat message as coding context, not SMS.", forbidden=("send_message", "view_messages")),
    P("overload_hash_text", "overload",
      "Use the shell to calculate the SHA-256 hash of the text 'wisp'.",
      "Use shell computation; text does not mean Messages.", required=("run_shell",),
      forbidden=("view_messages", "summarize_messages")),
    P("overload_email_regex", "overload", "Write a regex that validates an email address.",
      "Route as coding, not inbox access.", forbidden=("view_emails", "summarize_emails", "send_email")),
    P("overload_email_body", "overload",
      "Summarize the text of this email without opening my inbox.",
      "Summarize supplied content only and respect the inbox prohibition.",
      forbidden=("view_emails", "summarize_emails")),
    P("overload_plain_text_notes", "overload", "Create a plain-text file containing my notes.",
      "Read Notes and create a file, not another Notes.app note.",
      required=("search_notes", "write_file"), forbidden=("create_note",), mode="plan"),
    P("overload_code_message", "overload", "What message is this code trying to communicate?",
      "Explain code, not Messages.", forbidden=("view_messages", "summarize_messages")),
    P("overload_physical_code", "overload", "Make a note that the door code is 4821.",
      "Persist the fact or create a note; do not route as programming.",
      one_of=("remember", "create_note"), mode="plan"),
    P("overload_screen", "overload", "What's on my screen right now?",
      "Disclose the lack of vision; never invent screen contents.", forbidden=("get_upcoming",)),
    P("overload_tv", "overload", "What's on TV tonight?",
      "Treat as entertainment information, not calendar/screen state.",
      forbidden=("get_upcoming", "screen_capture")),
    P("overload_whats_next", "overload", "What's next?",
      "Use conversation context or ask for clarification.", forbidden=("get_upcoming",)),

    # Memory and reminder boundaries.
    P("memory_preference", "memory", "Remember that I prefer morning meetings.",
      "Persist a durable preference.", required=("remember",), mode="plan"),
    P("reminder_call_dentist", "memory", "Remember to call the dentist tomorrow.",
      "Create a reminder, not durable memory.", required=("add_reminder",),
      forbidden=("remember",), mode="plan"),
    P("memory_sister_diet", "memory", "Don't forget that my sister is vegetarian.",
      "Persist a durable fact.", required=("remember",), mode="plan"),
    P("reminder_email_sister", "memory", "Don't forget to email my sister on Friday.",
      "Create a reminder; do not immediately email or save memory.",
      required=("add_reminder",), forbidden=("remember", "send_email"), mode="plan"),
    P("memory_told_assistant", "memory", "What did I tell you about my car?",
      "Query assistant memory.", required=("recall",)),
    P("memory_told_dan", "memory", "What did I tell Dan about my car?",
      "Search conversations with Dan, not assistant memory.",
      one_of=("search_conversations", "summarize_messages"),
      forbidden=("recall", "get_upcoming"), clarify_channel=False),
    P("memory_calendar_boundary", "memory", "What do you know about my calendar?",
      "Read live calendar data, not memory.", required=("get_upcoming",), forbidden=("recall",)),
    P("memory_people", "memory", "Who are the important people in my life?",
      "Read contacts and relationship memory without writing facts.",
      required=("list_contacts", "recall"), forbidden=("remember",)),
    P("memory_selective_forget", "memory",
      "Forget what I told you about the old apartment, but keep everything else.",
      "Delete only matching durable memory.", required=("forget",),
      forbidden=("clear_memory",), mode="plan"),

    # Calendar and time-window traps.
    P("calendar_past_future", "calendar",
      "What did I have yesterday, and what's my next appointment?",
      "Read both past and upcoming windows.", required=("get_past_events", "get_upcoming")),
    P("calendar_free_before_sunset", "calendar",
      "Find a free 90-minute block after my last meeting but before sunset on Thursday.",
      "Combine availability with sunset data.", required=("find_free_time",),
      one_of=("astronomy", "get_weather")),
    P("calendar_assignments", "calendar", "What assignments are due before my next class?",
      "Read deadlines and the next class.", required=("get_upcoming",)),
    P("calendar_conflict_reminder", "calendar",
      "Do I have anything tomorrow that conflicts with the reminder I just created?",
      "Read events/reminders without creating another.", required=("get_upcoming",),
      forbidden=("add_reminder",)),
    P("calendar_cancel_scheduled_text", "calendar", "Cancel that text scheduled for tonight.",
      "Operate on scheduled sends, not calendar events.", required=("cancel_scheduled_send",),
      forbidden=("cancel_event",), mode="plan"),
    P("calendar_complete_reminder", "calendar",
      "Complete the laundry reminder, not the calendar event with the same name.",
      "Complete only the reminder.", required=("complete_reminder",),
      required_args=(("complete_reminder", "title", "laundry"),),
      forbidden=("cancel_event", "update_event"), mode="plan"),
    P("calendar_business_days", "calendar",
      "How many business days are there between my next two deadlines?",
      "Read both deadlines and calculate the interval.", required=("get_upcoming", "calculate")),
    P("calendar_dst_move", "calendar",
      "Move Friday's meeting to next Friday, keeping the same local time even if daylight saving changes.",
      "Resolve and update the event using local-time semantics.",
      required=("get_upcoming", "update_event"), mode="plan"),

    # Files, devices, and capabilities.
    P("files_reorganize_vague", "files", "Reorganize my files.",
      "Ask which directory before moving anything.", one_of=("organize_files", "move_path"),
      mode="plan", clarify_target=True),
    P("files_logs_dry_run", "files",
      "Move my debug logs into folders by month, but show me the proposed moves first.",
      "Plan the organization without changing files.", one_of=("organize_files", "find_files"),
      forbidden=("move_path",), mode="plan"),
    P("files_zshrc_readonly", "files", "Read the first few lines of `~/.zshrc`; don't modify it.",
      "Read only the named file.", one_of=("read_file", "run_shell"),
      forbidden=("write_file", "move_path", "delete_path")),
    P("device_open_state", "device", "What do I currently have open?",
      "Inspect apps/windows, not calendar data.", one_of=("list_running_apps", "window_control"),
      forbidden=("get_upcoming",)),
    P("device_wisp_slow", "device", "Why is my Mac slow when Wisp is answering?",
      "Inspect system and Wisp status.", required=("system_status", "wisp_status")),
    P("device_volume_music", "device", "Turn the volume down and then play some quiet jazz.",
      "Change volume and start music in sequence.", required=("set_volume", "music"), mode="plan"),
    P("device_screenshot_vision", "device", "Take a screenshot and tell me what is visible in it.",
      "Capture only with approval, then disclose that visual interpretation is unavailable.",
      required=("screen_capture",), mode="plan"),
    P("capability_bank", "capability", "What is my current bank balance?",
      "State banking data is unavailable; do not hunt through unrelated data.",
      forbidden=("search_notes", "view_emails", "run_shell")),
    P("capability_matrix", "capability",
      "Can you send text messages, create reminders, and read browser history?",
      "Answer capability truthfully without performing the actions.",
      one_of=("wisp_capabilities", "wisp_status", "wisp_skills"),
      forbidden=("send_message", "add_reminder")),
    P("files_delete_duplicates", "files",
      "Delete duplicate downloads, but only when their contents are identical.",
      "Verify identity and require confirmation before deleting exact duplicates.",
      required=("find_files",), one_of=("trash_file", "delete_path"), mode="plan"),

    # Multi-turn sequences with synthetic previous-tool context.
    P("seq61_t1", "multiturn", "Summarize my inbox.", "Read the inbox.",
      required=("summarize_emails",)),
    P("seq61_t2", "multiturn", "Only from yesterday.", "Inherit email and narrow time.",
      required=("summarize_emails",), last_tools="summarize_emails"),
    P("seq62_t1", "multiturn", "What's on my calendar this week?", "Read this week.",
      required=("get_upcoming",)),
    P("seq62_t2", "multiturn", "Write that up as a note.", "Create a note from calendar context.",
      required=("create_note",), last_tools="get_upcoming", mode="plan"),
    P("seq62_t3", "multiturn", "Yes, go ahead.", "Confirm note creation, not chit-chat.",
      required=("create_note",), last_tools="get_upcoming create_note",
      last_assistant="I can create that note. Would you like me to go ahead?", mode="plan"),
    P("seq63_t1", "multiturn", "What did Alex text me?", "Read Alex's thread.",
      one_of=("search_conversations", "summarize_messages")),
    P("seq63_t2", "multiturn", "Reply that Thursday works.", "Reply in Messages.",
      required=("send_message",), last_tools="search_conversations summarize_messages", mode="plan"),
    P("seq63_t3", "multiturn", "Actually, email him instead.", "Switch to email, preserving Alex.",
      required=("send_email",), last_tools="send_message lookup_contact", mode="plan"),
    P("seq64_t1", "multiturn", "Find my next meeting with Priya.", "Read the event.",
      required=("get_upcoming",)),
    P("seq64_t2", "multiturn", "Move it back one hour.", "Inherit and update the event.",
      required=("update_event",), last_tools="get_upcoming", mode="plan"),
    P("seq64_t3", "multiturn", "No, I meant one day.", "Correct the pending update.",
      required=("update_event",), last_tools="get_upcoming update_event", mode="plan"),
    P("seq65_t1", "multiturn", "What's Nvidia trading at?", "Fetch Nvidia price.",
      required=("get_stock_price",)),
    P("seq65_t2", "multiturn", "What about Apple?", "Inherit stock-price intent.",
      required=("get_stock_price",), last_tools="get_stock_price"),
    P("seq65_t3", "multiturn", "Which had the worse week?", "Compare both stocks weekly.",
      required=("get_stock_price",), last_tools="get_stock_price"),
    P("seq66_t1", "multiturn", "Find the Wi-Fi password in my notes.", "Search Notes.",
      required=("search_notes",)),
    P("seq66_t2", "multiturn", "Text it to me.", "Send the prior result to self.",
      required=("draft_message",), forbidden=("send_message",),
      last_tools="search_notes", mode="plan"),
    P("seq66_t3", "multiturn", "Delete that message from the queue.", "Cancel scheduled send only.",
      required=("cancel_scheduled_send",), last_tools="send_message schedule_send", mode="plan"),

    # Exact live-regression shapes from the 2026-08-24 router remediation. These remain in the
    # original spelling/casing because typo normalization and natural phrasing
    # are part of what each case measures. No real addresses are duplicated.
    P("live_move_in_grounding", "live_regression",
      "I need you to send mom a message reminder her about my move in date",
      "Search a wide calendar window and secondary sources; never compose a date absent from tool results.",
      required=("get_upcoming", "search_notes"),
      required_args=(("get_upcoming", "days", 60),),
      one_of=("view_messages", "search_conversations"), mode="plan"),
    P("live_stock_exact_span", "live_regression",
      "send a message to mom with the share price of nvidia and amd from today and from two weeks ago. Be descriptive",
      "Use an exact two-week stock period and ground both dated prices before composing.",
      required=("get_stock_price", "send_message"),
      required_args=(("get_stock_price", "period", "2 weeks"),), mode="plan"),
    P("live_schedule_typo", "live_regression",
      "scedule send a message to mom at 9pm with my schedule for tmrow",
      "Normalize typos, read tomorrow's calendar, and queue only after successful confirmation.",
      required=("get_upcoming", "schedule_send"),
      required_args=(("get_upcoming", "days", 2),), mode="plan"),
    P("live_bare_calendar_window", "live_regression", "check my calender",
      "Treat an unqualified calendar check as broader than the seven-day tool default.",
      required=("get_upcoming",), required_args=(("get_upcoming", "days", 60),)),
    P("live_stock_topic_not_calendar", "live_regression",
      "can you send mom a message with the movements of my stocks today",
      "Fetch stock data without forcing an unrelated calendar lookup.",
      required=("get_stock_price", "send_message"), forbidden=("get_upcoming",), mode="plan"),
    P("live_tools_inventory", "live_regression", "what tools are available to you",
      "Use deterministic capability inventory, not the turn's retrieved subset.",
      one_of=("wisp_capabilities", "wisp_status", "wisp_skills")),

    # Known aggregate gaps from docs/OPTIMIZATION_BACKLOG.md.
    P("aggregate_catch_up", "known_gap", "Catch me up.",
      "Aggregate recent personal activity.", one_of=("get_recent_activity", "daily_brief")),
    P("aggregate_forgetting", "known_gap", "What am I forgetting?",
      "Aggregate reminders, calendar, notes, mail, and messages.",
      required=("get_upcoming", "search_notes", "summarize_emails", "summarize_messages")),
)


def validate() -> list[str]:
    """Return corpus-shape errors without importing the router."""
    errors: list[str] = []
    seen: set[str] = set()
    for case in CASES:
        if case.id in seen:
            errors.append(f"duplicate id: {case.id}")
        seen.add(case.id)
        if case.mode not in {"read", "plan"}:
            errors.append(f"{case.id}: invalid mode {case.mode!r}")
        if not case.prompt.strip() or not case.expected.strip():
            errors.append(f"{case.id}: prompt and expected behavior are required")
        overlap = set(case.required) & set(case.forbidden)
        if overlap:
            errors.append(f"{case.id}: required and forbidden overlap: {sorted(overlap)}")
    return errors


if __name__ == "__main__":
    problems = validate()
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"{len(CASES)} adversarial router prompts validated")
