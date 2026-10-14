"""Synthetic tool results used by ``eval_planner.py``.

Every explicit mapping below targets a tool in the current registry. Tools
without a tailored result receive a generic synthetic result in ``fake_result``.
"""
from __future__ import annotations

from service.router.model_led import needs_effect_owner
from service.tools.registry import REGISTRY


def _value(args: dict, *keys: str, default: str = "?") -> str:
    for key in keys:
        value = args.get(key) if isinstance(args, dict) else None
        if value not in (None, ""):
            return str(value)[:60]
    return default


FAKE = {
    "get_upcoming": lambda a: "Calendar: Team standup, Tue Oct 13 9:30-10:00 AM\nCalendar: Dentist, Tue Oct 13 3:00-4:00 PM",
    "get_past_events": lambda a: "Calendar: Project sync, Fri Oct 9 2:00-2:30 PM\nCalendar: Lunch with Sam, Thu Oct 8 12:00-1:00 PM",
    "find_free_time": lambda a: "Free: Tue Oct 13 10:30 AM-12:00 PM; Tue Oct 13 1:00-3:00 PM",
    "search_reminders": lambda a: "Reminder: Call mom, Tue Oct 13 6:00 PM\nReminder: Pay rent, Thu Oct 15 9:00 AM",
    "daily_brief": lambda a: "Today: 2 events (standup 9:30, dentist 3:00 PM), 1 reminder (call mom 6 PM), 3 unread emails.",
    "add_calendar_event": lambda a: f"Added to Calendar: {_value(a, 'title')} at {_value(a, 'when_iso')}.",
    "cancel_event": lambda a: f"Cancelled: {_value(a, 'title')}.",
    "update_event": lambda a: f"Updated event: {_value(a, 'title')} now {_value(a, 'when_iso')}.",
    "add_reminder": lambda a: f"Reminder added: {_value(a, 'title')} at {_value(a, 'when_iso')}.",
    "complete_reminder": lambda a: f"Marked done: {_value(a, 'title')}.",
    "update_reminder": lambda a: f"Reminder updated: {_value(a, 'title', 'new_title')}.",
    "clear_reminders": lambda a: "Cleared 2 reminders.",
    "clear_past_reminders": lambda a: "Cleared 1 past reminder.",
    "lookup_contact": lambda a: f"{_value(a, 'name')} (Sam Lee): +1 555 0100, sam.lee@example.com",
    "list_contacts": lambda a: "Sam Lee +1 555 0100; Alex Kim +1 555 0111",
    "summarize_emails": lambda a: "2 emails: (1) Alex Kim, 'Q3 budget review', 9:12 AM, wants sign-off by Friday. (2) Sam Lee, 'Lunch Thursday?', 8:40 AM.",
    "view_emails": lambda a: "id msg-1001 | From: Alex Kim <alex.kim@example.com> | Subject: Q3 budget review | 9:12 AM\nid msg-1002 | From: Sam Lee <sam.lee@example.com> | Subject: Lunch Thursday? | 8:40 AM",
    "triage_inbox": lambda a: "3 need replies: Alex Kim (msg-1001), Sam Lee (msg-1002), Dana Cho (msg-1003).",
    "send_email": lambda a: f"Sent email to {_value(a, 'to')}: {_value(a, 'subject')}.",
    "reply_to_email": lambda a: f"Replied to {_value(a, 'message_id')}.",
    "forward_email": lambda a: f"Forwarded {_value(a, 'message_id')} to {_value(a, 'to')}.",
    "draft_email": lambda a: f"Draft email saved for {_value(a, 'to')}: {_value(a, 'subject')}.",
    "schedule_send": lambda a: f"Scheduled {_value(a, 'channel')} to {_value(a, 'to')} for {_value(a, 'when')}.",
    "send_message": lambda a: f"Sent to {_value(a, 'to')} via Messages.",
    "draft_message": lambda a: f"Draft ready to {_value(a, 'to')}: {_value(a, 'text')}",
    "summarize_messages": lambda a: "2 conversations: Sam Lee: 'running 10 min late' (9:05 AM). Mom: 'call me tonight' (8:20 AM).",
    "view_messages": lambda a: "Sam Lee | Sam Lee: running 10 min late [9:05 AM]\nMom | Mom: call me tonight [8:20 AM]",
    "search_conversations": lambda a: "Sam Lee | Me: see you at 5 [Oct 9]\nSam Lee | Sam Lee: sounds good [Oct 9]",
    "search_notes": lambda a: "Note 'Groceries' (Oct 10): milk, eggs, bread\nNote 'Ideas' (Oct 8): app redesign",
    "create_note": lambda a: f"Created the note '{_value(a, 'title')}'.",
    "append_note": lambda a: f"Added it to '{_value(a, 'title')}'.",
    "recall": lambda a: "1 memory: Sam Lee is the user's coworker.",
    "remember": lambda a: f"Saved memory: {_value(a, 'fact')}",
    "forget": lambda a: "Forgot 1 memory.",
    "web_search": lambda a: f"1) Example result about {_value(a, 'query')} (example.com) 2) Second result (example.org)",
    "web_fetch": lambda a: "Page text: Example page. Hours 9-5. Contact info@example.com.",
    "get_weather": lambda a: "Tue Oct 13: 68F partly cloudy, 10% chance of rain.",
    "get_stock_price": lambda a: "AAPL 227.10 USD (+0.8%) as of 10:15 AM.",
    "list_dir": lambda a: "report.pdf\nnotes.txt\nphotos/",
    "find_files": lambda a: "/Users/test/Documents/report.pdf\n/Users/test/Downloads/report_v2.pdf",
    "read_file": lambda a: "Quarterly report draft. Revenue up 4%. TODO: add charts.",
    "delete_path": lambda a: f"Deleted {_value(a, 'path')}.",
    "write_file": lambda a: f"Wrote {_value(a, 'path')}.",
    "open_app": lambda a: f"Opened {_value(a, 'name', 'app')}.",
    "set_timer": lambda a: "Timer set.",
    "set_volume": lambda a: "Volume set.",
    "calculate": lambda a: "= 42",
    "world_time": lambda a: "Tokyo: Tue Oct 13 2:30 AM",
    "get_volume": lambda a: "Output volume 45%, not muted.",
    "system_status": lambda a: "CPU 18% (top: Safari 9%, Wisp 4%). Memory 15.2 of 24 GB used. Disk: 212 GB free of 994 GB.",
    "list_running_apps": lambda a: "Safari (9% CPU), Mail (2%), Wisp (4%), Music (1%), Finder (0%).",
    "travel_time": lambda a: f"About 24 min by car to {_value(a, 'destination', 'to', default='the destination')} in current traffic (11.8 mi).",
    "find_place": lambda a: "1) Thai Basil, 0.4 mi, 4.6 stars, open until 10 PM 2) Lotus Kitchen, 0.9 mi, 4.4 stars, open until 9:30 PM",
    "wikipedia_summary": lambda a: f"{_value(a, 'title', 'query', default='Topic')}: wrought-iron lattice tower in Paris, completed 1889, 330 m tall, designed by Gustave Eiffel's company.",
    "describe_me": lambda a: "Profile: name Test User, works at Example Co, lives in San Francisco.",
    "list_scheduled_sends": lambda a: "No scheduled sends.",
}

# These mappings in PR #211 referred to tools removed by the 100-tool registry
# consolidation. Their names stay in the audit report, but their fake functions
# are intentionally not carried forward.
RETIRED_FAKE_NAMES = (
    "air_quality", "astronomy", "convert_currency", "define_word",
    "get_battery_status", "get_directions", "get_sports_scores",
    "keychain_read", "list_bluetooth_devices", "network_info", "rain_radar",
    "recipe_lookup", "search_memory", "set_alarm", "summarize_thread",
    "trash_file",
)


def unregistered_fake_names() -> list[str]:
    """Return explicit fake mappings that no longer have a registered tool."""
    return sorted(set(FAKE) - REGISTRY.keys())


def fake_result(name: str, args: dict) -> str:
    if name in FAKE:
        try:
            return FAKE[name](args)[:380]
        except Exception:  # noqa: BLE001 — malformed fixture arguments stay synthetic
            pass
    tool = REGISTRY.get(name)
    if tool is None:
        return "(unknown synthetic tool)"
    if needs_effect_owner(tool.category):
        return f"Done: {name} completed."
    return f"({name} returned no data for this request.)"
