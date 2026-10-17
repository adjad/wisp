"""Bounded California weather location normalization, without geocoding guesses."""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass


CA_CITIES = (
    "San Francisco", "Santa Clara", "Santa Cruz", "Los Angeles", "San Diego",
    "San Jose", "Sacramento", "Fresno", "Long Beach", "Oakland", "Bakersfield",
    "Anaheim", "Stockton", "Riverside", "Irvine", "Chula Vista", "Fremont",
    "Modesto", "San Bernardino", "Santa Rosa",
)

# City-center coordinates for the canonical cities. wttr.in's name geocoder is
# unreliable for several of them (live 2026-10-16: "Oakland,CA" resolved to
# Ontario, Canada; "Santa Rosa,CA" to Vancouver; "Los Angeles,CA" to a point
# it names "Wingfoot"), so these cities are queried by coordinates instead and
# verified by distance, not by the provider's area label.
CA_CENTERS = {
    "San Francisco": (37.7749, -122.4194), "Santa Clara": (37.3541, -121.9552),
    "Santa Cruz": (36.9741, -122.0308), "Los Angeles": (34.0522, -118.2437),
    "San Diego": (32.7157, -117.1611), "San Jose": (37.3382, -121.8863),
    "Sacramento": (38.5816, -121.4944), "Fresno": (36.7378, -119.7871),
    "Long Beach": (33.7701, -118.1937), "Oakland": (37.8044, -122.2712),
    "Bakersfield": (35.3733, -119.0187), "Anaheim": (33.8366, -117.9143),
    "Stockton": (37.9577, -121.2908), "Riverside": (33.9806, -117.3755),
    "Irvine": (33.6846, -117.8265), "Chula Vista": (32.6401, -117.0842),
    "Fremont": (37.5485, -121.9886), "Modesto": (37.6391, -120.9969),
    "San Bernardino": (34.1083, -117.2898), "Santa Rosa": (38.4404, -122.7141),
}
_MAX_CENTER_KM = 15.0


def _key(value: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", value)
                    if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", plain.casefold().replace(".", "").replace("_", " ")).strip()


_CITIES = {_key(city): city for city in CA_CITIES}
_ALIASES = {
    "sf": "San Francisco",
    "la": "Los Angeles", "sd": "San Diego", "sj": "San Jose",
    # Deliberately finite, reviewed spelling repairs; do not fuzzy-match an
    # arbitrary place against a list that omits other real California cities.
    "san fransisco": "San Francisco", "santa clra": "Santa Clara",
    "santa crz": "Santa Cruz", "san jsoe": "San Jose",
}
_US = {"us", "usa", "united states", "united states of america"}
_CA_SUFFIX = re.compile(
    r"^(?P<city>.+?)(?:\s*,\s*|\s+)(?:c\.?a\.?|calif\.?|california\.?)"
    r"(?:\s*,\s*(?:us|usa|united states(?: of america)?))?$", re.I)
_AMBIGUOUS = {"san", "santa", "santa c", "sc", "la", "sd", "sj", "california", "ca"}
# Full names whose California city is the overwhelmingly dominant match, so a
# bare "Los Angeles" resolves there (verified like any CA lookup) and the result
# says it assumed California. Names shared with a comparably large or capital
# city elsewhere (San Jose, Costa Rica; Santa Cruz, Bolivia; Santa Clara, Cuba;
# Riverside, Irvine, Stockton, Fremont, Santa Rosa) still require the state or
# country.
_DOMINANT = frozenset(_key(city) for city in (
    "San Francisco", "Los Angeles", "San Diego", "Sacramento",
    "Oakland", "Fresno", "Long Beach", "Bakersfield", "Anaheim", "Chula Vista",
    "San Bernardino", "Modesto",
))


@dataclass(frozen=True)
class WeatherLocation:
    query: str
    california_city: str | None = None
    assumed_state: bool = False

    @property
    def provider_query(self) -> str:
        """What wttr.in is asked for: center coordinates when known."""
        center = CA_CENTERS.get(self.california_city or "")
        return f"{center[0]},{center[1]}" if center else self.query


def normalize_weather_location(place: str) -> WeatherLocation:
    """Normalize explicit CA input; leave other countries/states unchanged.

    SF is the one supported standalone city alias, and a bare full name in
    _DOMINANT resolves to California with ``assumed_state`` set. Shorter shared
    city prefixes and other state-less CA names require clarification rather
    than a guessed state. Unknown explicit CA cities remain supported without
    spelling repair.
    """
    normalized = _key(place)
    match = _CA_SUFFIX.fullmatch(place.strip())
    if match:
        city_text = re.sub(r"\s+", " ", match["city"]).strip()
        city_key = _key(city_text)
        if ((city_key in _AMBIGUOUS and city_key not in _ALIASES)
                or not re.fullmatch(r"[^\W\d_]+(?:[ .’'\-][^\W\d_]+)*", city_text)):
            raise ValueError("ambiguous California city; ask the user for its full name")
        city = _CITIES.get(city_key) or _ALIASES.get(city_key) or city_text
        return WeatherLocation(f"{city},CA", city)
    if normalized == "sf":
        return WeatherLocation("San Francisco,CA", "San Francisco")
    if "," not in place and normalized in _DOMINANT:
        city = _CITIES[normalized]
        return WeatherLocation(f"{city},CA", city, assumed_state=True)
    # A comma-qualified non-CA city (San Jose, Costa Rica) must not be replaced.
    if "," not in place and (normalized in _CITIES or normalized in _ALIASES
                             or normalized in _AMBIGUOUS):
        raise ValueError("ambiguous city; ask the user for the city and state/country")
    if re.search(r"\b(?:ca|calif|california)\b", normalized):
        raise ValueError("ambiguous California location; ask for city, state and country")
    return WeatherLocation(place)


def provider_area_matches(area: object, city: str) -> bool:
    """Require CA/US identity plus the exact city or a point near its center."""
    def first(key: str) -> str:
        try:
            value = area[key][0]["value"]
        except (KeyError, IndexError, TypeError):
            return ""
        return _key(value) if isinstance(value, str) else ""

    if first("region") not in {"ca", "california"} or first("country") not in _US:
        return False
    return first("areaName") == _key(city) or _near_center(area, city)


def _near_center(area: object, city: str) -> bool:
    center = CA_CENTERS.get(city)
    if center is None or not isinstance(area, dict):
        return False
    try:
        lat, lon = float(area["latitude"]), float(area["longitude"])
    except (KeyError, TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    # Equirectangular distance is exact enough at a 15 km radius.
    x = math.radians(lon - center[1]) * math.cos(math.radians((lat + center[0]) / 2))
    y = math.radians(lat - center[0])
    return 6371.0 * math.hypot(x, y) <= _MAX_CENTER_KM
