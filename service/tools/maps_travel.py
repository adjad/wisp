"""Places, routes and travel time — keyless, over OpenStreetMap.

WHY THESE ARE TOOLS AND NOT `web_fetch`
---------------------------------------
The 2026-08-18 diagnosis recorded what happens without them: weather went
through `web_fetch` with hand-built URLs and the model "invented a city once,
resolved 'Dublin' to Ireland once, reported live conditions as a forecast once."
Every one of those is the same defect — the model assembling a URL from a guess.
A place lookup has exactly the same shape, so it gets the same treatment: the
tool owns the URL, and the answer names the place it actually resolved to so a
wrong match is visible rather than silent.

WHAT KEYLESS COSTS, STATED PLAINLY
----------------------------------
Nominatim (geocoding), Overpass (places) and OSRM (routing) are free and need no
account, which is why they are here. The price is that OSRM routes on speed
limits, NOT live traffic. "How long to the airport" is therefore a
free-flow estimate, and `travel_time` says so in its output rather than letting
the user plan around a number that silently ignores a jam. Live traffic needs a
keyed provider; that is a `Keyless-first + optional user keys` decision recorded
in the plan, not an oversight.

Nominatim's usage policy requires an identifying User-Agent and at most ~1
request/second. Both are honored below.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import httpx

from service.tools.registry import register

_UA = "Wisp/1.0 (local macOS assistant; personal use)"
_TIMEOUT = 12

# Nominatim asks for <= 1 req/s. This is a single-user desktop assistant so a
# plain module-level gate is sufficient — there is no second worker to race.
_last_geocode = 0.0
_geocode_lock = asyncio.Lock()


async def _get(url: str, *, params: dict | None = None) -> dict | list | str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as c:
            r = await c.get(url, params=params, headers={"User-Agent": _UA})
    except httpx.TimeoutException:
        return "(the mapping service timed out — try again in a moment)"
    except httpx.HTTPError as e:
        return f"(could not reach the mapping service: {e})"
    if r.status_code >= 400:
        return f"(mapping service returned HTTP {r.status_code})"
    try:
        return r.json()
    except ValueError:
        return "(the mapping service returned something unreadable)"


async def _geocode(place: str) -> tuple[float, float, str] | str:
    """Place name -> (lat, lon, display name), or an error string."""
    global _last_geocode
    place = (place or "").strip()
    if not place:
        return "(no place given)"
    async with _geocode_lock:
        gap = time.monotonic() - _last_geocode
        if gap < 1.0:
            await asyncio.sleep(1.0 - gap)
        _last_geocode = time.monotonic()
        data = await _get("https://nominatim.openstreetmap.org/search",
                          params={"q": place, "format": "json", "limit": 1})
    if isinstance(data, str):
        return data
    if not data:
        return (f"(couldn't find a place called {place!r}. Try adding a city or "
                f"country — 'Dublin, CA' rather than 'Dublin'.)")
    hit = data[0]
    return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", place)


def _km(metres: float) -> str:
    miles = metres / 1609.344
    return f"{miles:.1f} mi ({metres / 1000:.1f} km)"


def _mins(seconds: float) -> str:
    total = int(round(seconds / 60))
    if total < 60:
        return f"{total} min"
    h, m = divmod(total, 60)
    return f"{h}h {m:02d}m"


@register(
    "find_place",
    "Find places near a location — restaurants, coffee, petrol stations, "
    "pharmacies, ATMs, and so on. Returns names with distance and address. "
    "`near` must name a real place; if you don't know where the user is, ASK "
    "rather than guessing a city.",
    {
        "type": "object",
        "properties": {
            "what": {"type": "string",
                     "description": "What to look for, e.g. 'coffee', 'pharmacy', 'petrol station'."},
            "near": {"type": "string",
                     "description": "Where to search around — a city, address, or postcode."},
            "radius_km": {"type": "number",
                          "description": "How far out to look, in km. Default 3, max 20."},
        },
        "required": ["what", "near"],
    },
    category="web_read",
    aliases=["where's the nearest coffee shop", "find me somewhere to eat around here",
             "is there a pharmacy nearby", "closest petrol station",
             "any good restaurants near the office", "where can I get cash out"],
)
async def find_place(what: str, near: str, radius_km: float = 3.0) -> str:
    what = (what or "").strip()
    if not what:
        return "(error: find_place needs `what` to look for.)"
    loc = await _geocode(near)
    if isinstance(loc, str):
        return loc
    lat, lon, display = loc

    try:
        radius = max(0.2, min(20.0, float(radius_km)))
    except (TypeError, ValueError):
        radius = 3.0
    metres = int(radius * 1000)

    # Map plain words onto OSM tags. An unmapped word falls back to a name
    # search, which is worse but still useful — better than refusing.
    tags = {
        "coffee": 'amenity=cafe', "cafe": 'amenity=cafe',
        "restaurant": 'amenity=restaurant', "food": 'amenity=restaurant',
        "eat": 'amenity=restaurant', "dinner": 'amenity=restaurant',
        "lunch": 'amenity=restaurant', "bar": 'amenity=bar', "pub": 'amenity=pub',
        "pharmacy": 'amenity=pharmacy', "chemist": 'amenity=pharmacy',
        "hospital": 'amenity=hospital', "doctor": 'amenity=doctors',
        "atm": 'amenity=atm', "bank": 'amenity=bank',
        "petrol": 'amenity=fuel', "gas": 'amenity=fuel', "fuel": 'amenity=fuel',
        "gas station": 'amenity=fuel', "petrol station": 'amenity=fuel',
        "parking": 'amenity=parking', "supermarket": 'shop=supermarket',
        "grocery": 'shop=supermarket', "groceries": 'shop=supermarket',
        "hotel": 'tourism=hotel', "gym": 'leisure=fitness_centre',
        "park": 'leisure=park', "library": 'amenity=library',
        "post office": 'amenity=post_office', "vet": 'amenity=veterinary',
        "charging": 'amenity=charging_station', "ev charger": 'amenity=charging_station',
    }
    key = what.lower().strip()
    selector = tags.get(key)
    if selector is None:
        selector = next((v for k, v in tags.items() if k in key), None)
    if selector:
        clause = (f'node[{selector}](around:{metres},{lat},{lon});'
                  f'way[{selector}](around:{metres},{lat},{lon});')
    else:
        esc = key.replace('"', '')
        clause = (f'node[name~"{esc}",i](around:{metres},{lat},{lon});'
                  f'way[name~"{esc}",i](around:{metres},{lat},{lon});')

    query = f"[out:json][timeout:20];({clause});out center 25;"
    data = await _get("https://overpass-api.de/api/interpreter",
                      params={"data": query})
    if isinstance(data, str):
        return data
    elements = data.get("elements", []) if isinstance(data, dict) else []
    if not elements:
        return (f"Nothing matching {what!r} within {radius:.0f}km of "
                f"{display.split(',')[0]}. Try a wider `radius_km`.")

    import math

    rows = []
    for el in elements:
        t = el.get("tags") or {}
        name = t.get("name")
        if not name:
            continue
        elat = el.get("lat") or (el.get("center") or {}).get("lat")
        elon = el.get("lon") or (el.get("center") or {}).get("lon")
        if elat is None or elon is None:
            continue
        # Equirectangular approximation — at these radii the error is metres,
        # and it avoids a trig-heavy haversine per element.
        dx = math.radians(float(elon) - lon) * math.cos(math.radians(lat)) * 6371000
        dy = math.radians(float(elat) - lat) * 6371000
        dist = math.hypot(dx, dy)
        addr = " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street")]))
        rows.append((dist, name, addr))

    if not rows:
        return f"Found places near {display.split(',')[0]} but none had names on record."
    rows.sort()
    lines = [f"{name} — {dist / 1000:.1f}km" + (f", {addr}" if addr else "")
             for dist, name, addr in rows[:10]]
    return (f"Near {display.split(',')[0]}:\n" + "\n".join(f"  {ln}" for ln in lines))


@register(
    "travel_time",
    "How far and how long it takes to DRIVE from one place to another. NOTE: "
    "this is a free-flow estimate from speed limits — it does NOT account for "
    "live traffic, and the answer says so. Driving only; for walking or transit "
    "times use get_directions, which opens Apple Maps.",
    {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "Starting point — address, city, or postcode."},
            "destination": {"type": "string", "description": "Where to."},
        },
        "required": ["origin", "destination"],
    },
    category="web_read",
    aliases=["how long to get to the airport", "how far is it to portland",
             "what's the drive time from here to my parents",
             "how long is the drive to tahoe", "how many miles to the coast"],
)
async def travel_time(origin: str, destination: str) -> str:
    # DRIVING ONLY, and the mode parameter is gone rather than ignored.
    #
    # The public OSRM demo server hosts the car profile and NOTHING else: a
    # request to /foot/ or /bike/ returns the car route, byte-identical, with no
    # error. Measured on SF -> Oakland, all three profiles gave "20 min, 18.1
    # km". Keeping a `mode` argument would therefore have reported a 20-minute
    # DRIVE as a 20-minute WALK, confidently — the precise failure class this
    # module's docstring cites (invented cities, forecasts reported as current
    # conditions), and worse than not offering the feature.
    #
    # Apple Maps does route all modes properly, so get_directions covers them.
    mode = "driving"
    profile = "car"

    a = await _geocode(origin)
    if isinstance(a, str):
        return a
    b = await _geocode(destination)
    if isinstance(b, str):
        return b
    (alat, alon, aname), (blat, blon, bname) = a, b

    url = (f"https://router.project-osrm.org/route/v1/{profile}/"
           f"{alon},{alat};{blon},{blat}")
    data = await _get(url, params={"overview": "false"})
    if isinstance(data, str):
        return data
    routes = data.get("routes") if isinstance(data, dict) else None
    if not routes:
        return (f"No {mode} route found between {aname.split(',')[0]} and "
                f"{bname.split(',')[0]}.")
    route = routes[0]
    dist, dur = route.get("distance", 0), route.get("duration", 0)
    return (f"{aname.split(',')[0]} to {bname.split(',')[0]} by car: "
            f"{_mins(dur)}, {_km(dist)} "
            f"(free-flow estimate — live traffic not included).")


@register(
    "get_directions",
    "Open turn-by-turn directions in Apple Maps. Use this when the user wants "
    "to actually navigate somewhere; use travel_time when they only want to "
    "know how long it takes.",
    {
        "type": "object",
        "properties": {
            "destination": {"type": "string", "description": "Where they're going."},
            "origin": {"type": "string",
                       "description": "Optional starting point. Defaults to current location."},
            "mode": {"type": "string", "enum": ["driving", "walking", "transit"],
                     "description": "Travel mode. Default driving."},
        },
        "required": ["destination"],
    },
    category="app_control",
    aliases=["give me directions to the airport", "navigate me home",
             "how do I get to the museum from here", "map me to the office",
             "show me the route there"],
)
def get_directions(destination: str, origin: str = "", mode: str = "driving") -> str:
    import subprocess

    destination = (destination or "").strip()
    if not destination:
        return "(error: get_directions needs a `destination`.)"
    flag = {"driving": "d", "walking": "w", "transit": "r"}.get(
        (mode or "driving").strip().lower(), "d")
    url = f"maps://?daddr={quote(destination)}&dirflg={flag}"
    if origin.strip():
        url += f"&saddr={quote(origin.strip())}"
    p = subprocess.run(["open", url], capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        return f"(could not open Maps: {(p.stderr or '').strip()})"
    from_txt = f" from {origin.strip()}" if origin.strip() else ""
    return f"Opened {mode} directions to {destination}{from_txt} in Maps."


@register(
    "transit_info",
    "Live public-transit departure times and delays for a specific line or "
    "stop. Requires a free API key from a transit data provider the user "
    "hasn't configured (coverage is city-specific, so there's no single "
    "keyless source) — until then this reports that plainly. For turn-by-turn "
    "transit ROUTING (not live departures), use get_directions with "
    "mode='transit' instead, which already works via Apple Maps.",
    {"type": "object",
     "properties": {"query": {"type": "string", "description": "Line/route or stop name."}},
     "required": ["query"]},
    category="web_read",
    aliases=["when's the next train", "is my bus running late",
             "what time does the next train leave", "check transit delays"],
)
def transit_info(query: str) -> str:
    return ("Live transit departure times need a city-specific transit API "
            "key that isn't configured yet — add one in Wisp's settings "
            "under API keys to enable this. For directions right now, ask "
            "for transit directions instead — that opens Apple Maps and "
            "works without a key.")


@register(
    "track_flight",
    "Live flight status — gate, delays, departure/arrival times. Requires a "
    "free API key from a flight-data service the user hasn't configured yet "
    "— until then this reports that plainly.",
    {"type": "object",
     "properties": {"flight_number": {"type": "string", "description": "e.g. 'UA123'."}},
     "required": ["flight_number"]},
    category="web_read",
    aliases=["is my flight on time", "what gate does my flight leave from",
             "check the status of flight UA123", "is my flight delayed"],
)
def track_flight(flight_number: str) -> str:
    return ("Flight tracking needs a free API key (e.g. AeroDataBox) that "
            "isn't configured yet — add one in Wisp's settings under API "
            "keys to enable this.")
