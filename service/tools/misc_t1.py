"""The remaining T1 odds and ends: alerts, definitions, calls, Wisp's own state.

Each of these replaces something the model would otherwise answer from memory or
by hand-building a URL — the failure mode the 2026-08-18 diagnosis measured when
weather went through `web_fetch` ("invented a city once, resolved 'Dublin' to
Ireland once, reported live conditions as a forecast once").

Severe-weather alerts (get_weather's include=["alerts"]) are deliberately US-only and say so. The National Weather
Service API is keyless and authoritative for the US; there is no free global
equivalent. Returning "no alerts" for a European address — when the truth is
"this service does not cover that address" — would be a fabricated all-clear,
which is the worst possible failure for a severe-weather tool.
"""
from __future__ import annotations

import re

import httpx

from service.tools.registry import register

_UA = "Wisp/1.0 (local macOS assistant; personal use)"
_TIMEOUT = 12


async def _json(url: str, *, params: dict | None = None,
                headers: dict | None = None) -> dict | list | str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as c:
            r = await c.get(url, params=params,
                            headers={"User-Agent": _UA, **(headers or {})})
    except httpx.TimeoutException:
        return "(the service timed out — try again in a moment)"
    except httpx.HTTPError as e:
        return f"(could not reach the service: {e})"
    if r.status_code == 404:
        return "__404__"
    if r.status_code >= 400:
        return f"(service returned HTTP {r.status_code})"
    try:
        return r.json()
    except ValueError:
        return "(the service returned something unreadable)"


# --------------------------------------------------------------------------
# severe weather
# --------------------------------------------------------------------------
async def weather_alerts_at(lat: float, lon: float, name: str, country: str) -> str:
    """Active NWS alerts at coordinates get_weather already resolved and verified."""
    if country.strip().lower() not in {"united states of america", "united states", "usa", "us"}:
        return (f"(no alert source for {name}: severe-weather alerts come from the US "
                "National Weather Service, which only covers US locations.)")
    data = await _json("https://api.weather.gov/alerts/active",
                       params={"point": f"{lat:.4f},{lon:.4f}"},
                       headers={"Accept": "application/geo+json"})
    if isinstance(data, str):
        if data == "__404__":
            return f"(the NWS has no alert zone for {name} — it may be outside US coverage.)"
        return data
    features = data.get("features", []) if isinstance(data, dict) else []
    if not features:
        return "No active severe-weather alerts."

    lines = []
    for f in features[:6]:
        p = f.get("properties") or {}
        event = p.get("event", "Alert")
        severity = p.get("severity", "")
        headline = (p.get("headline") or "").strip()
        lines.append(f"  {event}" + (f" [{severity}]" if severity else "")
                     + (f" — {headline}" if headline else ""))
    more = f"\n  (+{len(features) - 6} more)" if len(features) > 6 else ""
    return "Active alerts:\n" + "\n".join(lines) + more


# --------------------------------------------------------------------------
# dictionary
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# calling
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Wisp's own state
# --------------------------------------------------------------------------
@register(
    "wisp_status",
    "Report Wisp's own state. topic='models' (default): which model is answering, "
    "what's loaded, and how much memory it's using — for 'what model are you "
    "running', 'why are you slow', 'are you using the cloud'. topic='sync': when "
    "mail, messages and notes last synced — for 'when did my email last sync'.",
    {"type": "object", "properties": {
        "topic": {"type": "string", "enum": ["models", "sync"],
                  "description": "What to report. Defaults to models."},
    }},
    category="wisp_admin",
    aliases=["what model are you running", "are you using the cloud",
             "why are you being slow today", "how much memory are you using",
             "which brain is answering me", "what version of you is this",
             "how fresh is your data right now", "when did you last sync everything",
             "how up to date is what you know"],
)
async def wisp_status(topic: str = "models") -> str:
    topic = (topic or "models").strip().lower()
    if topic == "sync":
        return _sync_report()
    if topic != "models":
        return f"(error: unknown topic {topic!r}. Use models or sync.)"
    from service.config import models_config, role_to_model

    from service.inference.remote_policy import remote_only_profile
    profile = remote_only_profile()
    if profile is not None:
        # Remote inference must not claim on-Mac inference: every request
        # goes to the pinned remote endpoint, and there is no local engine.
        from urllib.parse import urlsplit
        lines = [f"Answering with: {profile.model_id}",
                 f"Inference runs on your remote server ({urlsplit(profile.base_url).hostname}), "
                 "not on this Mac: what you ask, and the data a reply needs, is sent there.",
                 "There is no local model or embedder in this build."]
        try:
            from service.inference.remote_policy import remote_tool_allowed
            from service.tools.registry import routable_tool_names
            usable = [n for n in routable_tool_names() if remote_tool_allowed(n)]
            lines.append(f"Usable tools available: {len(usable)}")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)

    cfg = models_config()
    roles = cfg.get("roles", {}) or {}
    agent = role_to_model("agent")
    embed = roles.get("embedding", "—")

    lines = [f"Answering with: {agent}",
             f"Embedder (search + tool routing): {embed}",
             "Everything runs on this Mac — no request leaves the machine."]

    try:
        from service.inference.omlx_client import OMLXClient
        loaded = await OMLXClient().loaded_models()
        if loaded:
            lines.append(f"Loaded right now: {', '.join(sorted(loaded))}")
    except Exception as e:  # noqa: BLE001 — status must not fail because oMLX is down
        lines.append(f"(couldn't reach the inference server: {e})")

    try:
        from service.tools.registry import routable_tool_names
        lines.append(f"Usable tools available: {len(routable_tool_names())}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


@register(
    "wisp_capabilities",
    "Report Wisp's actual capabilities from the live tool registry. Use for "
    "'what tools can you use', 'can you send texts/create reminders/read "
    "browser history', and capability comparisons. This reports the complete "
    "registry, not merely the tools offered on the current turn.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "optional capability question"},
    }},
    category="wisp_admin",
    aliases=["what tools are available to you", "what can you do",
             "can you send texts and create reminders",
             "which assistant capabilities do you have"],
)
def wisp_capabilities(query: str = "") -> str:
    """A general "what can you do?" gets a short welcome built from live state;
    a question about specific abilities ("can you send texts?") gets the
    yes/no report, including the honest "No" rows."""
    if not query.strip() or _GENERAL_CAPABILITY_QUESTION.fullmatch(query):
        return _capability_welcome()
    return _capability_report(query)


# "What can you do?" and its close variants, with nothing more specific asked.
_GENERAL_CAPABILITY_QUESTION = re.compile(
    r"\W*(?:(?:please|hey|hi|ok|okay|so|wisp)[,\s]+)*(?:"
    r"what\s+(?:else\s+)?(?:can|could)\s+you\s+(?:do|help\s+(?:me\s+)?with)|"
    r"what\s+(?:tools|capabilities)\s+(?:are\s+available|do\s+you\s+have)(?:\s+to\s+you)?|"
    r"which\s+(?:assistant\s+)?capabilities\s+do\s+you\s+have|"
    r"what\s+are\s+you\s+able\s+to\s+do)"
    r"(?:\s+(?:for\s+me|here|now|right\s+now|exactly))?[?.!\s]*", re.I)

# One try-it prompt per connected source, in this order, then prompts that need
# no source. Each one routes to a real tool today.
_TRY_IT = (
    ("calendar", "What's on my calendar tomorrow?"),
    ("email", "Any emails I need to reply to?"),
    ("reminders", "Remind me at 5pm to call Mom."),
    ("messages", "Any texts I need to respond to?"),
    (None, "Set a timer for 10 minutes."),
    (None, "What time is it in Tokyo?"),
    (None, "What's the weather in Paris this weekend?"),
)


def _capability_welcome() -> str:
    sources: list[dict] = []
    try:
        from service.assistant.sync_status import SOURCE_IDS, source_status
        for source_id in SOURCE_IDS:
            try:
                sources.append(source_status(source_id))
            except Exception:  # noqa: BLE001 - one unreadable source must not hide the rest
                continue
    except Exception:  # noqa: BLE001 - the welcome still works without live state
        sources = []
    ready = [s["label"] for s in sources if s.get("state") == "ready"]
    syncing = [s["label"] for s in sources if s.get("state") == "syncing"]
    off = [s for s in sources if s.get("state") not in ("ready", "syncing")]

    lines = ["I can read your calendar, reminders, email and messages, set reminders "
             "and timers, and write texts and emails for you. I ask before I send anything.", ""]
    if ready:
        lines.append(f"Connected: {', '.join(ready)}.")
    if syncing:
        lines.append(f"Still reading for the first time: {', '.join(syncing)}.")
    for source in off:
        reason = str(source.get("reason") or "").strip()
        lines.append(f"Not connected: {source['label']}" + (f". {reason}" if reason else "."))
    if not sources:
        lines.append("I couldn't check which of your apps are connected just now.")

    connected = {s["id"] for s in sources if s.get("state") == "ready"}
    tries = [prompt for source, prompt in _TRY_IT if source is None or source in connected][:3]
    lines += ["", "Try asking:"] + [f"- \"{prompt}\"" for prompt in tries]
    lines += [
        "",
        "Where things are:",
        "- Press ⌥ Space to ask me from anywhere.",
        "- Type / in Ask to see shortcuts, like /today and /report.",
        "- Brief and Day are the tabs next to Ask: a summary of what's new, and your plan for today.",
        "- Getting to know you (type /about) tells me your name and what matters to you, "
        "so my answers fit you better.",
    ]
    return "\n".join(lines)


def _capability_report(query: str) -> str:
    from service.tools.registry import routable_tool_names

    names = routable_tool_names()
    capabilities = [
        ("Send text messages", {"send_message"}, True),
        ("Draft text messages", {"draft_message"}, False),
        ("Send email", {"send_email"}, True),
        ("Reply in an existing email thread", {"reply_to_email"}, True),
        ("Schedule a new email or text for later", {"schedule_send"}, True),
        ("Create reminders", {"add_reminder"}, True),
        ("Move calendar events to a new time", {"update_event"}, True),
        ("Read calendar/reminders", {"get_upcoming"}, False),
        ("Read browser history", {"search_browser_history"}, False),
        ("Read Mail", {"view_emails", "summarize_emails"}, False),
        ("Read Messages", {"view_messages", "summarize_messages"}, False),
        ("Search Notes", {"search_notes"}, False),
        ("Read and organize local files", {"read_file", "find_files"}, False),
        ("Change system settings", {"toggle_setting"}, True),
    ]
    rows = []
    for label, required, confirmed in capabilities:
        supported = required <= names
        suffix = " (confirmation required)" if supported and confirmed else ""
        rows.append(f"{'Yes' if supported else 'No'} — {label}{suffix}")
    rows += [
        "No — schedule a reply inside an existing email thread (only new standalone emails and texts can be scheduled)",
        "No — inspect or understand the screen visually (Wisp has no vision model)",
        "No — access a bank balance unless a dedicated connected banking tool is installed",
    ]
    return "Here's what Wisp can and can't do:\n" + "\n".join(rows)


@register(
    "wisp_skills",
    "List installed skills — what they are and whether each is enabled. "
    "Use for 'what skills do I have', 'is X skill on'.",
    {"type": "object", "properties": {}},
    category="wisp_admin",
    aliases=["what skills do I have installed", "which of my skills are active",
             "list my installed skills"],
)
def wisp_skills() -> str:
    from service.skills import all_skills

    skills = all_skills()
    if not skills:
        return "No skills installed."
    lines = [f"  {s.name} — {'enabled' if s.enabled else 'DISABLED'}"
            + (f" (error: {s.error})" if s.error else "")
            for s in skills.values()]
    return f"{len(skills)} skill(s):\n" + "\n".join(lines)


@register(
    "wisp_mcp",
    "List connected MCP servers and how many tools each contributes. Use "
    "for 'what MCP servers are connected', 'is X server working'.",
    {"type": "object", "properties": {}},
    category="wisp_admin",
    aliases=["what mcp servers are connected", "is my mcp server working",
             "list connected mcp servers"],
)
def wisp_mcp() -> str:
    from service.mcp import manager

    if not manager.servers:
        return "No MCP servers configured."
    lines = []
    for name, srv in manager.servers.items():
        status = "connected" if srv.running else f"not running{f' ({srv.error})' if srv.error else ''}"
        lines.append(f"  {name} — {status}, {len(srv.tools)} tool(s)")
    return f"{len(manager.servers)} server(s):\n" + "\n".join(lines)


def _sync_report() -> str:
    import datetime

    from service.tools import email_tools, imessage_tools, notes_tools

    def _fmt(ts: float) -> str:
        if not ts:
            return "never"
        age = datetime.datetime.now() - datetime.datetime.fromtimestamp(ts)
        mins = int(age.total_seconds() // 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        return f"{mins // 60}h {mins % 60}m ago"

    # imessage_tools tracks no sync TIMESTAMP (only the raw cache content) —
    # reported as "cache present/empty" rather than guessed at as "never",
    # which would misreport a genuinely-synced cache with no tracked time as
    # unsynced.
    msgs_state = "cache present" if getattr(imessage_tools, "_lines", "") else "no data cached"
    lines = [
        f"  Mail (recent):    {_fmt(getattr(email_tools, '_headers_at', 0))}",
        f"  Mail (raw):       {_fmt(getattr(email_tools, '_raw_emails_at', 0))}",
        f"  Messages:         {msgs_state} (sync time not tracked)",
        f"  Notes:            {_fmt(getattr(notes_tools, '_notes_at', 0))}",
    ]
    return "Last synced:\n" + "\n".join(lines)
