"""What Wisp says about itself: "What can you do?", "Who are you?", "Is my
data private?" and "How do I use Wisp?".

These are a new user's first questions, and they are often asked before the
model has finished loading. So the answers are written here, deterministic and
true, and the router hands them straight to the user (wisp_capabilities is a
presynthesized tool, see service/main.py): no model runs, nothing is guessed,
and a removed feature cannot creep back into the answer.

Rules for this text:
- Every feature named here exists in this build. A line about a tool is shown
  only when that tool is registered and usable now (routable_tool_names).
- Live state is read, never assumed: which sources Wisp can read right now,
  whether a Google account is connected, and whether a cloud model is on.
- Plain words for a first-time user. No tool names, counts or internal terms.
"""
from __future__ import annotations

import re

TOPICS = ("overview", "identity", "privacy", "howto")

# One try-it prompt per connected source, in this order, then prompts that need
# no source. Each one routes to a real, scoped tool today
# (tests/test_capability_welcome.py checks the routes).
TRY_IT = (
    ("calendar", "What's on my calendar tomorrow?"),
    ("email", "Any emails I need to reply to?"),
    ("reminders", "Remind me at 5pm to call Mom."),
    ("messages", "Any texts I need to respond to?"),
    (None, "Set a timer for 10 minutes."),
    (None, "What time is it in Tokyo?"),
    (None, "What's the weather in Paris this weekend?"),
)


def _usable_tools() -> set[str]:
    try:
        from service.tools.registry import routable_tool_names
        return routable_tool_names()
    except Exception:  # noqa: BLE001 - the answer must never fail on this
        return set()


def _sources() -> list[dict] | None:
    """Every source's live status (service/assistant/sync_status), or None when
    the readers can't be asked at all. One unreadable source never hides the rest."""
    try:
        from service.assistant.sync_status import SOURCE_IDS, source_status
    except Exception:  # noqa: BLE001
        return None
    out = []
    for source_id in SOURCE_IDS:
        try:
            out.append(source_status(source_id))
        except Exception:  # noqa: BLE001
            continue
    return out


def _google_connected() -> bool:
    try:
        from service.gmail_source import connected_accounts
        return bool(connected_accounts())
    except Exception:  # noqa: BLE001
        return False


def _cloud_state() -> tuple[str, str]:
    """("everything" | "super" | "", provider host). Remote-only builds count as everything."""
    try:
        from service.inference.remote_policy import remote_only_profile
        profile = remote_only_profile()
        if profile is not None:
            from urllib.parse import urlsplit
            return "everything", urlsplit(profile.base_url).hostname or "your cloud provider"
    except Exception:  # noqa: BLE001
        pass
    try:
        from service.config import cloud_everything_enabled, cloud_super_model_enabled
        if cloud_everything_enabled():
            return "everything", "your cloud provider"
        if cloud_super_model_enabled():
            return "super", "your cloud provider"
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def _user_name() -> str:
    try:
        from service.memory.profile import load
        return load().get("name") or ""
    except Exception:  # noqa: BLE001
        return ""


def _join(labels: list[str]) -> str:
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _status_lines(sources: list[dict] | None) -> list[str]:
    if not sources:
        return ["I couldn't check which of your apps are connected just now."]
    lines = []
    ready = [s["label"] for s in sources if s.get("state") == "ready"]
    syncing = [s["label"] for s in sources if s.get("state") == "syncing"]
    if ready:
        lines.append(f"Connected: {', '.join(ready)}.")
    if syncing:
        lines.append(f"Still reading for the first time: {', '.join(syncing)}.")
    for source in sources:
        if source.get("state") in ("ready", "syncing"):
            continue
        reason = str(source.get("reason") or "").strip()
        lines.append(f"Not connected: {source['label']}" + (f". {reason}" if reason else "."))
    return lines


def _try_its(sources: list[dict] | None) -> list[str]:
    connected = {s.get("id") for s in sources or () if s.get("state") == "ready"}
    return [prompt for source, prompt in TRY_IT if source is None or source in connected][:3]


def _can_lines(tools: set[str]) -> list[str]:
    lines = []
    reads = [label for label, needed in (
        ("calendar", {"get_upcoming"}), ("reminders", {"get_upcoming"}),
        ("mail", {"summarize_emails"}), ("messages", {"summarize_messages"}),
        ("notes", {"search_notes"})) if needed <= tools]
    if reads:
        lines.append(f"Read and sum up your {_join(reads)}, and tell you what needs a reply.")
    acts = [label for label, needed in (
        ("draft and send emails and texts", {"send_email", "send_message"}),
        ("add reminders and calendar events", {"add_reminder", "add_calendar_event"}),
        ("move events on your calendar", {"update_event"})) if needed <= tools]
    if acts:
        joined = "; ".join(acts) if len(acts) > 2 else " and ".join(acts)
        lines.append(joined[0].upper() + joined[1:] + ". Before anything is sent, or your "
                     "calendar changes, I show it to you and wait for your OK.")
    looks = [label for label, needed in (
        ("search the web", {"web_search"}), ("check the weather", {"get_weather"}),
        ("do quick math", {"calculate"})) if needed <= tools]
    if looks:
        joined = _join(looks)
        lines.append(joined[0].upper() + joined[1:] + ".")
    if "toggle_setting" in tools or "open_app" in tools:
        lines.append("Open apps and change Mac settings like Do Not Disturb or dark mode.")
    if "remember" in tools:
        lines.append("Remember what you tell me (\"remember that my locker is 112\").")
    return lines


def overview() -> str:
    """"What can you do?": what Wisp does (only usable features), which sources
    it can read right now, three try-its that fit them, and where things are."""
    tools = _usable_tools()
    sources = _sources()
    out = ["**Hi, I'm Wisp.** My AI runs right here on this Mac.", "", "**What I can do**"]
    out += ["- " + line for line in _can_lines(tools)]
    out.append("- Google: your account is connected, so I read its Gmail and Calendar too."
               if _google_connected() else
               "- Google: connect a Google account in **Settings → Google** and I'll read its "
               "Gmail and Calendar too.")
    out += ["", "**Right now**"] + _status_lines(sources)
    out += ["", "**Try asking**"] + [f"- \"{prompt}\"" for prompt in _try_its(sources)]
    out += ["",
            "**Where things are**",
            "- Press ⌥ Space to ask me from anywhere.",
            "- Type / in Ask to see shortcuts, like /today and /report.",
            "- Brief and Day are the tabs next to Ask: a summary of what's new, and your plan for today.",
            "- Getting to know you (type /about) tells me your name and what matters to you, "
            "so my answers fit you better.",
            "",
            "Ask \"is my data private?\" or \"how do I use Wisp?\" any time."]
    return "\n".join(out)


def identity() -> str:
    name = _user_name()
    out = ["I'm **Wisp**, your assistant in the Mac's notch. My AI runs on this Mac, so I can help "
           "with your calendar, reminders, mail, messages and notes without sending them to a server.",
           "",
           "Open me any time with **⌥Space** or by moving the pointer to the notch. Ask me anything, "
           "check **Brief** for your day at a glance, or **Day** for today's timeline."]
    if name:
        out += ["", f"And you're {name}: you told me in Getting to know you. Type **/about** to change it."]
    else:
        out += ["", "I don't know your name yet. Type **/about** and I'll ask three quick questions."]
    out += ["", "Ask \"what can you do?\" for the full tour."]
    return "\n".join(out)


def privacy() -> str:
    cloud, host = _cloud_state()
    out = ["**Your data stays on this Mac.**", ""]
    if cloud == "everything":
        out = ["**Right now a cloud model is on**, so this is different from usual.", "",
               f"- What you ask, and the data a reply needs (your mail, messages, calendar and what "
               f"you told me about yourself), is sent to {host} to be answered.",
               "- To keep everything on this Mac again, turn the cloud model off in **Settings**."]
    else:
        out.append("- My AI model runs here, on your Mac. Your mail, messages, calendar, notes, "
                   "and what you tell me are read and kept on this Mac, not on a server.")
        if cloud == "super":
            out.append("- You've turned on a cloud model in Settings. Questions it answers go to "
                       "that provider. Everything else stays here.")
        else:
            out.append("- No cloud AI is used unless you turn one on in **Settings**.")
    out += ["- Some things have to go online: web searches, weather, stocks and research. "
            "If you connect Google, I fetch that account's mail and calendar from Google.",
            "- I never send a message or email, or change your calendar, without showing it "
            "to you first.",
            "- You decide what I can read, in System Settings → Privacy & Security.",
            "- To see or erase what I know about you: **/about** for Getting to know you, and "
            "**/memory** for things I've remembered."]
    return "\n".join(out)


# How-to entries: (keywords that pick it, the step). A specific question
# ("how do I connect Google?") gets only its matching entries; anything else
# gets the whole guide.
_HOWTO = (
    (r"open|start|launch|find|summon|shortcut|hotkey|notch",
     "**Open Wisp**: press **⌥Space** from any app, or move the pointer to the notch."),
    (r"ask|talk|chat|use|question|type",
     "**Ask**: type your question and press Return. Type **/** to see shortcuts. "
     "**⌘N** starts a new chat; **⌘O** continues it in the Wisp window."),
    (r"brief|day|summary|plan|schedule|today",
     "**Brief and Day**: the tabs next to Ask. Brief is your day at a glance; Day is today's "
     "timeline of events and reminders."),
    (r"about me|know me|my name|profile|getting to know|tone|goals?|call me",
     "**Tell Wisp about you**: type **/about**, or choose **Tell Wisp About You…** from the "
     "menu bar icon. Change or erase it any time in the Wisp window's Memory → About you."),
    (r"remember|memory|forget",
     "**Memory**: say \"remember that…\" and I'll keep it. Type **/memory** to see or delete "
     "what I've saved."),
    (r"google|gmail|workspace|drive",
     "**Connect Google**: open **Settings → Google**, then sign in. I'll read that account's "
     "Gmail and Calendar too."),
    (r"permission|access|allow|grant|privacy|calendar access|full disk",
     "**Let Wisp read more**: choose **Grant More Access…** from the menu bar icon, or open "
     "System Settings → Privacy & Security."),
    (r"research|report",
     "**Research**: type **/research**, then your topic. I draft a plan you can edit, read "
     "the web and write a cited report."),
    (r"problem|bug|broken|wrong|report|feedback",
     "**Something wrong?** Type **/report**, or choose **Report a Problem…** from the menu "
     "bar icon."),
    (r"quit|close|stop|turn off",
     "**Close or quit**: × closes the panel and Wisp stays in the menu bar. To quit, choose "
     "**Quit Wisp** from the menu bar icon."),
)

_GENERIC_HOWTO = re.compile(
    r"\b(?:use|get(?:ting)? started|work|works|set ?up|begin|help)\b", re.I)


def howto(query: str = "") -> str:
    text = (query or "").lower()
    specific = [step for keys, step in _HOWTO
                if re.search(rf"\b(?:{keys})", text) and not _GENERIC_HOWTO.search(text)]
    if specific:
        return "\n".join(["- " + step for step in specific]
                         + ["", "Ask \"how do I use Wisp?\" for the whole guide."])
    return "\n".join(["**Getting started with Wisp**", ""]
                     + ["- " + step for _, step in _HOWTO])


def answer(topic: str = "overview", query: str = "") -> str:
    topic = (topic or "overview").strip().lower()
    if topic == "identity":
        return identity()
    if topic == "privacy":
        return privacy()
    if topic == "howto":
        return howto(query)
    return overview()


# A general "what can you do" (not "can you send texts?", which keeps the
# precise Yes/No capability report).
_GENERAL_INVENTORY = re.compile(
    r"\W*(?:(?:please|hey|hi|ok|okay|so|wisp)[,\s]+)*(?:"
    r"what\s+(?:else\s+)?(?:can|could)\s+you\s+(?:do|help\s+(?:me\s+)?with)|"
    r"what\s+(?:tools|capabilities)\s+(?:are\s+available|do\s+you\s+have)(?:\s+to\s+you)?|"
    r"which\s+(?:assistant\s+)?capabilities\s+do\s+you\s+have|"
    r"what\s+are\s+you\s+able\s+to\s+do)"
    r"(?:\s+(?:for\s+me|here|now|right\s+now|exactly))?[?.!\s]*", re.I)


def is_general_inventory(query: str) -> bool:
    return not (query or "").strip() or bool(_GENERAL_INVENTORY.fullmatch(query or ""))
