"""Discover recurring states from personal activity; no app/site/activity taxonomy.

These are observable-pattern candidates, not busy labels. A separate grounded
semantic interpretation or ordinary user decision supplies that hypothesis.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import date
from zoneinfo import ZoneInfo

from service.behavior.timeutil import at_minute, local_dt


def _vector(observation):
    attrs = observation.attrs
    covered = max(1, attrs.get("covered", 0))
    vector = {
        "app:" + app: seconds / covered
        for app, seconds in attrs.get("apps", {}).items()
        if app
    }
    # Correlated idle, lock and screen state share a feature budget. These are
    # descriptors, not rules declaring a person busy or free.
    for name in ("idle", "inactive", "screen_sleep"):
        vector["device:" + name] = attrs.get(name, 0) / covered / 3
    return vector


def _similarity(a, b):
    norm = math.sqrt(sum(x * x for x in a.values()) * sum(x * x for x in b.values()))
    return (
        sum(value * b.get(key, 0) for key, value in a.items()) / norm
        if norm
        else float(a == b)
    )


def has_active_app_support(pattern):
    """Minimum semantic support, never proof that a person is occupied.

    The correlated device-absence features and point website visits cannot
    supply observed app activity. Unknown/invalid support remains unknown.
    """
    descriptors = pattern.get("descriptors")
    return isinstance(descriptors, dict) and any(
        isinstance(key, str) and key.startswith("app:") and len(key) > 4
        and type(value) in (int, float) and 0 < value <= 1 and math.isfinite(value)
        for key, value in descriptors.items()
    )


def discover(bins, visits, day, timezone, now):
    """Unsupervised personal prototypes + covered weekday/time recurrence.

    Sampling, similarity and minimum evidence thresholds control data quality.
    None associates a particular app, site, word or example with busyness.
    """
    tz, target = ZoneInfo(timezone), date.fromisoformat(day)
    observations = [
        o
        for o in bins
        if o.attrs.get("timezone") == timezone
        and now - 56 * 86400 <= o.ts < now
        and local_dt(o.ts, tz).date() < local_dt(now, tz).date()
        and local_dt(o.ts, tz).weekday() == target.weekday()
        and o.attrs.get("covered", 0) >= 720
    ]
    observations.sort(key=lambda o: o.ts)
    prototypes, assignments = [], []
    opportunities = defaultdict(set)
    for obs in observations:
        dt = local_dt(obs.ts, tz)
        opportunities[dt.hour * 60 + dt.minute // 15 * 15].add(dt.date())
    for obs in observations:
        vector = _vector(obs)
        if not any(vector.values()):
            continue  # unknown state retains its covered opportunity, never meaning
        scores = [
            (
                _similarity(vector, p["vector"])
                if max(
                    (
                        abs(vector.get(k, 0) - p["vector"].get(k, 0))
                        for k in vector.keys() | p["vector"].keys()
                    ),
                    default=0,
                )
                <= 0.25
                else 0
            )
            for p in prototypes
        ]
        if scores and max(scores) >= 0.85:
            index = max(range(len(scores)), key=scores.__getitem__)
            p = prototypes[index]
            count = p["count"]
            p["vector"] = {
                key: (p["vector"].get(key, 0) * count + vector.get(key, 0))
                / (count + 1)
                for key in p["vector"].keys() | vector.keys()
            }
            p["count"] += 1
        elif len(prototypes) < 128:
            index = len(prototypes)
            prototypes.append(dict(vector=vector, count=1, anchor=obs.id))
        else:
            continue  # bounded state space; disclose omitted novelty
        assignments.append((obs, index))
    support = defaultdict(dict)
    for obs, index in assignments:
        dt = local_dt(obs.ts, tz)
        minute = dt.hour * 60 + dt.minute // 15 * 15
        support[index, minute][dt.date()] = obs
    candidates = []
    for (index, minute), hits in sorted(support.items()):
        eligible = sorted(
            d for d in opportunities[minute] if d.weekday() == target.weekday()
        )
        matching = sorted(d for d in hits if d.weekday() == target.weekday())
        weeks = {d.isocalendar()[:2] for d in matching}
        if not eligible or len(weeks) < 3 or len(matching) / len(eligible) < 0.6:
            continue
        if sum(d in hits for d in eligible[-3:]) < 2:
            continue
        last = max(hits[d].end_ts for d in matching)
        if not 0 <= now - last <= 21 * 86400:
            continue
        prototype = prototypes[index]
        state_id = hashlib.sha256(prototype["anchor"].encode()).hexdigest()[:20]
        candidates.append(
            dict(
                state_id=state_id,
                minute=minute,
                support=len(matching),
                opportunities=len(eligible),
                distinct_weeks=len(weeks),
                observed_at=last,
                dates=set(matching),
                eligible=set(eligible),
                hits=hits,
                evidence=[hits[d].id for d in matching][-5:],
            )
        )

    def cohort(group):
        dates = set.intersection(*(x["dates"] for x in group))
        eligible = sorted(set.intersection(*(x["eligible"] for x in group)))
        samples = [x["hits"][d] for d in sorted(dates) for x in group]
        observed_at = max((o.end_ts for o in samples), default=0)
        valid = (
            bool(eligible)
            and len({d.isocalendar()[:2] for d in dates}) >= 3
            and len(dates) / len(eligible) >= 0.6
            and sum(d in dates for d in eligible[-3:]) >= 2
            and 0 <= now - observed_at <= 21 * 86400
        )
        return valid, dates, eligible, observed_at, [o.id for o in samples][-5:]

    groups = []
    for c in candidates:
        if (
            groups
            and groups[-1][-1]["state_id"] == c["state_id"]
            and groups[-1][-1]["minute"] + 15 == c["minute"]
            and cohort([*groups[-1], c])[0]
        ):
            groups[-1].append(c)
        else:
            groups.append([c])
    patterns = []
    for group in groups:
        valid, sample_dates, eligible, observed_at, evidence = cohort(group)
        if not valid:
            continue
        samples = [x["hits"][d] for d in sorted(sample_dates) for x in group]
        vector = defaultdict(float)
        for sample in samples:
            for key, value in _vector(sample).items():
                vector[key] += value / len(samples)
        descriptors = {key: round(value, 3) for key, value in sorted(vector.items()) if value >= 0.05}
        first, last = group[0]["minute"], group[-1]["minute"] + 15
        start, end = at_minute(target, first, tz), at_minute(target, last, tz)
        if (
            local_dt(start, tz).hour * 60 + local_dt(start, tz).minute != first
            or end <= start
        ):
            continue
        c = group[0]
        ident = hashlib.sha256(
            f"{timezone}:{target.weekday()}:{first}:{last}:{c['state_id']}".encode()
        ).hexdigest()[:24]
        # Visits remain point context. No visit-to-next-visit dwell is created.
        domains = sorted(
            {
                str(v.get("host", ""))
                for v in visits
                if type(v.get("ts")) in (int, float)
                and local_dt(v["ts"], tz).date() in sample_dates
                and first
                <= local_dt(v["ts"], tz).hour * 60 + local_dt(v["ts"], tz).minute
                < last
            }
        )[:20]
        n = len(eligible)
        hit = len(sample_dates)
        if hit / n < 0.6:
            continue
        patterns.append(
            dict(
                id="activity-pattern:" + ident,
                state_id=c["state_id"],
                start=start,
                end=end,
                timezone=timezone,
                support=hit,
                opportunities=n,
                distinct_weeks=len({d.isocalendar()[:2] for d in sample_dates}),
                observed_at=observed_at,
                evidence=evidence,
                descriptors=descriptors,
                website_visit_context=domains,
                confidence=min(0.85, 0.5 + 0.3 * hit / n + 0.01 * min(n, 5)),
                coverage="adequate",
                validation="provisional recurrence strength; not calibrated availability probability",
                interpretation="unclassified recurring activity; availability unknown",
            )
        )
    return patterns


def apply_interpretations(patterns, interpretations):
    """A semantic hypothesis never becomes an observed fact or automatic action."""
    by_id = {p["id"]: p for p in patterns}
    windows = []
    for item in interpretations:
        p = by_id.get(item.get("pattern_id"))
        if (p is None or item.get("availability") != "likely_occupied"
                or not has_active_app_support(p)):
            continue
        windows.append(
            {
                **p,
                "kind": "busy",
                "current": True,
                "title": "Likely occupied",
                "basis": "inferred",
                "because": f"Recurring personal activity in {p['support']} of {p['opportunities']} covered windows; interpreted as possibly occupied. Correctable, not verified attendance.",
                "semantic_reason": item.get("reason", ""),
                "uncertainty": "Activity and inferred availability may differ.",
            }
        )
    return windows


def competing_activity(events, bins, timezone):
    """Compare each event's observed app distribution with itself across dates.

    Divergence describes a changing context; it does not rank the event's value.
    """
    groups = defaultdict(list)
    for event in events:
        if (
            event.end_ts is None
            or event.end_ts <= event.ts
            or event.attrs.get("all_day")
        ):
            continue
        vector, covered = defaultdict(float), 0
        for b in bins:
            if (
                b.attrs.get("timezone") != timezone
                or b.ts >= event.end_ts
                or b.end_ts <= event.ts
            ):
                continue
            overlap = min(event.end_ts, b.end_ts) - max(event.ts, b.ts)
            # Coarse aggregates do not locate their gaps within the bin. Count
            # only defensible lower bounds, rather than assuming uniform use.
            outside = (b.end_ts - b.ts) - overlap
            covered += max(0, b.attrs.get("covered", 0) - outside)
            for app, seconds in b.attrs.get("apps", {}).items():
                vector[app] += max(0, seconds - outside)
        if covered >= 0.8 * (event.end_ts - event.ts):
            total = sum(vector.values()) or 1
            groups[event.subject].append(
                (event.id, {k: v / total for k, v in vector.items()})
            )
    out = []
    for title, samples in groups.items():
        if len(samples) < 3:
            continue
        centroid = {
            key: sum(v.get(key, 0) for _, v in samples) / len(samples)
            for key in set().union(*(v for _, v in samples))
        }
        out.append(
            dict(
                title=title,
                opportunities=len(samples),
                evidence=[i for i, _ in samples][-5:],
                context_consistency=round(
                    sum(_similarity(v, centroid) for _, v in samples) / len(samples), 3
                ),
                apps=centroid,
                basis="observed computer context during scheduled windows",
                importance="unknown; competing activity does not establish attendance or lower priority",
            )
        )
    return out
