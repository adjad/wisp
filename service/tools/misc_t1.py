"""The remaining T1 odds and ends: alerts, definitions, calls, Wisp's own state.

Each of these replaces something the model would otherwise answer from memory or
by hand-building a URL — the failure mode the 2026-08-18 diagnosis measured when
weather went through `web_fetch` ("invented a city once, resolved 'Dublin' to
Ireland once, reported live conditions as a forecast once").

`weather_alerts` is deliberately US-only and says so. The National Weather
Service API is keyless and authoritative for the US; there is no free global
equivalent. Returning "no alerts" for a European address — when the truth is
"this service does not cover that address" — would be a fabricated all-clear,
which is the worst possible failure for a severe-weather tool.
"""
from __future__ import annotations

import subprocess
from urllib.parse import quote

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
@register(
    "weather_alerts",
    "Check for active SEVERE WEATHER warnings (storms, floods, heat, winter "
    "storms) for a US location. This is the National Weather Service feed and "
    "covers the UNITED STATES ONLY — for anywhere else it says so rather than "
    "reporting a false all-clear. For ordinary forecasts use get_weather.",
    {
        "type": "object",
        "properties": {
            "location": {"type": "string",
                         "description": "US city and state, e.g. 'Palo Alto,CA', or a ZIP code."},
        },
        "required": ["location"],
    },
    category="web_read",
    aliases=["is there a storm warning", "any weather warnings for tonight",
             "should I worry about the hurricane", "flood warning near me",
             "is it dangerous to drive tonight", "any severe weather alerts"],
)
async def weather_alerts(location: str) -> str:
    place = (location or "").strip()
    if not place:
        return "(no location given — ask the user which city, and do not guess one)"

    geo = await _json("https://nominatim.openstreetmap.org/search",
                      params={"q": place, "format": "json", "limit": 1,
                              "countrycodes": "us"})
    if isinstance(geo, str):
        return geo if geo != "__404__" else f"(couldn't locate {place!r})"
    if not geo:
        return (f"(couldn't find {place!r} in the United States. Severe-weather "
                f"alerts come from the US National Weather Service, which only "
                f"covers US locations — I have no alert source for elsewhere.)")
    lat, lon = float(geo[0]["lat"]), float(geo[0]["lon"])
    display = geo[0].get("display_name", place).split(",")[0]

    data = await _json("https://api.weather.gov/alerts/active",
                       params={"point": f"{lat:.4f},{lon:.4f}"},
                       headers={"Accept": "application/geo+json"})
    if isinstance(data, str):
        if data == "__404__":
            return f"(the NWS has no alert zone for {display} — it may be outside US coverage.)"
        return data
    features = data.get("features", []) if isinstance(data, dict) else []
    if not features:
        return f"No active severe-weather alerts for {display}."

    lines = []
    for f in features[:6]:
        p = f.get("properties") or {}
        event = p.get("event", "Alert")
        severity = p.get("severity", "")
        headline = (p.get("headline") or "").strip()
        lines.append(f"  {event}" + (f" [{severity}]" if severity else "")
                     + (f" — {headline}" if headline else ""))
    more = f"\n  (+{len(features) - 6} more)" if len(features) > 6 else ""
    return f"Active alerts for {display}:\n" + "\n".join(lines) + more


# --------------------------------------------------------------------------
# dictionary
# --------------------------------------------------------------------------
@register(
    "define_word",
    "Look up what a word means, how it's spelled, and its synonyms. Use this "
    "rather than defining it yourself — a definition invented from memory looks "
    "exactly like a real one.",
    {
        "type": "object",
        "properties": {
            "word": {"type": "string", "description": "The word to look up."},
        },
        "required": ["word"],
    },
    category="web_read",
    aliases=["what does obstreperous mean", "define serendipity",
             "how do you spell necessary", "another word for happy",
             "what's the meaning of that word", "is that even a real word"],
)
async def define_word(word: str) -> str:
    term = (word or "").strip().strip(".,?!\"'")
    if not term:
        return "(error: define_word needs a `word`.)"
    if len(term.split()) > 3:
        return f"(error: {word!r} is a phrase, not a word — I can only define single words.)"

    data = await _json(
        f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(term)}")
    if isinstance(data, str):
        if data == "__404__":
            return (f"No dictionary entry for {term!r}. Check the spelling — "
                    f"or it may be a proper noun or too new for the dictionary.")
        return data
    if not isinstance(data, list) or not data:
        return f"No dictionary entry for {term!r}."

    entry = data[0]
    out = [f"{entry.get('word', term)}"]
    if (phon := entry.get("phonetic")):
        out[0] += f"  {phon}"

    synonyms: set[str] = set()
    shown = 0
    for meaning in entry.get("meanings", []):
        if shown >= 3:
            break
        pos = meaning.get("partOfSpeech", "")
        defs = meaning.get("definitions", []) or []
        if not defs:
            continue
        first = defs[0]
        out.append(f"  ({pos}) {first.get('definition', '').strip()}")
        if (ex := (first.get("example") or "").strip()):
            out.append(f"      e.g. “{ex}”")
        synonyms.update(meaning.get("synonyms", [])[:6])
        shown += 1

    if synonyms:
        out.append("  synonyms: " + ", ".join(sorted(synonyms)[:8]))
    return "\n".join(out)


# --------------------------------------------------------------------------
# calling
# --------------------------------------------------------------------------
@register(
    "place_call",
    "Start a FaceTime or phone call to a contact or number. This PLACES the "
    "call through the Mac; it cannot answer, decline, or mute an incoming one — "
    "macOS exposes no API for those.",
    {
        "type": "object",
        "properties": {
            "to": {"type": "string",
                   "description": "Contact name or phone number to call."},
            "audio_only": {"type": "boolean",
                           "description": "Audio call rather than FaceTime video. Default true."},
        },
        "required": ["to"],
    },
    category="app_control",
    aliases=["call mom", "ring my dad", "facetime my sister",
             "give the dentist a call", "get her on the phone",
             "start a video call with alex"],
)
def place_call(to: str, audio_only: bool = True) -> str:
    who = (to or "").strip()
    if not who:
        return "(error: place_call needs someone `to` call.)"

    target = who
    # A name has to become a number before FaceTime can dial it. lookup_contact
    # already owns contact resolution, so reuse it rather than growing a second
    # implementation that can disagree with the first.
    if not any(ch.isdigit() for ch in who):
        try:
            from service.tools.imessage_tools import resolve_contact
            hit = resolve_contact(who)
            if isinstance(hit, str) and any(ch.isdigit() for ch in hit):
                target = hit
        except Exception:  # noqa: BLE001 — fall through to letting FaceTime resolve it
            pass

    scheme = "facetime-audio" if audio_only else "facetime"
    p = subprocess.run(["open", f"{scheme}://{quote(target)}"],
                       capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        return f"(could not start the call: {(p.stderr or '').strip()})"
    kind = "audio call" if audio_only else "FaceTime video call"
    return (f"Starting a {kind} to {who}. macOS will ask you to confirm before "
            f"it connects.")


# --------------------------------------------------------------------------
# Wisp's own state
# --------------------------------------------------------------------------
@register(
    "wisp_status",
    "Report Wisp's own state: which model is answering, what's loaded, and how "
    "much memory it's using. Use this for 'what model are you running', 'why "
    "are you slow', or 'are you using the cloud'.",
    {"type": "object", "properties": {}},
    category="wisp_admin",
    aliases=["what model are you running", "are you using the cloud",
             "why are you being slow today", "how much memory are you using",
             "which brain is answering me", "what version of you is this"],
)
async def wisp_status() -> str:
    from service.config import models_config, role_to_model

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
        from service.tools.registry import REGISTRY
        lines.append(f"Tools available: {len(REGISTRY)}")
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
    from service.tools.registry import REGISTRY

    names = set(REGISTRY)
    capabilities = [
        ("Send text messages", {"send_message"}, True),
        ("Draft text messages", {"draft_message"}, False),
        ("Send email", {"send_email"}, True),
        ("Create reminders", {"add_reminder"}, True),
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
        "No — inspect or understand the screen visually (Wisp has no vision model)",
        "No — access a bank balance unless a dedicated connected banking tool is installed",
    ]
    return (f"Wisp currently has {len(names)} registered tools. Complete capability summary"
            + (f" for {query!r}" if query.strip() else "") + ":\n" + "\n".join(rows))


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


@register(
    "wisp_sync",
    "Report when each data source (mail, messages, calendar, notes, "
    "contacts) last synced. Use for 'when did my email last sync', "
    "'is messages syncing'.",
    {"type": "object", "properties": {}},
    category="wisp_admin",
    aliases=["how fresh is your data right now", "when did you last sync everything",
             "how up to date is what you know"],
)
def wisp_sync() -> str:
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
