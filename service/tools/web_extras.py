"""More keyless web-data tools, same shape as maps_travel.py and misc_t1.py:
the tool owns the URL and names the place it actually resolved to, because a
model that hand-builds a URL is the exact failure the 2026-08-18 diagnosis
measured (weather via web_fetch inventing a city, resolving 'Dublin' to
Ireland, reporting live conditions as a forecast).

WHAT'S DELIBERATELY NOT HERE
-----------------------------
`fact_check` (Atlas T2 row) isn't a tool. Fact-checking a claim is a REASONING
task over retrieved sources, not a lookup with one right answer — the Atlas's
own T3 "Fact-check a claim" note says exactly this ("web verification").
web_fetch + the model's own reasoning already covers it; a dedicated tool
would just be a worse web_fetch with extra steps, the same reasoning that
dropped search_email/list_reminders/translate_text in Phase 1.
"""
from __future__ import annotations

import httpx

from service.tools.registry import register

_UA = "Wisp/1.0 (local macOS assistant; personal use)"
_TIMEOUT = 12

# Wikimedia's edge specifically enforces their User-Agent policy (a plain
# descriptive string like the one above gets a 403 with "please respect our
# robot policy", from httpx though NOT from curl with the identical string —
# this is UA-content matching, not a generic bot-fingerprint block). Their
# policy wants an identifying string with a contact URL or email. Wisp has no
# public homepage to point at truthfully, so this says exactly that —
# "no public homepage" is right in the string — rather than inventing a
# project URL or repo that doesn't exist, which would be the wrong kind of
# fix for the wrong reason.
_WIKIMEDIA_UA = ("Wisp/1.0 (https://localhost/wisp-personal-assistant, no "
                 "public homepage; local single-user tool)")


async def _json(url: str, *, params: dict | None = None,
                headers: dict | None = None) -> dict | list | str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as c:
            r = await c.get(url, params=params, headers={"User-Agent": _UA, **(headers or {})})
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


async def air_quality_at(lat: float, lon: float, name: str) -> str:
    """Current US AQI and pollutants at coordinates get_weather already resolved."""
    data = await _json("https://air-quality-api.open-meteo.com/v1/air-quality",
                       params={"latitude": lat, "longitude": lon,
                               "current": "us_aqi,pm2_5,pm10,ozone"})
    if isinstance(data, str):
        return "(air quality service unavailable)" if data == "__404__" else data
    cur = (data.get("current") or {}) if isinstance(data, dict) else {}
    aqi = cur.get("us_aqi")
    if not isinstance(aqi, (int, float)) or isinstance(aqi, bool):
        return f"(no air quality data available for {name}.)"
    band = ("Good" if aqi <= 50 else "Moderate" if aqi <= 100 else
            "Unhealthy for sensitive groups" if aqi <= 150 else
            "Unhealthy" if aqi <= 200 else "Very unhealthy" if aqi <= 300 else "Hazardous")
    return (f"AQI {aqi:.0f} ({band}). PM2.5 {cur.get('pm2_5', '?')}µg/m³, "
            f"PM10 {cur.get('pm10', '?')}µg/m³, ozone {cur.get('ozone', '?')}µg/m³.")


async def rain_next_hours_at(lat: float, lon: float, name: str, hours: int = 6) -> str:
    """Hour-by-hour precipitation chance at coordinates get_weather already resolved.

    A forecast readout, not a radar image (Wisp cannot show a picture in chat).
    """
    hrs = max(1, min(24, int(hours)))
    data = await _json("https://api.open-meteo.com/v1/forecast",
                       params={"latitude": lat, "longitude": lon,
                               "hourly": "precipitation_probability,precipitation",
                               "forecast_hours": hrs, "timezone": "auto"})
    if isinstance(data, str):
        return "(hourly forecast service unavailable)" if data == "__404__" else data
    hourly = data.get("hourly") or {} if isinstance(data, dict) else {}
    times = hourly.get("time") or []
    prob = hourly.get("precipitation_probability") or []
    amt = hourly.get("precipitation") or []
    if not times:
        return f"(no hourly forecast available for {name}.)"
    lines = []
    for t, p, a in zip(times, prob, amt):
        hour = str(t).split("T")[-1][:5]
        lines.append(f"  {hour}  {p:>3}% chance" + (f", {a}mm" if a else ""))
    any_rain = any((p or 0) >= 40 for p in prob)
    lead = (f"Rain likely in the next {hrs}h:" if any_rain
            else f"Mostly dry for the next {hrs}h:")
    return lead + "\n" + "\n".join(lines)


@register(
    "wikipedia_summary",
    "Get a short factual summary of a topic from Wikipedia. Use for 'who is "
    "X', 'what is X', general-knowledge lookups — more reliable than "
    "answering from memory for anything checkable.",
    {"type": "object",
     "properties": {"topic": {"type": "string", "description": "The person, place, or thing to look up."}},
     "required": ["topic"]},
    category="web_read",
    aliases=["who is marie curie", "what is the eiffel tower",
             "give me a quick summary of quantum computing",
             "tell me about the roman empire", "what's photosynthesis",
             # Phrasings the retired define_word tool used to answer.
             "what does obstreperous mean", "define serendipity",
             "what's the meaning of that word"],
)
async def wikipedia_summary(topic: str) -> str:
    term = (topic or "").strip()
    if not term:
        return "(error: wikipedia_summary needs a `topic`.)"
    data = await _json(
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{term.replace(' ', '_')}",
        headers={"User-Agent": _WIKIMEDIA_UA})
    if isinstance(data, str):
        if data == "__404__":
            return (f"No Wikipedia article found for {term!r}. Try a more "
                    f"specific or differently-spelled name.")
        return data
    if not isinstance(data, dict):
        return f"No Wikipedia article found for {term!r}."
    title = data.get("title", term)
    extract = (data.get("extract") or "").strip()
    if not extract:
        return f"Found {title!r} on Wikipedia but it has no summary text."
    disambiguation = data.get("type") == "disambiguation"
    note = "\n(This name is ambiguous — ask for a more specific topic.)" if disambiguation else ""
    return f"{title}: {extract}{note}"


# --------------------------------------------------------------------------
# Key-required — registered honestly, not faked. See module docstring.
# --------------------------------------------------------------------------
