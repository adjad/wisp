"""Time, dates and time zones — computed locally, never recalled.

WHY THE CLOCK NEEDS A TOOL
--------------------------
A language model has no clock. Asked "what time is it", it will produce a
plausible time with complete confidence, and there is nothing in the answer to
indicate it was invented — the same class of failure as the arithmetic case in
`conversions.py`, and just as easy to eliminate.

Time-zone conversion has the same property plus a second trap: DST. "What time
is it in London" is not a fixed offset from here, it depends on the date and on
two separate DST calendars. `zoneinfo` reads the IANA database that ships with
the OS, so this is exact and needs no network.
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo, available_timezones

from service.tools.registry import register

# Cities people name, mapped to IANA zones. `available_timezones()` covers the
# formal names ("Europe/London"); this covers how anyone actually asks.
_CITIES = {
    "london": "Europe/London", "paris": "Europe/Paris", "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome", "amsterdam": "Europe/Amsterdam",
    "dublin": "Europe/Dublin", "lisbon": "Europe/Lisbon", "zurich": "Europe/Zurich",
    "stockholm": "Europe/Stockholm", "oslo": "Europe/Oslo", "moscow": "Europe/Moscow",
    "istanbul": "Europe/Istanbul", "athens": "Europe/Athens",
    "new york": "America/New_York", "nyc": "America/New_York",
    "boston": "America/New_York", "washington": "America/New_York",
    "miami": "America/New_York", "atlanta": "America/New_York",
    "toronto": "America/Toronto", "montreal": "America/Toronto",
    "chicago": "America/Chicago", "dallas": "America/Chicago",
    "houston": "America/Chicago", "austin": "America/Chicago",
    "denver": "America/Denver", "phoenix": "America/Phoenix",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "sf": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "vancouver": "America/Vancouver",
    "mexico city": "America/Mexico_City", "sao paulo": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "tokyo": "Asia/Tokyo", "osaka": "Asia/Tokyo", "seoul": "Asia/Seoul",
    "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong", "singapore": "Asia/Singapore",
    "bangkok": "Asia/Bangkok", "jakarta": "Asia/Jakarta",
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "dubai": "Asia/Dubai", "tel aviv": "Asia/Jerusalem",
    "jerusalem": "Asia/Jerusalem", "karachi": "Asia/Karachi",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane", "perth": "Australia/Perth",
    "auckland": "Pacific/Auckland",
    "johannesburg": "Africa/Johannesburg", "cairo": "Africa/Cairo",
    "lagos": "Africa/Lagos", "nairobi": "Africa/Nairobi",
    "honolulu": "Pacific/Honolulu", "anchorage": "America/Anchorage",
    "utc": "UTC", "gmt": "UTC",
}


def _resolve_zone(place: str) -> ZoneInfo | None:
    key = re.sub(r"\s+", " ", (place or "").strip().lower())
    key = re.sub(r"^(?:in|at|the)\s+", "", key)
    if not key:
        return None
    if key in _CITIES:
        return ZoneInfo(_CITIES[key])
    # An exact IANA name, however the user cased it.
    for zone in available_timezones():
        if zone.lower() == key:
            return ZoneInfo(zone)
    # Last resort: the city segment of an IANA name ("Kolkata" -> Asia/Kolkata).
    want = key.replace(" ", "_")
    for zone in sorted(available_timezones()):
        if zone.lower().rsplit("/", 1)[-1] == want:
            return ZoneInfo(zone)
    return None


def _fmt(when: dt.datetime) -> str:
    return when.strftime("%-I:%M %p on %A, %B %-d, %Y")


def _label(place: str, zone: ZoneInfo) -> str:
    """Display name for a place, taken from the RESOLVED zone rather than what
    the user typed. `"sf".title()` is "Sf"; the zone's own last segment is
    "Los Angeles", which is both correct and what they meant."""
    city = str(zone).rsplit("/", 1)[-1].replace("_", " ")
    return city if city.upper() == "UTC" else city.title()


@register(
    "world_time",
    "Get the current date and time — here, or in another city. Use this for "
    "ANY 'what time is it', 'what's today's date', or 'what time is it in X' "
    "question rather than answering from memory; you have no clock of your own. "
    "Also computes the difference between two places when both are given.",
    {
        "type": "object",
        "properties": {
            "place": {"type": "string",
                      "description": "City or IANA zone to report, e.g. 'Tokyo' or 'Europe/London'. Omit for local time."},
            "compare_to": {"type": "string",
                           "description": "Optional second place — returns the offset between the two."},
        },
    },
    category="compute",
    aliases=["what time is it", "what's today's date", "what day is it",
             "what time is it in tokyo right now", "how far ahead is london",
             "is it still monday in california", "what's the time difference with india"],
)
def world_time(place: str = "", compare_to: str = "") -> str:
    local_now = dt.datetime.now().astimezone()

    if not place and not compare_to:
        return f"It's {_fmt(local_now)} ({local_now.tzname()})."

    if place and compare_to:
        a, b = _resolve_zone(place), _resolve_zone(compare_to)
        if a is None:
            return f"(error: I don't know a place called {place!r}.)"
        if b is None:
            return f"(error: I don't know a place called {compare_to!r}.)"
        now_a = local_now.astimezone(a)
        now_b = local_now.astimezone(b)
        # Compare the UTC offsets at THIS instant, not the zones' nominal
        # offsets — that is what makes the answer correct across a DST boundary
        # where one side has changed over and the other hasn't yet.
        delta = (now_a.utcoffset() or dt.timedelta()) - (now_b.utcoffset() or dt.timedelta())
        hours = delta.total_seconds() / 3600
        if hours == 0:
            rel = "the same time as"
        else:
            mag = f"{abs(hours):.0f}" if abs(hours) == int(abs(hours)) else f"{abs(hours):.1f}"
            rel = f"{mag} hour{'s' if abs(hours) != 1 else ''} {'ahead of' if hours > 0 else 'behind'}"
        la, lb = _label(place, a), _label(compare_to, b)
        return (f"{la} is {rel} {lb}.\n"
                f"  {la}: {_fmt(now_a)}\n"
                f"  {lb}: {_fmt(now_b)}")

    target = place or compare_to
    zone = _resolve_zone(target)
    if zone is None:
        return (f"(error: I don't know a place called {target!r}. "
                f"Try a major city or an IANA zone like 'Europe/London'.)")
    there = local_now.astimezone(zone)
    delta = ((there.utcoffset() or dt.timedelta())
             - (local_now.utcoffset() or dt.timedelta())).total_seconds() / 3600
    if delta == 0:
        rel = "same as here"
    else:
        mag = f"{abs(delta):.0f}" if abs(delta) == int(abs(delta)) else f"{abs(delta):.1f}"
        rel = f"{mag}h {'ahead of' if delta > 0 else 'behind'} you"
    return f"In {_label(target, zone)} it's {_fmt(there)} ({rel})."


@register(
    "random_pick",
    "Pick randomly from a list, or roll dice, or flip a coin — using real "
    "randomness (`secrets`), never the model's own guess. The SYSTEM rule is "
    "explicit that model-generated 'random' text is not actually random.",
    {
        "type": "object",
        "properties": {
            "options": {"type": "array", "items": {"type": "string"},
                       "description": "Things to choose from, e.g. ['pizza','sushi','tacos']. Omit for a coin flip."},
            "count": {"type": "integer", "description": "How many to pick (no repeats). Default 1."},
        },
    },
    category="compute",
    aliases=["pick one of these for me randomly", "flip a coin",
             "roll a die", "randomly choose between pizza and sushi",
             "pick a random number between 1 and 10"],
)
def random_pick(options: list[str] | None = None, count: int = 1) -> str:
    import secrets as _secrets

    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if not opts:
        return "Heads" if _secrets.randbelow(2) else "Tails"
    try:
        n = max(1, min(len(opts), int(count)))
    except (TypeError, ValueError):
        n = 1
    picked = []
    pool = list(opts)
    for _ in range(n):
        i = _secrets.randbelow(len(pool))
        picked.append(pool.pop(i))
    return picked[0] if n == 1 else ", ".join(picked)


@register(
    "astronomy",
    "Sunrise, sunset, and day length for a place. Use for 'what time does "
    "the sun set', 'how much daylight today'.",
    {"type": "object",
     "properties": {"location": {"type": "string", "description": "City, address, or postcode. Defaults to today, here, if omitted."}},
     "required": ["location"]},
    category="web_read",
    aliases=["what time does the sun set today", "when's sunrise tomorrow",
             "how much daylight do we have today", "what time is golden hour"],
)
async def astronomy(location: str) -> str:
    import httpx

    place = (location or "").strip()
    if not place:
        return "(no location given — ask the user which city, and do not guess one)"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            geo = await c.get("https://nominatim.openstreetmap.org/search",
                              params={"q": place, "format": "json", "limit": 1},
                              headers={"User-Agent": "Wisp/1.0 (local macOS assistant; personal use)"})
    except httpx.HTTPError as e:
        return f"(could not reach the mapping service: {e})"
    hits = geo.json() if geo.status_code == 200 else []
    if not hits:
        return f"(couldn't find a place called {place!r}.)"
    lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
    name = hits[0].get("display_name", place).split(",")[0]

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.open-meteo.com/v1/forecast",
                            params={"latitude": lat, "longitude": lon,
                                    "daily": "sunrise,sunset", "timezone": "auto"})
    except httpx.HTTPError as e:
        return f"(could not reach the weather service: {e})"
    if r.status_code >= 400:
        return f"(weather service returned HTTP {r.status_code})"
    daily = r.json().get("daily") or {}
    sunrises, sunsets = daily.get("sunrise") or [], daily.get("sunset") or []
    if not sunrises or not sunsets:
        return f"(no sunrise/sunset data available for {name}.)"

    import datetime as _dt

    up = _dt.datetime.fromisoformat(sunrises[0])
    down = _dt.datetime.fromisoformat(sunsets[0])
    length = down - up
    hours, minutes = divmod(int(length.total_seconds() // 60), 60)
    return (f"{name}: sunrise {up.strftime('%-I:%M %p')}, sunset "
            f"{down.strftime('%-I:%M %p')} — {hours}h {minutes}m of daylight.")


@register(
    "country_info",
    "Basic facts about a country — capital, population, currency, languages. "
    "Requires a free API key the user hasn't configured yet (restcountries.com "
    "moved its free tier behind registration) — until then this reports that "
    "plainly.",
    {"type": "object",
     "properties": {"country": {"type": "string", "description": "Country name."}},
     "required": ["country"]},
    category="web_read",
    aliases=["what's the capital of portugal", "what language do they speak in brazil",
             "what currency does japan use", "how many people live in vietnam"],
)
def country_info(country: str) -> str:
    return ("Country lookups need a free API key (restcountries.com moved "
            "its free tier behind registration) that isn't configured yet — "
            "add one in Wisp's settings under API keys to enable this.")
