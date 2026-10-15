"""Argument matchers for case expectations.

A case says what an argument must look like, not its exact bytes — a model is
right to send "mom" or "Mom" or "mom@example.com". A spec is either a bare
value (shorthand for `eq`) or a mapping with ONE of these keys:

    eq: v            case-insensitive equality for strings, == otherwise
    icontains: s     the argument (stringified) contains s, ignoring case
    contains_any: [] contains at least one of the strings
    regex: pat       re.search, ignoring case
    in: [..]         equals one of the values (case-insensitive for strings)
    present: true    the argument is supplied and non-empty
    absent: true     the argument is not supplied (or empty)
    date_offset: n   an ISO date/datetime whose DATE is today + n days
    weekday: name    an ISO date/datetime falling on that weekday, in the future

`match_args(spec, args, today)` returns a list of human-readable mismatch
strings; empty means the call satisfies the spec.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _norm(v):
    return v.strip().lower() if isinstance(v, str) else v


def _as_date(v) -> date | None:
    if not isinstance(v, str):
        return None
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})", v)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def match_value(spec, value, today: date) -> str | None:
    """None when `value` satisfies `spec`, else why it does not."""
    if not isinstance(spec, dict):
        spec = {"eq": spec}
    if len(spec) != 1:
        raise ValueError(f"matcher must have exactly one key, got {sorted(spec)}")
    (op, want), = spec.items()
    if op == "absent":
        return None if _empty(value) == bool(want) else f"expected {'absent' if want else 'present'}"
    if op == "present":
        return None if (not _empty(value)) == bool(want) else f"expected {'present' if want else 'absent'}"
    if _empty(value):
        return "missing"
    text = value if isinstance(value, str) else str(value)
    if op == "eq":
        return None if _norm(value) == _norm(want) else f"{value!r} != {want!r}"
    if op == "icontains":
        return None if str(want).lower() in text.lower() else f"{value!r} lacks {want!r}"
    if op == "contains_any":
        return None if any(str(w).lower() in text.lower() for w in want) else \
            f"{value!r} lacks any of {want!r}"
    if op == "regex":
        return None if re.search(want, text, re.I) else f"{value!r} !~ /{want}/"
    if op == "in":
        return None if _norm(value) in {_norm(w) for w in want} else f"{value!r} not in {want!r}"
    if op == "date_offset":
        d = _as_date(value)
        target = today + timedelta(days=int(want))
        return None if d == target else f"{value!r} is not {target.isoformat()}"
    if op == "weekday":
        d = _as_date(value)
        if d is None:
            return f"{value!r} is not an ISO date"
        ok = d.weekday() == _WEEKDAYS.index(str(want).lower()) and today <= d <= today + timedelta(days=7)
        return None if ok else f"{value!r} is not the coming {want}"
    raise ValueError(f"unknown matcher {op!r}")


def match_args(spec: dict | None, args: dict, today: date) -> list[str]:
    problems = []
    for key, sub in (spec or {}).items():
        why = match_value(sub, (args or {}).get(key), today)
        if why:
            problems.append(f"{key}: {why}")
    return problems
