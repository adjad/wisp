"""Example user utterances for tools that shipped before aliases existed.

WHERE ALIASES BELONG — THE RULE
-------------------------------
NEW tools declare their own aliases inline, in the `register(...)` call, next to
the description they complement. That is the better home: you can see at a
glance whether an alias repeats the description (wasted) or covers vocabulary it
misses (the point).

This table is the RETROFIT for the 54 tools that already existed when semantic
retrieval was added. Backfilling them inline would have meant touching a dozen
live tool modules to add data that changes on a completely different cadence
from the behavior around it — recall tuning is not a reason to reopen
`email_tools.py`. So the retrofit lives here, in one file, with one diff.

`apply()` never overwrites aliases a tool declared for itself, so the two
sources cannot fight. If a tool listed here later grows inline aliases, the
inline ones win and its entry here becomes dead weight worth deleting.

WHAT MAKES A GOOD ALIAS
-----------------------
Write what the USER says, not what the tool does. The name and description are
already in the embedded document, so an alias that paraphrases the description
adds nothing — the retrieval gap is between machine vocabulary and human
vocabulary. `recall`'s description says "search everything the user has asked you
to remember"; nobody says that. They say "what did I tell you about the car".

Cover the phrasings that DON'T share vocabulary with the name. `move_path` is
easy to retrieve for "move this file" and hard for "tidy up my downloads" — the
second is the one worth writing down.

These never enter a prompt. They are embedded into the tool index and nowhere
else, so a long list here costs retrieval quality only, never context.
"""
from __future__ import annotations

ALIASES: dict[str, list[str]] = {
    # ---- calendar & reminders ----
    "add_calendar_event": [
        "put dinner with Sam on my calendar Friday at 7",
        "schedule a meeting with the team next Tuesday",
        "book me a dentist appointment on the 12th",
        "I have a flight Thursday morning, add it",
    ],
    "add_reminder": [
        "set an alarm tomorrow for my iPhone repair appointment",
        "remind me to take the trash out tonight",
        "don't let me forget to call the landlord",
        "nudge me about the insurance renewal next month",
        "remind me to take my medication at 9",
    ],
    "cancel_event": [
        "cancel my lunch tomorrow",
        "delete the dentist appointment",
        "I'm not going to the standup, take it off",
        "drop the Friday meeting",
    ],
    "clear_past_reminders": [
        "clean up everything that's already past due",
        "get rid of my old overdue reminders",
        "clear out the stuff I already missed",
    ],
    "clear_reminders": [
        "delete my reminders for today",
        "clear all of my reminders",
        "remove tomorrow's reminders",
        "delete every active reminder",
    ],
    "get_upcoming": [
        "what's on my calendar today",
        "what do I have going on this week",
        "am I free tomorrow afternoon",
        "what's my next meeting",
        "what do I need to do today",
    ],
    "get_past_events": [
        "what did I do last Tuesday",
        "when was my last dentist appointment",
        "what meetings did I have last month",
    ],
    # ---- mail ----
    "send_email": [
        "email Sarah the quarterly numbers",
        "shoot my professor a note about the extension",
        "let the landlord know the sink is leaking, by email",
    ],
    "draft_email": [
        "start an email to Dan but don't send it",
        "write up a message to HR for me to look over first",
        "prepare an email about the invoice",
    ],
    "reply_to_email": [
        "reply to that and say I'll be there",
        "respond to Sarah's email saying yes",
        "answer the message from the bank",
    ],
    "view_emails": [
        "what does that email actually say",
        "read me the email from the bank word for word",
        "show me the full text of the message from Dan",
        # SEARCH phrasings live here rather than on a separate `search_email`
        # tool: view_emails already takes a `query`, so a second tool would be a
        # near-duplicate schema — precisely what a small model picks wrong
        # between, and a cost on every menu both appear in.
        "find the email about the apartment viewing",
        "search my inbox for the flight confirmation",
        "did I get anything from the landlord",
        "look for that email with the invoice attached",
    ],
    "summarize_emails": [
        "what's in my inbox",
        "any important email today",
        "catch me up on my mail",
        "did anyone email me about the apartment",
    ],
    "archive_email": [
        "get that out of my inbox",
        "file away the Walgreens email",
        "clear that one out",
    ],
    "mark_email_read": [
        "mark those as read",
        "I've seen it, clear the unread badge",
    ],
    # ---- messages & contacts ----
    "send_message": [
        "text mom I'm running late",
        "let Dan know I'll be there in ten",
        "message the group that dinner is off",
    ],
    "draft_message": [
        "type out a text to Sarah but don't send it yet",
        "get a message ready for my brother",
    ],
    "view_messages": [
        "what exactly did she say",
        "read me Dan's texts word for word",
        "show me the actual messages",
        # SEARCH phrasings live here rather than on a separate search_messages
        # tool: view_messages already takes a `query`, so a second tool would
        # be a near-duplicate schema — the same consolidation call as
        # view_emails/search_email in Phase 1.
        "search my texts for the address he sent",
        "find the message where she mentioned the flight",
        "look through my messages for anything about the party",
    ],
    "summarize_messages": [
        "any new texts",
        "what did I miss on messages",
        "catch me up on my texts",
    ],
    "lookup_contact": [
        "what's mom's phone number",
        "what's Dan's email address",
        "do I have a number for the dentist",
    ],
    "list_contacts": [
        "who's in my contacts",
        "how many contacts do I have",
        "show me everyone I have saved",
    ],
    # ---- scheduled sends ----
    "schedule_send": [
        "text him this tomorrow morning at 8",
        "send that email later tonight",
        "wait until Monday to send it",
    ],
    "list_scheduled_sends": [
        "what have I got queued up to go out",
        "anything waiting to send",
    ],
    "cancel_scheduled_send": [
        "don't send that after all",
        "call off the text I scheduled",
    ],
    # ---- notes & memory ----
    "search_notes": [
        "what did I write down about the trip",
        "find my note about the wifi password",
        "pull up what I jotted down in the meeting",
    ],
    "remember": [
        "my sister's birthday is March 3rd",
        "keep in mind I'm allergic to shellfish",
        "for future reference, I park in section C",
    ],
    "recall": [
        "what did I tell you about the car",
        "what's my sister's name again",
        "what do you know about my job",
        "did I mention anything about the apartment",
    ],
    "forget": [
        "stop remembering that about me",
        "that's not true anymore, drop it",
    ],
    # ---- files ----
    "list_dir": [
        "what's in my downloads folder",
        "show me what's on my desktop",
        "what files are in that folder",
    ],
    "read_file": [
        "what does this pdf say",
        "summarize that document for me",
        "open the spreadsheet and tell me what's in it",
        "read the contract and pull out the key dates",
    ],
    "write_file": [
        "save that to a file",
        "put this text in a document on my desktop",
    ],
    "move_path": [
        "tidy up my downloads folder",
        "put all the screenshots in one place",
        "rename that file to something sensible",
        "file those away somewhere else",
        "reorganize my desktop",
    ],
    "delete_path": [
        "get rid of that file",
        "throw away the old installer",
    ],
    # ---- system ----
    "set_volume": [
        "turn it down",
        "make it louder",
        "mute the sound",
    ],
    "get_volume": ["how loud is it right now", "is the sound muted"],
    "lock_screen": ["lock my screen", "I'm stepping away, lock it"],
    "set_wifi": ["turn wifi off", "get me back on wifi"],
    "get_battery_status": [
        "how much battery do I have left",
        "is my battery health okay",
        "am I plugged in",
    ],
    "list_bluetooth_devices": [
        "what's connected over bluetooth",
        "are my airpods paired",
    ],
    "set_keyboard_backlight": [
        "make the keyboard light brighter",
        "turn off the key backlight",
    ],
    "run_speed_test": [
        "how fast is my internet right now",
        "why is my connection so slow",
        "test my wifi speed",
    ],
    # ---- apps & media ----
    "open_app": ["open Spotify", "launch Xcode", "bring up Safari"],
    "quit_app": ["close Chrome", "quit Slack", "shut down Photoshop"],
    "music": [
        "play some jazz",
        "put on my focus playlist",
        "skip this track",
        "pause the music",
        "play something upbeat",
        # "put something on" is how people ask for background music without
        # naming music at all — measured as the one held-out probe that missed
        # entirely (music ranked 12th) before this line existed.
        "put something on in the background",
        "throw on some music while I work",
    ],
    "spotify": [
        "play that album on Spotify",
        "next song on Spotify",
    ],
    # ---- clipboard ----
    "clipboard_read": ["what did I just copy", "what's on my clipboard"],
    "clipboard_write": ["copy that for me", "put that on my clipboard"],
    # ---- web & data ----
    "get_weather": [
        "what's it like outside",
        "do I need an umbrella today",
        "how cold is it going to get tonight",
        "what's the weather in Chicago this weekend",
    ],
    "get_stock_price": [
        "how's Apple doing today",
        "what's Tesla trading at",
        "did my stocks go up",
        "what's bitcoin worth",
    ],
    "web_fetch": [
        "what does this page say",
        "pull up that article and summarize it",
        "check what's on this link",
    ],
    "web_search": [
        "search the web for this",
        "look this up online",
        "find recent reliable sources",
        "research this topic",
        "compare what different sources say",
    ],
    "http_request": [
        "post this to the API",
        "send a PUT request to that endpoint",
    ],
    "search_browser_history": [
        "what was that site I was on yesterday",
        "find the page I visited about mortgages",
        "what have I been reading lately",
    ],
    # ---- activity & shell ----
    "get_recent_activity": [
        "what's new",
        "catch me up",
        "anything I missed while I was out",
        "what happened today",
    ],
    "run_shell": [
        "run this command for me",
        "check what processes are running",
        "what's using all my disk space",
    ],
    "create_tool": [
        "build yourself a tool that can do this",
        "you should be able to do this natively, make it so",
    ],
}


def apply() -> int:
    """Attach the table above to the live registry. Returns the number of tools
    that received aliases.

    Called once at startup, BEFORE the tool index is built — aliases are part of
    the embedded document, so applying them afterwards would leave the index
    keyed on documents that no longer match and force a full re-embed.

    Never clobbers aliases a tool declared inline: a tool that speaks for itself
    is the authority on its own phrasings, and this table is only the retrofit
    for ones that don't (see the module docstring).

    Silently skips names that aren't registered: this table outlives any single
    roster (a skill's tool may be uninstalled, an MCP server disconnected), and a
    stale entry here is not a reason to fail startup.
    """
    from service.tools.registry import REGISTRY

    n = 0
    for name, aliases in ALIASES.items():
        tool = REGISTRY.get(name)
        if tool is not None and not tool.aliases:
            tool.aliases = list(aliases)
            n += 1
    return n
