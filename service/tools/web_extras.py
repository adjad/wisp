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

`track_package` and `find_local_events` have no integrated provider. Their
compatibility registrations return one specific limitation and remain outside
normal routing rather than advertising nonexistent settings or guessing.
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


async def _geocode(place: str) -> tuple[float, float, str] | str:
    place = (place or "").strip()
    if not place:
        return "(no location given — ask the user which city, and do not guess one)"
    data = await _json("https://nominatim.openstreetmap.org/search",
                       params={"q": place, "format": "json", "limit": 1})
    if isinstance(data, str):
        return data
    if not data:
        return f"(couldn't find a place called {place!r}.)"
    hit = data[0]
    return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", place).split(",")[0]


# --------------------------------------------------------------------------
@register(
    "air_quality",
    "Current air quality index (AQI) and pollutant levels for a place. Use "
    "for 'is the air bad today', 'is it smoky', allergy/asthma planning.",
    {"type": "object",
     "properties": {"location": {"type": "string", "description": "City, address, or postcode."}},
     "required": ["location"]},
    category="web_read",
    aliases=["is the air quality bad today", "is it smoky outside",
             "should I wear a mask because of air quality", "how's the air today",
             "is it safe to run outside today"],
)
async def air_quality(location: str) -> str:
    loc = await _geocode(location)
    if isinstance(loc, str):
        return loc
    lat, lon, name = loc
    data = await _json("https://air-quality-api.open-meteo.com/v1/air-quality",
                       params={"latitude": lat, "longitude": lon,
                               "current": "us_aqi,pm2_5,pm10,ozone"})
    if isinstance(data, str):
        return data
    cur = (data.get("current") or {}) if isinstance(data, dict) else {}
    aqi = cur.get("us_aqi")
    if aqi is None:
        return f"(no air quality data available for {name}.)"
    band = ("Good" if aqi <= 50 else "Moderate" if aqi <= 100 else
            "Unhealthy for sensitive groups" if aqi <= 150 else
            "Unhealthy" if aqi <= 200 else "Very unhealthy" if aqi <= 300 else "Hazardous")
    return (f"{name}: AQI {aqi:.0f} ({band}). PM2.5 {cur.get('pm2_5', '?')}µg/m³, "
            f"PM10 {cur.get('pm10', '?')}µg/m³, ozone {cur.get('ozone', '?')}µg/m³.")


@register(
    "rain_radar",
    "Hour-by-hour precipitation forecast for the next few hours — 'is it "
    "about to rain', 'when will it stop raining'. This is a forecast readout, "
    "not a radar image (Wisp has no way to show a picture in chat).",
    {"type": "object",
     "properties": {
         "location": {"type": "string", "description": "City, address, or postcode."},
         "hours": {"type": "integer", "description": "How many hours ahead to cover. Default 6."},
     },
     "required": ["location"]},
    category="web_read",
    aliases=["is it about to rain", "when will it stop raining",
             "do I need an umbrella in the next hour", "is rain coming soon",
             "how's the rain looking for the next few hours"],
)
async def rain_radar(location: str, hours: int = 6) -> str:
    loc = await _geocode(location)
    if isinstance(loc, str):
        return loc
    lat, lon, name = loc
    try:
        hrs = max(1, min(24, int(hours)))
    except (TypeError, ValueError):
        hrs = 6
    data = await _json("https://api.open-meteo.com/v1/forecast",
                       params={"latitude": lat, "longitude": lon,
                               "hourly": "precipitation_probability,precipitation",
                               "forecast_hours": hrs, "timezone": "auto"})
    if isinstance(data, str):
        return data
    hourly = data.get("hourly") or {} if isinstance(data, dict) else {}
    times = hourly.get("time") or []
    prob = hourly.get("precipitation_probability") or []
    amt = hourly.get("precipitation") or []
    if not times:
        return f"(no hourly forecast available for {name}.)"
    lines = []
    for t, p, a in zip(times, prob, amt):
        hour = t.split("T")[-1][:5]
        lines.append(f"  {hour}  {p:>3}% chance" + (f", {a}mm" if a else ""))
    any_rain = any((p or 0) >= 40 for p in prob)
    lead = (f"Rain likely in the next {hrs}h in {name}:" if any_rain
            else f"Mostly dry in {name} for the next {hrs}h:")
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
             "tell me about the roman empire", "what's photosynthesis"],
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


@register(
    "get_sports_scores",
    "Live and recent scores for a team or league — NFL, NBA, MLB, NHL, "
    "Premier League, and other major leagues ESPN covers.",
    {"type": "object",
     "properties": {
         "team_or_league": {"type": "string",
                             "description": "A team name ('Warriors', 'Lakers') or league ('NBA', 'NFL', 'Premier League')."},
     },
     "required": ["team_or_league"]},
    category="web_read",
    aliases=["did the warriors win", "what's the score of the lakers game",
             "how'd the niners do", "any nba games tonight",
             "what's the premier league table looking like"],
)
async def get_sports_scores(team_or_league: str) -> str:
    query = (team_or_league or "").strip().lower()
    if not query:
        return "(error: get_sports_scores needs a team or league.)"

    leagues = {
        "nfl": "football/nfl", "nba": "basketball/nba", "nhl": "hockey/nhl",
        "mlb": "baseball/mlb", "ncaaf": "football/college-football",
        "ncaab": "basketball/mens-college-basketball",
        "premier league": "soccer/eng.1", "epl": "soccer/eng.1",
        "la liga": "soccer/esp.1", "champions league": "soccer/uefa.champions",
        "mls": "soccer/usa.1",
    }
    team_leagues = {
        "warriors": "nba", "lakers": "nba", "celtics": "nba", "knicks": "nba",
        "niners": "nfl", "49ers": "nfl", "chiefs": "nfl", "cowboys": "nfl",
        "eagles": "nfl", "packers": "nfl", "yankees": "mlb", "dodgers": "mlb",
        "giants": "mlb", "red sox": "mlb",
    }
    # `team_name` tracks whether a SPECIFIC team was asked about, separately
    # from `league_key` (which is only ever a league to query). Conflating the
    # two was a real bug: "did the warriors win" with no Warriors game today
    # silently fell through to dumping the ENTIRE NBA scoreboard under a bare
    # "NBA:" header — Heat @ Raptors, nothing to do with the Warriors — which
    # reads as an answer about the Warriors' game if you're not looking
    # closely. A team that was actually named and not found must say so.
    team_name: str | None = None
    if query in leagues:
        league_key = query
    elif query in team_leagues:
        league_key, team_name = team_leagues[query], query
    else:
        team_name = next((k for k in team_leagues if k in query), None)
        league_key = team_leagues.get(team_name) if team_name else None
    if league_key is None:
        return (f"(I don't recognize {team_or_league!r} as a team or league I "
                f"can look up. Try naming the league, e.g. 'NBA' or 'Premier League'.)")
    path = leagues[league_key]

    data = await _json(f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard")
    if isinstance(data, str):
        return data
    events = data.get("events", []) if isinstance(data, dict) else []
    if not events:
        return f"No {league_key.upper()} games scheduled right now."

    lines = []
    for ev in events[:8]:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) != 2:
            continue
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
        status = (comp.get("status") or {}).get("type", {}).get("shortDetail", "")
        line = (f"  {away['team']['displayName']} {away.get('score', '-')} @ "
               f"{home['team']['displayName']} {home.get('score', '-')}  [{status}]")
        lines.append(line)
        if team_name and team_name in " ".join(
                c["team"].get("displayName", "") for c in competitors).lower():
            return line.strip()

    if team_name:
        return f"No {team_name.title()} game found today ({league_key.upper()})."
    return f"{league_key.upper()}:\n" + "\n".join(lines)


@register(
    "recipe_lookup",
    "Find a recipe by dish name or by an ingredient to use up — ingredients "
    "and instructions. Use for 'how do I make X' or 'what can I cook with Y'.",
    {"type": "object",
     "properties": {
         "dish": {"type": "string", "description": "A dish name, e.g. 'carbonara'."},
         "ingredient": {"type": "string", "description": "An ingredient to search recipes by, e.g. 'chicken'."},
     }},
    category="web_read",
    aliases=["how do I make carbonara", "what can I cook with chicken and rice",
             "give me a recipe for banana bread", "what's a good use for leftover ham",
             "how do you make lasagna from scratch"],
)
async def recipe_lookup(dish: str = "", ingredient: str = "") -> str:
    dish, ingredient = dish.strip(), ingredient.strip()
    if not dish and not ingredient:
        return "(error: recipe_lookup needs a `dish` name or an `ingredient`.)"

    if dish:
        data = await _json("https://www.themealdb.com/api/json/v1/1/search.php",
                           params={"s": dish})
        meals = (data.get("meals") if isinstance(data, dict) else None) or []
        if not meals:
            return f"No recipe found for {dish!r}."
        meal = meals[0]
        ingredients = []
        for i in range(1, 21):
            name = (meal.get(f"strIngredient{i}") or "").strip()
            amount = (meal.get(f"strMeasure{i}") or "").strip()
            if name:
                ingredients.append(f"{amount} {name}".strip())
        instructions = (meal.get("strInstructions") or "").strip()
        return (f"{meal.get('strMeal', dish)} ({meal.get('strArea', '')} "
                f"{meal.get('strCategory', '')})\n\nIngredients:\n"
                + "\n".join(f"  - {i}" for i in ingredients)
                + f"\n\nInstructions:\n{instructions[:1200]}"
                + ("..." if len(instructions) > 1200 else ""))

    data = await _json("https://www.themealdb.com/api/json/v1/1/filter.php",
                       params={"i": ingredient})
    meals = (data.get("meals") if isinstance(data, dict) else None) or []
    if not meals:
        return f"No recipes found using {ingredient!r}."
    names = ", ".join(m.get("strMeal", "?") for m in meals[:10])
    return f"Recipes using {ingredient}: {names}. Ask for one by name for the full recipe."


# --------------------------------------------------------------------------
# Key-required — registered honestly, not faked. See module docstring.
# --------------------------------------------------------------------------
@register(
    "track_package",
    "Track a shipment by tracking number. UNAVAILABLE: Wisp has no integrated "
    "carrier-tracking provider, so no lookup runs and no status is guessed.",
    {"type": "object",
     "properties": {"tracking_number": {"type": "string", "description": "The carrier tracking number."}},
     "required": ["tracking_number"]},
    category="web_read",
    aliases=["where's my package", "track this delivery",
             "has my amazon order shipped", "when will my package arrive"],
)
async def track_package(tracking_number: str) -> str:
    from service.tools.registry import UNAVAILABLE_TOOL_REASONS
    return UNAVAILABLE_TOOL_REASONS["track_package"]


@register(
    "find_local_events",
    "Find events happening nearby — concerts, shows, local happenings. "
    "UNAVAILABLE: Wisp has no integrated local-events provider, so no lookup "
    "runs and no events are guessed.",
    {"type": "object",
     "properties": {
         "near": {"type": "string", "description": "City or area to search."},
         "what": {"type": "string", "description": "Optional: type of event, e.g. 'concerts', 'comedy'."},
     },
     "required": ["near"]},
    category="web_read",
    aliases=["find local events happening nearby", "any concerts nearby",
             "what events are going on downtown", "anything fun to do this weekend"],
)
async def find_local_events(near: str, what: str = "") -> str:
    from service.tools.registry import UNAVAILABLE_TOOL_REASONS
    return UNAVAILABLE_TOOL_REASONS["find_local_events"]
