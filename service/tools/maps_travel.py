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
the user plan around a number that silently ignores a jam. `route_to_arrive`
defines a separate native Apple Maps expected-traffic provider contract; its
default is unavailable until the app's native adapter is connected.

Nominatim's usage policy requires an identifying User-Agent and at most ~1
request/second. Both are honored below.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
                f"country — 'Paris, TX' rather than 'Paris'.)")
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


# Native MapKit is deliberately not connected by this provider contract alone.
# The eventual app adapter must use MKDirections.calculateETA: ETAResponse's
# expectedTravelTime considers expected traffic; MKRoute's value does not.
_ARRIVAL_MAX_AGE_S = 300
_ARRIVAL_MAX_LOCATION_AGE_S = 120
_ARRIVAL_MAX_LOCATION_ACCURACY_M = 250


def _arrival_now() -> datetime:
    return datetime.now(datetime_timezone.utc)


async def _arrival_provider(request: dict) -> dict:
    """Injected native read provider. No native call or location permission yet.

    Success contract: status=ok, request_id, provider=apple_mapkit_eta,
    traffic_basis=expected_traffic, observed_at, valid_until, duration_seconds,
    expected_departure, expected_arrival, origin and destination. Each endpoint
    has requested, resolved, latitude, longitude and source. Current origin uses
    requested=current_location, source=core_location, observed_at and accuracy_m;
    explicit endpoints use source=mapkit_search. All timestamps are ISO8601.
    The adapter must bind resolved identities to actual native lookup evidence.
    """
    return {"status": "unavailable", "error": "Native Maps ETA provider is not connected"}


def _arrival_identity(value: str) -> str:
    name = " ".join(value.casefold().split())
    if name in {"sfo", "san francisco international airport", "san francisco international airport (sfo)"}:
        return "airport:sfo"
    return name


def _arrival_time(value: object, zone: ZoneInfo) -> datetime:
    """An explicit instant or an unambiguous wall clock in the named zone."""
    if not isinstance(value, str) or len(value) > 80 or "T" not in value:
        raise ValueError("use a full ISO8601 date and time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("use a valid ISO8601 date and time") from exc
    if parsed.tzinfo is not None:
        local = parsed.astimezone(zone)
        # UTC is an absolute timestamp. Non-UTC offsets must describe a real
        # wall clock in the explicitly requested local timezone.
        if parsed.utcoffset() != timedelta(0) and (
                parsed.utcoffset() != local.utcoffset()
                or parsed.replace(tzinfo=None) != local.replace(tzinfo=None)):
            raise ValueError("date/time offset conflicts with the requested timezone")
        return parsed.astimezone(datetime_timezone.utc)
    valid = {}
    for fold in (0, 1):
        aware = parsed.replace(tzinfo=zone, fold=fold)
        stamp = aware.timestamp()
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == parsed:
            valid[stamp] = aware
    if len(valid) != 1:
        raise ValueError("local date/time is missing or ambiguous at a daylight-saving transition; give its UTC offset")
    return next(iter(valid.values())).astimezone(datetime_timezone.utc)


def _arrival_number(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError("provider returned an invalid number")
    return float(value)


def _arrival_endpoint(value: object, requested: str, *, current: bool,
                      now: datetime, zone: ZoneInfo) -> tuple[float, float, str]:
    if not isinstance(value, dict) or value.get("requested") != requested:
        raise ValueError("provider endpoint does not match the requested place")
    lat, lon = _arrival_number(value.get("latitude")), _arrival_number(value.get("longitude"))
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("provider returned invalid coordinates")
    resolved = value.get("resolved")
    if not isinstance(resolved, str) or not resolved.strip():
        raise ValueError("provider did not identify the resolved place")
    if current:
        if value.get("source") != "core_location":
            raise ValueError("current location was not measured by the device")
        age = (now - _arrival_time(value.get("observed_at"), zone)).total_seconds()
        accuracy = _arrival_number(value.get("accuracy_m"))
        if not 0 <= age <= _ARRIVAL_MAX_LOCATION_AGE_S or not 0 <= accuracy <= _ARRIVAL_MAX_LOCATION_ACCURACY_M:
            raise ValueError("current location is stale or insufficiently accurate")
    elif (value.get("source") != "mapkit_search"
          or _arrival_identity(resolved) != _arrival_identity(requested)):
        raise ValueError("provider resolved a different place")
    return lat, lon, resolved.strip()


@register(
    "route_to_arrive",
    "Plan driving departure to arrive by an explicit ISO8601 deadline, with an "
    "optional buffer, using a verified native Apple Maps expected-traffic ETA. "
    "Requires an IANA timezone (for SFO use America/Los_Angeles). An empty origin "
    "requests measured current location; never infer home or a city. Returns a "
    "Maps link without opening it. The native provider may be unavailable; "
    "report that honestly, never substitute the OSRM free-flow estimate.",
    {"type": "object", "properties": {
        "destination": {"type": "string", "description": "Exact destination name/address, e.g. SFO."},
        "arrive_by": {"type": "string", "description": "Full ISO8601 deadline, e.g. 2026-10-12T23:00:00-07:00."},
        "timezone": {"type": "string", "description": "Explicit IANA timezone, e.g. America/Los_Angeles."},
        "origin": {"type": "string", "description": "Explicit starting address, or empty for verified current location."},
        "buffer_minutes": {"type": "integer", "description": "Extra minutes before the arrival deadline, 0–180."},
    }, "required": ["destination", "arrive_by", "timezone"]},
    category="web_read",
    aliases=["when should I leave to reach SFO by 11 PM", "plan arrival using traffic",
             "work backward from my arrival deadline", "get me there by tonight"],
)
async def route_to_arrive(destination: str, arrive_by: str, timezone: str = "",
                          origin: str = "", buffer_minutes: int = 0) -> str:
    if not isinstance(destination, str) or not isinstance(origin, str):
        return "(ask the user for an exact destination and starting address)"
    destination, origin = (destination or "").strip(), (origin or "").strip()
    ambiguous = {"home", "my home", "office", "my office", "here", "there", "current location", "current_location"}
    if not destination or _arrival_identity(destination) in ambiguous | {"airport", "the airport"}:
        return "(ask the user for the exact destination and arrival date/time; do not guess an airport)"
    if origin and _arrival_identity(origin) in ambiguous:
        return "(ask the user for the actual starting address, or use an empty origin for measured current location; do not guess home)"
    try:
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("provide the destination's IANA timezone")
        zone = ZoneInfo(timezone)
        deadline = _arrival_time(arrive_by, zone)
        now = _arrival_now()
        if type(buffer_minutes) is not int or not 0 <= buffer_minutes <= 180:
            raise ValueError("buffer_minutes must be an integer from 0 to 180")
        target = deadline - timedelta(minutes=buffer_minutes)
        if target <= now:
            raise ValueError("arrival time including the buffer is already in the past")
        if deadline - now > timedelta(days=7):
            raise ValueError("arrival planning supports only the next seven days")
    except (ValueError, ZoneInfoNotFoundError, OverflowError, OSError) as exc:
        return f"(ask the user to clarify the arrival date/time: {exc})"
    origin_request = origin or "current_location"
    request = {"request_id": uuid.uuid4().hex, "origin": origin_request,
               "destination": destination, "arrival_at": target.isoformat(),
               "timezone": timezone, "requested_at": now.isoformat()}
    try:
        result = await asyncio.wait_for(_arrival_provider(dict(request)), timeout=_TIMEOUT)
    except Exception:  # Provider errors must not become a guessed departure.
        result = {"status": "unavailable"}
    if not isinstance(result, dict) or result.get("status") != "ok":
        origin_note = " Current location is unavailable; ask the user for a starting address." if not origin else ""
        return "(traffic-aware arrival planning is unavailable; no departure estimate was computed." + origin_note + ")"
    try:
        if (result.get("request_id") != request["request_id"]
                or result.get("provider") != "apple_mapkit_eta"
                or result.get("traffic_basis") != "expected_traffic"):
            raise ValueError("the provider did not verify the request and expected-traffic basis")
        checked_at = _arrival_now()
        observed = _arrival_time(result.get("observed_at"), zone)
        valid_until = _arrival_time(result.get("valid_until"), zone)
        if (not 0 <= (checked_at - observed).total_seconds() <= _ARRIVAL_MAX_AGE_S
                or not observed < valid_until or checked_at >= valid_until):
            raise ValueError("the provider estimate is stale or has invalid timing")
        a = _arrival_endpoint(result.get("origin"), origin_request, current=not origin, now=checked_at, zone=zone)
        b = _arrival_endpoint(result.get("destination"), destination, current=False, now=checked_at, zone=zone)
        duration = _arrival_number(result.get("duration_seconds"))
        departure = _arrival_time(result.get("expected_departure"), zone)
        arrival = _arrival_time(result.get("expected_arrival"), zone)
        if (not 0 < duration <= 2 * 24 * 3600
                or abs((arrival - departure).total_seconds() - duration) > 1
                or abs((arrival - target).total_seconds()) > 1):
            raise ValueError("the provider's duration and arrival/departure do not agree")
        if departure < checked_at:
            return "(the required departure is already in the past; this estimate cannot meet the requested arrival and buffer)"
    except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
        return f"(arrival estimate could not be verified: {exc}; no departure recommendation is available)"
    link = (f"https://maps.apple.com/?saddr={quote(f'{a[0]},{a[1]}', safe='')}"
            f"&daddr={quote(f'{b[0]},{b[1]}', safe='')}&dirflg=d")
    fmt = lambda value: value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
    age = int((checked_at - observed).total_seconds())
    return (f"{a[2]} to {b[2]}: leave by {fmt(departure)}; planned arrival {fmt(arrival)} "
            f"for the {fmt(deadline)} deadline, with {buffer_minutes} min buffer.\n"
            f"Apple Maps expected-traffic estimate: {_mins(duration)}; lookup age {age}s. "
            "Traffic and arrival can change; this is not a guaranteed arrival.\n"
            f"[View driving directions in Maps]({link})")


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
    "live traffic, and the answer says so. Driving only; for walking or transit, "
    "say so and suggest Apple Maps (open_app 'Maps').",
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
    # Apple Maps does route all modes properly; the user can open it for those.
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


