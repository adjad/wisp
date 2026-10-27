"""A short spoken-style weather answer built from get_weather's report.

get_weather returns a line-per-fact report written for a model. A read that
needs no model pass (service/workflows/reads.py execute_read) used to show that
report to the person verbatim. This turns it into two or three plain sentences
without another model call. Only the user-facing text changes: the raw report
stays the tool result that the model and follow-up turns see.

Anything the parser cannot read falls back to None so the caller keeps the raw
report rather than guessing.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_HEADER = re.compile(
    r"^Weather for (?P<resolved>.+?) \((?P<asked>.*)\)"
    r"(?: — (?P<period>.+?); forecast only)?$")
_NOW = re.compile(r"^\s*now:\s*(?P<body>.+)$")
_DAY = re.compile(r"^\s*forecast \((?P<date>\d{4}-\d{2}-\d{2})\):\s*(?P<body>.+)$")
_ASSUMED = re.compile(r"assumed (?P<place>[^;]+?) — say so if you meant a different one")
_TEMP = re.compile(r"(?P<f>-?\d+(?:\.\d+)?)°F(?: \((?P<c>-?\d+(?:\.\d+)?)°C\))?")
_FEELS = re.compile(r"feels like (-?\d+(?:\.\d+)?)°F")
_HIGH_LOW = re.compile(r"high (-?\d+(?:\.\d+)?)°F / low (-?\d+(?:\.\d+)?)°F")
_RAIN = re.compile(r"(\d+(?:\.\d+)?)% chance of rain")

# A day after today only mentions rain when it is worth planning around.
_RAIN_WORTH_SAYING = 20

# One-line optional sections, labelled as get_weather's _WEATHER_EXTRAS does.
_EXTRA = re.compile(
    r"^  (?P<label>severe-weather alerts|air quality|rain, next hours): (?P<text>\S.*)$")


def _place(resolved: str) -> str:
    """"San Francisco, California, United States of America" -> "San Francisco".

    A place outside the US keeps its country so "Dublin, Ireland" stays distinct.
    """
    parts = [p.strip() for p in resolved.split(",") if p.strip()]
    if not parts:
        return resolved.strip()
    if len(parts) >= 2 and not parts[-1].lower().startswith("united states"):
        return f"{parts[0]}, {parts[-1]}"
    return parts[0]


def _num(value: str) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else str(number)


def _day_labels(days: list[date], *, current: bool, first_raw: date | None) -> list[str]:
    """Words for each forecast date, from the report alone.

    Forecast dates belong to the place, not to this Mac, so the Mac's clock
    says nothing about which one is "today". With current conditions the
    report is unfiltered and its first row is the place's today, but only if
    that row is the first one we are showing: when it was unreadable and
    skipped, the next row is not "today". Everything else gets a weekday,
    never a relative word that could be wrong.
    """
    labels = [d.strftime("%A") for d in days]
    if current and days and first_raw is not None and days[0] == first_raw:
        labels[0] = "Today"
        if len(days) > 1 and days[1] == days[0] + timedelta(days=1):
            labels[1] = "Tomorrow"
    return labels


def _now_parts(body: str) -> dict | None:
    temp = _TEMP.search(body)
    if not temp:
        return None
    out = {"f": _num(temp["f"]), "c": _num(temp["c"]) if temp["c"] else ""}
    # "61°F (16°C), Sunny, feels like 56°F, humidity 68%, wind 10 mph"
    rest = body[temp.end():].lstrip(", ")
    first = rest.split(",")[0].strip()
    out["desc"] = ("" if not first or first.startswith(("feels like", "humidity", "wind"))
                   else first.lower())
    feels = _FEELS.search(body)
    out["feels"] = _num(feels[1]) if feels else ""
    return out


def _day_parts(body: str) -> dict | None:
    hl = _HIGH_LOW.search(body)
    if not hl:
        return None
    rest = body[hl.end():].lstrip(", ")
    desc = rest.split(",")[0].strip()
    if not desc or _RAIN.search(desc) or "chance of rain" in desc:
        desc = ""
    rain = _RAIN.search(body)
    return {"high": _num(hl[1]), "low": _num(hl[2]), "desc": desc.lower(),
            "rain": float(rain[1]) if rain else None}


def _rain_text(rain: float | None) -> str:
    return f"{_num(str(rain))}% chance of rain" if rain is not None else ""


def format_weather_reply(raw) -> str | None:
    """Plain sentences for a successful get_weather report, else None."""
    text = str(raw or "").strip()
    if not text or text.startswith("(error"):
        return None
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    header = _HEADER.match(lines[0].strip()) if lines else None
    if not header:
        return None
    place = _place(header["resolved"])
    forecast_only = bool(header["period"])

    now = None
    days: list[tuple[date, dict]] = []
    extras: list[str] = []
    first_raw: date | None = None
    seen_raw = False
    for line in lines[1:]:
        if m := _NOW.match(line):
            now = _now_parts(m["body"])
        elif m := _DAY.match(line):
            try:
                day = datetime.strptime(m["date"], "%Y-%m-%d").date()
            except ValueError:
                seen_raw = True   # an unreadable first row is still the first row
                continue
            if not seen_raw:
                first_raw, seen_raw = day, True
            if parts := _day_parts(m["body"]):
                days.append((day, parts))
        elif (m := _EXTRA.match(line)) and not m["text"].rstrip().endswith(":"):
            # An optional section the tool appends (alerts, air quality, rain
            # next hours) that fits on one line is already plain text.
            extras.append(line.strip())
        else:
            # A section with a list under it ("Active alerts:" then titles,
            # "(+3 more)", hourly rain lines) or anything unrecognised: do
            # not summarise around it, or an alert could be hidden.
            return None

    if now is None and not days:
        return None

    sentences: list[str] = []
    if now:
        temp = f"{now['f']}°F" + (f" ({now['c']}°C)" if now["c"] else "")
        cond = f" and {now['desc']}" if now["desc"] else ""
        feels = (f" — feels like {now['feels']}°F"
                 if now["feels"] and now["feels"] != now["f"] else "")
        sentences.append(f"It's {temp}{cond} in {place} right now{feels}.")

    # Without a requested range the report carries three days; two is plenty.
    shown = days if forecast_only else days[:2]
    labels = _day_labels([d for d, _ in shown], current=bool(now), first_raw=first_raw)
    for index, (day, info) in enumerate(shown):
        label = labels[index]
        rain = _rain_text(info["rain"])
        if now and index == 0 and label == "Today":
            bits = [f"high {info['high']}°F", f"low {info['low']}°F"]
            if rain:
                bits.append(rain)
            sentences.append(f"{label}: {', '.join(bits)}.")
            continue
        lead = f"{label} in {place}" if not sentences else label
        bits = ([info["desc"]] if info["desc"] else [])
        bits.append(f"{info['high']}°F / {info['low']}°F")
        if rain and (index == 0 or (info["rain"] or 0) >= _RAIN_WORTH_SAYING):
            bits.append(rain)
        sentences.append(f"{lead}: {', '.join(bits)}.")

    reply = " ".join(sentences)
    if (assumed := _ASSUMED.search(header["asked"])):
        reply += f" (I assumed {assumed['place']} — say so if you meant a different one.)"
    if extras:
        reply += "\n\n" + "\n".join(extras)
    return reply
