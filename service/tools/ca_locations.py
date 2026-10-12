"""Bounded California weather location normalization, without geocoding guesses."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


CA_CITIES = (
    "San Francisco", "Santa Clara", "Santa Cruz", "Los Angeles", "San Diego",
    "San Jose", "Sacramento", "Fresno", "Long Beach", "Oakland", "Bakersfield",
    "Anaheim", "Stockton", "Riverside", "Irvine", "Chula Vista", "Fremont",
    "Modesto", "San Bernardino", "Santa Rosa",
)


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


@dataclass(frozen=True)
class WeatherLocation:
    query: str
    california_city: str | None = None


def normalize_weather_location(place: str) -> WeatherLocation:
    """Normalize explicit CA input; leave other countries/states unchanged.

    SF is the one supported standalone city alias. Shorter shared city prefixes
    and other state-less CA names require clarification rather than a guessed
    state. Unknown explicit CA cities remain supported without spelling repair.
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
    # A comma-qualified non-CA city (San Jose, Costa Rica) must not be replaced.
    if "," not in place and (normalized in _CITIES or normalized in _ALIASES
                             or normalized in _AMBIGUOUS):
        raise ValueError("ambiguous city; ask the user for the city and state/country")
    if re.search(r"\b(?:ca|calif|california)\b", normalized):
        raise ValueError("ambiguous California location; ask for city, state and country")
    return WeatherLocation(place)


def provider_area_matches(area: object, city: str) -> bool:
    """Require exact city plus CA/US identity, never another city or country."""
    def first(key: str) -> str:
        try:
            value = area[key][0]["value"]
        except (KeyError, IndexError, TypeError):
            return ""
        return _key(value) if isinstance(value, str) else ""

    return (first("areaName") == _key(city)
            and first("region") in {"ca", "california"}
            and first("country") in _US)
