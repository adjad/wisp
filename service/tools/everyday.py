"""The composite briefings — one call where the model would otherwise make four.

WHY A COMPOSITE EARNS ITS PLACE
-------------------------------
Normally, folding several tools into one is a mistake: it hides structure the
model is perfectly capable of assembling itself. This is the exception, and the
reason is measured. Latency here is ~91% decode and ~9% prefill, and an output
token costs ~137x a prompt token — so the expensive part of "give me a morning
briefing" is not fetching the data, it is the four extra ROUND TRIPS the model
spends deciding to call get_upcoming, then get_weather, then summarize_emails,
then narrating between each. Each of those steps generates reasoning tokens
nobody sees.

Gathering the four sources in Python and handing back one block collapses that
to a single tool call and a single narration. The data is identical; what
disappears is three rounds of decode.

The second reason is correctness. `agent/loop.SYSTEM` is emphatic that a
multi-source answer built from one source "is not a partial answer, it is a
WRONG one" — the aggregate to-do route carries `multi_round=True` for exactly
this. A composite cannot silently answer from one source, because it always
gathers all of them.

FAILURE POLICY
--------------
Any source may be unavailable — no weather without a location, mail not synced
yet, oMLX down. A missing source is NAMED in the output rather than dropped
silently, so the user can tell "nothing on your calendar" apart from "I couldn't
read your calendar."
"""
from __future__ import annotations

import asyncio
import datetime as dt

from service.tools.registry import register

_PER_SOURCE_TIMEOUT = 25.0


async def _safe(label: str, coro) -> tuple[str, str | None]:
    """Run one source. Returns (text, error) — never raises.

    A briefing that dies because one of four sources timed out is worse than a
    briefing that says which one is missing.
    """
    try:
        return await asyncio.wait_for(coro, timeout=_PER_SOURCE_TIMEOUT), None
    except asyncio.TimeoutError:
        return "", f"{label} took too long to load"
    except Exception as e:  # noqa: BLE001 — one bad source must not kill the brief
        return "", f"{label} unavailable ({e})"


def _greeting(now: dt.datetime) -> str:
    h = now.hour
    if h < 12:
        return "Good morning"
    if h < 18:
        return "Good afternoon"
    return "Good evening"


@register(
    "daily_brief",
    "Assemble the user's briefing in ONE call: today's schedule, the weather, "
    "and what's new across mail and messages. Use this for 'good morning', "
    "'how's my day looking', 'catch me up', or 'give me a briefing' instead of "
    "calling get_upcoming, get_weather and the summarizers separately — it "
    "gathers all of them and is much faster.",
    {
        "type": "object",
        "properties": {
            "location": {"type": "string",
                         "description": "City for the weather, e.g. 'Palo Alto,CA'. Omit to skip weather; never guess one."},
            "days": {"type": "integer",
                     "description": "How far ahead to look on the calendar. Default 1 (today)."},
            "include_activity": {"type": "boolean",
                                 "description": "Include recent mail/messages activity. Default true."},
        },
    },
    category="assistant_read",
    # NOT "good morning". A bare greeting is claimed by TRIVIAL_RE and answered
    # by the fast model with no tools, which is the RIGHT behavior — someone
    # saying hello wants a hello, and forcing a tool call on genuinely
    # conversational input is the failure `expect_tool_first` exists to avoid.
    # Listing it here would only have made the retrieval eval pass on a phrasing
    # that never reaches retrieval.
    aliases=["how's my day looking", "give me the rundown",
             "what's my day like today", "brief me", "walk me through today",
             "what do I need to know this morning", "give me the full picture for today",
             "run me through today"],
)
async def daily_brief(location: str = "", days: int = 1,
                      include_activity: bool = True) -> str:
    from service.tools.assistant_tools import get_upcoming
    from service.tools.recent_tools import get_recent_activity

    try:
        days = max(1, min(14, int(days)))
    except (TypeError, ValueError):
        days = 1

    jobs: list[tuple[str, object]] = [("Schedule", get_upcoming(days=days))]
    if location.strip():
        from service.tools.web_tools import get_weather
        jobs.append(("Weather", get_weather(location.strip())))
    if include_activity:
        jobs.append(("Recent", get_recent_activity(hours=16, limit=12)))

    # Gathered concurrently, and none of these touch oMLX — they read local
    # stores and (for weather) one HTTP endpoint. This is thread/IO concurrency,
    # NOT model concurrency, which this system explicitly cannot do (see the
    # block comment in agent/loop.py and [[omlx-single-model-no-parallelism]]).
    results = await asyncio.gather(*(_safe(label, coro) for label, coro in jobs))

    now = dt.datetime.now()
    out = [f"{_greeting(now)} — {now.strftime('%A, %B %-d')}."]
    problems: list[str] = []

    for (label, _), (text, err) in zip(jobs, results):
        if err:
            problems.append(err)
            continue
        body = (text or "").strip()
        if not body:
            continue
        out.append(f"\n{label}:\n{body}")

    if not location.strip():
        problems.append("no weather (no location set — ask the user which city)")
    if problems:
        out.append("\nCouldn't include: " + "; ".join(problems) + ".")
    return "\n".join(out)


@register(
    "find_my_device",
    "Open Find My so the user can locate a device or see where someone is. "
    "This OPENS the app — it cannot read locations back, because macOS exposes "
    "no API for Find My data. Say that plainly rather than implying a location "
    "was retrieved.",
    {
        "type": "object",
        "properties": {
            "target": {"type": "string",
                       "description": "What they're looking for — 'iPhone', 'AirPods', or a person's name."},
        },
    },
    category="app_control",
    aliases=["where's my phone", "I can't find my airpods", "find my iphone",
             "ping my phone", "where did I leave my laptop",
             "where is my daughter right now"],
)
def find_my_device(target: str = "") -> str:
    import subprocess

    p = subprocess.run(["open", "-a", "FindMy"], capture_output=True,
                       text=True, timeout=15)
    if p.returncode != 0:
        return f"(could not open Find My: {(p.stderr or '').strip()})"
    what = f" Look for {target.strip()}." if target.strip() else ""
    # Deliberately explicit about the limit. Implying Wisp had READ the location
    # would be a fabricated fact, which is the one thing the tool layer exists
    # to prevent.
    return ("Opened Find My." + what +
            " I can't read locations back — macOS has no API for Find My data, "
            "so you'll need to look at the window.")
