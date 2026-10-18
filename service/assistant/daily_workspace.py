"""A source-first daily workspace. Assessment and planning never perform actions.

The pure ``assess`` boundary accepts frozen evidence. A communication is a
review candidate, never proof of an obligation. Behavioral windows are a
separate, optional input; this module does not collect or learn behavior.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo

from service.assistant.today import build_plan, day_bounds

SOURCES = ("calendar", "reminders", "email", "messages", "notes")
MAX_ITEMS = 80
MAX_SUGGESTIONS = 8
BEHAVIOR_PROOF_MAX_AGE = 20 * 60
SOURCE_RECEIPT_MAX_AGE = dict(email=1200, messages=600, notes=86400, calendar=1200, reminders=1200)
_availability_provider = None


def set_availability_provider(provider):
    """Host/test override of the public ``planning_context`` v1 callable.

    None restores lazy public-wrapper composition. No producer internals,
    collection, inference or consent decisions belong to this consumer.
    """
    global _availability_provider
    if provider is not None and not callable(provider):
        raise ValueError("A host availability provider must be callable")
    _availability_provider = provider


def availability_snapshot(day, timezone, now):
    """Read the public wrapper once; never cache or renew its evidence ages.

    The producer must fence consent, generation, source revision and analysis
    role on EVERY read. ``ready`` is permission to inspect hypotheses, not a
    claim of complete coverage or free time. A missing producer is unavailable.
    """
    unavailable = dict(version=1, ready=False, available=False, timezone=timezone,
        generated_at=None, evidence_checked_at=None, windows=[], demands=[],
        associations=[], source_coverage={},
        detail="Behavioral context is unavailable; availability is unknown and no inferred time is reserved.")
    try:
        if not finite(now):
            return unavailable
        day_bounds(day, timezone)
        provider = _availability_provider
        if provider is None:
            from service.assistant.behavior_runtime import planning_context
            provider = planning_context
        result = deepcopy(provider(day, timezone, now))
        return result if valid_availability(result, timezone, now) else unavailable
    except Exception:
        return unavailable


def _fresh(stamp, now):
    return finite(stamp) and 0 <= now - stamp <= BEHAVIOR_PROOF_MAX_AGE


def _original(stamp, checked_at):
    # Old analysis is durable when the public producer revalidates its exact
    # current source revision. Never turn the checked time into analysis time.
    return finite(stamp) and 0 < stamp <= checked_at


def valid_availability(value, timezone, now):
    """Actual public v1 envelope; source_coverage is descriptive, not global."""
    return (finite(now) and isinstance(value, dict)
        and type(value.get("version")) is int and value["version"] == 1
        and value.get("ready") is True and value.get("timezone") == timezone
        and _fresh(value.get("evidence_checked_at"), now)
        and _original(value.get("generated_at"), value["evidence_checked_at"])
        and all(isinstance(value.get(k, []), list) for k in ("windows", "demands", "associations"))
        and isinstance(value.get("source_coverage", {}), dict)
        and all(isinstance(k, str) and isinstance(v, str)
                for k, v in value.get("source_coverage", {}).items()))


def _refs(value):
    return isinstance(value, list) and 1 <= len(value) <= 100 and all(isinstance(v, str) and 0 < len(v) <= 500 for v in value)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def clean(value, limit=240):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def key(source, identity):
    return source + ":" + hashlib.sha256(str(identity).encode()).hexdigest()[:24]


# Quoted or forwarded history below the current body is not current work.
_QUOTED_HISTORY = re.compile(r"(?im)^\s*(?:On .+wrote:|[- ]*Original Message[- ]*|Begin forwarded message:|>.*)$")
# Who wrote it and when, carried from a communication record to its attention
# item so the notch can name the sender and order by recency; `replied` marks a
# conversation the user has texted since. An email's model-written gist is
# attached by the Today endpoint (mail_gists.attach), so this module stays pure.
_COMMUNICATION_FIELDS = ("sender", "person", "received_ts", "replied")


def important_email_rows(snapshot):
    """(row, current_lines) for each snapshot email that counts as attention.

    The one selection rule, shared by the day's evidence and the mail gists:
    an attention or school category, or a current-body line that reads as a
    request. Newest first, bounded to the 200 newest rows.
    """
    from service.tools.email_tools import digest_category
    from service.tools.imessage_tools import important_message_reason
    out = []
    for row in sorted(snapshot.rows, key=lambda r: r["ts"], reverse=True)[:200]:
        body = _QUOTED_HISTORY.split(row.get("body", ""))[0]
        current_lines = [line for line in body.splitlines() if important_message_reason("Sender: " + line)]
        if digest_category(row) not in {"attention", "school"} and not current_lines:
            continue
        out.append((row, current_lines))
    return out


def email_record_id(row):
    return key("email", (row["account_id"], row["message_id"]))


def _sender_fields(row):
    from service.tools.email_tools import is_machine_sender
    sender = clean(row.get("sender"), 80)
    return dict(sender=sender, received_ts=row["ts"] if finite(row.get("ts")) else None,
                person=bool(sender) and not is_machine_sender(f"{sender} {row.get('sender_address', '')}"))


def _item(source, identity, title, reason, *, quote="", due=None, priority=2, reference=""):
    return dict(id=key(source, identity), source=source, title=clean(title),
                reason=clean(reason, 500), quote=clean(quote, 500),
                due_ts=due if finite(due) else None, priority=priority,
                reference=str(reference or identity), suggested_minutes=25,
                needs_review=source in {"email", "messages", "notes"})


def _behavior_demand(demand, timezone, now, checked_at):
    """Admit durable v1 work proof independently of recurrence scores.

    The consumer cannot re-resolve a source revision: the public producer owns
    that check. Missing endpoint/original receipt/analysis proof is withheld,
    including the development producer's older cache-only demand shape.
    """
    if not isinstance(demand, dict):
        return None
    source, ident = demand.get("source"), demand.get("id")
    quote, endpoint, reference = demand.get("quote"), demand.get("endpoint"), demand.get("reference")
    if (source not in ("email", "messages", "notes", "calendar", "reminders")
            or not isinstance(ident, str) or not 1 <= len(ident) <= 200
            or demand.get("kind") not in ("preparation", "task", "meeting_request")
            or not isinstance(demand.get("timezone"), str) or demand.get("ownership") not in ("user", "unknown")
            or demand.get("status") not in ("tentative", "unresolved")
            or demand.get("confidence") is not None or demand.get("importance") != "unknown"
            or demand.get("action_authority") != "none"
            or not isinstance(quote, str) or not 8 <= len(quote) <= 800
            or not isinstance(endpoint, dict) or not isinstance(reference, dict)
            or reference.get("source") != source or not _refs(demand.get("evidence"))
            or not _original(demand.get("source_receipt_at"), checked_at)
            or not _original(demand.get("analyzed_at"), checked_at)
            or not 0 <= now - demand["source_receipt_at"] <= SOURCE_RECEIPT_MAX_AGE[source]):
        return None
    try:
        ZoneInfo(demand["timezone"])
        quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    except (UnicodeEncodeError, ValueError, KeyError):
        return None
    start, end = endpoint.get("start"), endpoint.get("end")
    if (endpoint.get("source") != source or endpoint.get("record_id") != demand.get("source_id")
            or not isinstance(demand.get("source_id"), str) or not demand["source_id"]
            or demand["source_id"] not in demand["evidence"]
            or not isinstance(endpoint.get("revision"), str) or not endpoint["revision"]
            or type(start) is not int or type(end) is not int or start < 0 or end - start != len(quote)
            or endpoint.get("sha256") != quote_hash):
        return None
    observed = demand.get("observed_at")
    if observed != endpoint.get("source_time") or (observed is not None and (not finite(observed) or observed > now)):
        return None
    due, span, precision = demand.get("due_ts"), demand.get("due_range"), demand.get("deadline_precision")
    if precision == "unknown":
        if span is not None or due is not None:
            return None
    elif (precision not in ("instant", "date") or not isinstance(span, list) or len(span) != 2
          or not all(finite(v) for v in span) or span[0] >= span[1]
          or (precision == "instant" and (not finite(due) or due != span[0]))
          or (precision != "instant" and due is not None)):
        return None
    duration, basis = demand.get("suggested_minutes"), demand.get("duration_basis")
    estimated = type(duration) is int and 1 <= duration <= 480 and basis == "adjustable default; not observed effort"
    ownership = demand["ownership"]
    reason = ("Tentative source-backed work; review relevance. " if ownership == "user" else
              "Ownership is unknown; review the source before accepting work. ")
    if precision == "unknown":
        reason += "Deadline unknown; review timing. "
    elif precision == "date":
        reason += "Source gives a date range, not an exact deadline. "
    reason += ("Adjustable default duration; not observed effort. " if estimated else
               "Duration unknown; choose an estimate before adding work. ")
    reason += "Importance is unknown. Ownership, effort and relevance remain uncertain."
    uncertainty = demand.get("uncertainty", [])
    if isinstance(uncertainty, str):
        uncertainty = [uncertainty]
    uncertainty = [clean(v, 120) for v in uncertainty[:5] if isinstance(v, str)] if isinstance(uncertainty, list) else []
    if uncertainty:
        reason += " Uncertainty: " + "; ".join(uncertainty)
    item = _item("behavior", ident, demand.get("title") or "Review source-backed work", reason,
                 quote=quote, due=due, priority=3)
    # Preserve the exact quote and structured source locator. Never synthesize
    # native URLs, a date instant, a confidence or a new publication timestamp.
    item.update(quote=quote, reference=json.dumps(reference, sort_keys=True, ensure_ascii=False), source_id=demand["source_id"],
        source_reference=deepcopy(reference), evidence=deepcopy(demand["evidence"]),
        endpoint=deepcopy(endpoint), source_receipt_at=demand["source_receipt_at"],
        analyzed_at=demand["analyzed_at"], analysis_timezone=demand["timezone"],
        observed_at=observed, confidence=None,
        time_evidence=deepcopy(demand.get("time_evidence")),
        earliest_start=demand.get("earliest_start"),
        priority_dimensions=deepcopy(demand.get("priority_dimensions")),
        due_range=deepcopy(span), deadline_precision=precision, status=demand["status"],
        needs_review=True, task_kind="task", ownership=ownership, demand_kind=demand["kind"],
        duration_basis=basis, importance="unknown", uncertainty=uncertainty,
        importance_hypothesis=demand.get("importance_hypothesis", "unknown"),
        planning_duration=(duration if estimated and ownership == "user" and precision != "unknown"
                           and demand["status"] == "tentative" and span[1] > now else None),
        suggested_minutes=duration if estimated else 25,
        planning_title="Review: " + item["title"])
    return item


def _remaining(start, end, occupied):
    """Keep only unlisted time; calendar commitments and explicit pins win."""
    parts = [(start, end)]
    for a, b in sorted(occupied):
        parts = [(x, y) for lo, hi in parts for x, y in
                 ((lo, min(hi, a)), (max(lo, b), hi)) if x < y]
    return parts


def assess(day, timezone, state, evidence, *, now, behavior=None, ignored_windows=(), profile=None):
    """Return explainable attention items and a proposed itinerary, all transient.

    ``evidence`` contains source-labelled records and explicit readiness/coverage.
    ``behavior`` is an optional public availability result, never raw activity.
    ``profile`` is the user's own profile (service/memory/profile.py) or None;
    an item naming one of its people is raised to the top with the reason said.
    Its windows need current evidence, increasing bounds, >=0.65 recurrence strength,
    a named timezone, and a human-readable explanation. No percentage is shown:
    these scores are evidence strength, not calibrated forecast probabilities.
    """
    lo, hi = day_bounds(day, timezone)
    if not finite(now):
        raise ValueError("now must be finite")
    items, coverage, suggestions = [], [], []
    seen = set()
    receipt = state["sources"].get("calendar", {})
    from service.tools.everyday import _native_receipt_current
    if not _native_receipt_current(receipt, now):
        state = {**state, "sources": {**state["sources"], "calendar": {**receipt, "available": False,
                  "reason": "Calendar receipt is missing, stale or partial; refresh before planning flexible work"}}}
    base = build_plan(day, timezone, state["tasks"], state["commitments"],
                      state["preferences"], state["sources"], now=now)
    for source in SOURCES:
        info = evidence.get(source, {})
        records = info.get("records") or []
        coverage.append(dict(source=source, ready=info.get("ready") is True,
                             detail=clean(info.get("detail") or "Source evidence is unavailable", 500),
                             inspected=info.get("inspected", len(records)),
                             omitted=info.get("omitted", 0)))
        if info.get("ready") is not True:
            records = [r for r in records if r.get("wisp_only") is True]
        for record in records:
            if record.get("status", "active") in {"done", "completed", "dismissed", "cancelled"}:
                continue
            identity = record.get("id") or record.get("reference")
            if not identity or (source, str(identity)) in seen:
                continue
            seen.add((source, str(identity)))
            title, quote = record.get("title", ""), record.get("quote", "")
            if not clean(title):
                continue
            due = record.get("due_ts")
            if source == "calendar":
                start, end = record.get("start"), record.get("end")
                if not finite(start) or start >= hi or (finite(end) and end <= max(lo, now)):
                    continue
                item = _item(source, identity, title, "Scheduled commitment; allow time to prepare.",
                             due=start, reference=record.get("reference", ""))
            elif source == "reminders":
                if finite(due) and due >= hi:
                    continue
                if not finite(due) and record.get("undated") is not True:
                    continue
                overdue = finite(due) and due < now
                item = _item(source, identity, title,
                             "Past due; review what is still needed." if overdue else
                             "Due before the selected day; review alongside today's work." if finite(due) and due < lo else
                             "Due in this day." if finite(due) else "No due date; choose whether to work on it today.",
                             due=due, priority=1 if overdue else 2,
                             reference=record.get("reference", ""))
                if record.get("wisp_only"):
                    item.update(source="wisp", reason="Wisp-only reminder; Apple copy/status unverified. " + item["reason"])
            else:
                # The source adapter supplies an existing grounded relevance rule.
                # Unknown ownership remains explicit; receipt time never becomes due time.
                if not record.get("review_reason") or not clean(quote):
                    continue
                item = _item(source, identity, title, record["review_reason"], quote=quote,
                             reference=record.get("reference", ""))
                item["suggested_minutes"] = 15
                for field in _COMMUNICATION_FIELDS:
                    value = record.get(field)
                    if isinstance(value, str):
                        value = clean(value, 120)
                    if value not in (None, "") and (not isinstance(value, float) or finite(value)):
                        item[field] = value
            items.append(item)

    supported_behavior = valid_availability(behavior, timezone, now)
    if supported_behavior:
        for demand in behavior.get("demands", [])[:100]:
            item = _behavior_demand(demand, timezone, now, behavior["evidence_checked_at"])
            if item is not None:
                items.append(item)

    for task in state["tasks"]:
        if task.get("status") == "active" and task.get("day") == day:
            carried = task.get("carried_from")
            items.append(_item("wisp", task["id"], task["title"],
                               f"Carried over from {carried}. On your day plan." if carried else "On your day plan.",
                               due=task.get("due_ts"), priority=task["priority"]))
    if profile:
        from service.memory.profile import prioritize
        items = prioritize(items, profile)
    items.sort(key=lambda i: (i["priority"], i["due_ts"] if i["due_ts"] is not None else float("inf"),
                              SOURCES.index(i["source"]) if i["source"] in SOURCES else -1, i["id"]))

    # Keep authored work primary and avoid a duplicate suggestion with the same
    # title as an active authored task. Cross-source evidence is not otherwise
    # merged by title: two equally named source records can be different tasks.
    authored = {clean(t["title"]).casefold() for t in state["tasks"] if t["status"] == "active"}
    suggested_titles = set()
    for item in items:
        if item["source"] in {"calendar", "wisp"} or len(suggestions) >= MAX_SUGGESTIONS:
            continue
        if item["source"] == "behavior" and item.get("planning_duration") is None:
            continue
        title = item["title"]
        title_key = title.casefold()
        if title_key in authored or (item["source"] != "reminders" and title_key in suggested_titles):
            continue
        # Undated reminders are useful attention evidence but not automatically
        # selected over obligations due today. Communications get review time,
        # not an invented assignment or a fabricated deadline.
        if item["source"] == "reminders" and item["due_ts"] is None:
            continue
        suggested_titles.add(title_key)
        planned_title = item.get("planning_title") or ("Review: " + title if item["needs_review"] else title)
        suggestions.append(dict(id="daily:" + item["id"], title=planned_title, day=day,
            timezone=timezone, kind=item.get("task_kind", "task"), duration_minutes=item["suggested_minutes"],
            priority=item["priority"], due_ts=item["due_ts"] if item["due_ts"] and item["due_ts"] > max(now, lo) else None,
            pinned_start=None, status="active", revision=0, evidence_id=item["id"]))

    behavioral, ignored, behavior_detail = [], set(ignored_windows), "No current availability evidence; no behavioral time reserved."
    pinned_ids = {t["id"] for t in state["tasks"] if t.get("pinned_start") is not None}
    occupied = [(max(lo, b["start"]), min(hi, b["end"])) for b in base["blocks"]
                if finite(b.get("end")) and b["end"] > b["start"]
                and (b.get("task_id") is None or b["task_id"] in pinned_ids)]
    windows = []
    if supported_behavior:
        behavior_detail = clean(behavior.get("detail") or "Tentative hypotheses; availability and coverage remain uncertain.", 500)
        for window in (behavior.get("windows") or [])[:100]:
            if not isinstance(window, dict):
                continue
            ident, start, end = window.get("id"), window.get("start"), window.get("end")
            score, stamp, kind = window.get("confidence"), window.get("observed_at"), window.get("kind")
            if (not isinstance(ident, str) or not 1 <= len(ident) <= 200 or ident in ignored
                    or kind not in ("busy", "possible_unavailability")
                    or window.get("current") is not True or not clean(window.get("because"))
                    or window.get("timezone") != timezone or not _refs(window.get("evidence"))
                    or not all(finite(v) for v in (start, end, score, stamp))
                    or not 0.65 <= score <= 1 or not 0 <= now - stamp <= 21 * 86400
                    or end <= start or end <= max(now, lo) or start >= hi
                    or window.get("coverage") not in ("adequate", "complete")):
                continue
            if kind == "possible_unavailability" and (
                    window.get("prediction_target") != "computer_activity"
                    or window.get("availability") != "unknown" or window.get("basis") != "inferred"
                    or window.get("action_authority") != "none" or not isinstance(window.get("because"), str)):
                continue
            windows.append(window)

    def reserve(kind, occupied):
        for window in windows:
            if window["kind"] != kind:
                continue
            away = kind == "possible_unavailability"
            for index, (a, b) in enumerate(_remaining(max(lo, window["start"]), min(hi, window["end"]), occupied)):
                behavioral.append(dict(id=("behavior-away:" if away else "behavior:") + window["id"] + ":" + str(index),
                    source="manual", kind="event", title="Possible time away" if away else
                        "Likely busy · " + clean(window.get("title") or "Usual routine"),
                    when_ts=a, end_ts=b, all_day=False, pattern_id=window["id"], window_kind=kind,
                    explanation=clean(window["because"], 500), confidence=window["confidence"],
                    coverage=window["coverage"], evidence=deepcopy(window["evidence"]),
                    observed_at=window["observed_at"], validation=window.get("validation"),
                    semantic_reason=window.get("semantic_reason"), uncertainty=window.get("uncertainty")))

    reserve("busy", occupied)
    authored_plan = build_plan(day, timezone, state["tasks"],
                               state["commitments"] + behavioral, state["preferences"], state["sources"], now=now)
    authored_starts = {b["task_id"]: b["start"] for b in authored_plan["blocks"] if b.get("task_id") is not None}
    # Computer inactivity does not establish personal availability. Its optional
    # advice can use only gaps left AFTER fitting authored work around busy time.
    advisory_occupied = [(max(lo, b["start"]), min(hi, b["end"]) if finite(b.get("end")) else hi)
                        for b in authored_plan["blocks"] if finite(b.get("start"))]
    reserve("possible_unavailability", advisory_occupied)
    # Local copies preserve fitting authored placements without changing pins,
    # revisions, saved tasks or the existing busy-period planning contract.
    reserved_tasks = [dict(t, pinned_start=authored_starts[t["id"]])
                      if t.get("pinned_start") is None and t["id"] in authored_starts else t
                      for t in state["tasks"]]
    proposal = build_plan(day, timezone, reserved_tasks + suggestions,
                          state["commitments"] + behavioral, state["preferences"], state["sources"], now=now)
    source_by_task = {s["id"]: s for s in suggestions}
    behavior_by_id = {b["id"]: b for b in behavioral}
    for block in proposal["blocks"]:
        if block["id"] in behavior_by_id:
            origin = behavior_by_id[block["id"]]
            away = origin["window_kind"] == "possible_unavailability"
            block.update(kind="behavior_away" if away else "behavior", pattern_id=origin["pattern_id"], evidence=origin["explanation"],
                evidence_refs=origin["evidence"], observed_at=origin["observed_at"],
                recurrence_strength=origin["confidence"], validation=origin["validation"],
                semantic_reason=origin["semantic_reason"], uncertainty=origin["uncertainty"])
            if away:
                block.update(prediction_target="computer_activity", availability="unknown", action_authority="none")
            block["warnings"].append(("Tentative · availability is unknown. " if away else "Tentative · ") + origin["explanation"])
        elif block.get("task_id") in source_by_task:
            origin = source_by_task[block["task_id"]]
            block.update(task_id=None, kind="suggestion", evidence_id=origin["evidence_id"])
            block["warnings"].append("Suggested time · duration is an estimate")
    proposal["tasks"] = state["tasks"]
    return dict(day=day, timezone=timezone, generated_at=now, attention=items[:MAX_ITEMS],
                attention_omitted=max(0, len(items) - MAX_ITEMS), source_coverage=coverage,
                behavior_detail=behavior_detail,
                behavior_context=(dict(ready=True, generated_at=behavior["generated_at"],
                    evidence_checked_at=behavior["evidence_checked_at"],
                    source_coverage=deepcopy(behavior.get("source_coverage", {})),
                    associations=deepcopy(behavior.get("associations", []))) if supported_behavior else
                    dict(ready=False, generated_at=None, evidence_checked_at=None, source_coverage={}, associations=[])),
                proposed_plan=proposal,
                suggested_count=len(suggestions), tentative_count=len(behavioral),
                explanation="Your sources establish commitments and work first. Review suggestions and estimated durations are adjustable; behavioral holds reserve only otherwise-unlisted time.")


def render(workspace, *, compact=False):
    """Grounded prose shared by the daily answer, preview and daily email."""
    zone = ZoneInfo(workspace["timezone"])
    clock = lambda stamp: datetime.fromtimestamp(stamp, zone).strftime("%-I:%M %p")
    rows = ["Needs my attention"]
    for item in workspace["attention"][:5 if compact else 12]:
        due = (" · " + datetime.fromtimestamp(item["due_ts"], zone).strftime("%b %-d, %-I:%M %p")) if item["due_ts"] is not None else ""
        if item.get("deadline_precision") == "date" and item.get("due_range"):
            dates = [datetime.fromtimestamp(v, zone).strftime("%b %-d, %-I:%M %p") for v in item["due_range"]]
            due = " · source range [" + dates[0] + ", " + dates[1] + ")"
        rows.append(f"• {item['title']} [{item['source']}]{due} — {item['reason']}")
    if not workspace["attention"]:
        rows.append("No attention items in the available evidence. Check source coverage below.")
    rows.extend(["", "Suggested day plan"])
    plan = workspace["proposed_plan"]
    for block in plan["blocks"][:8 if compact else 30]:
        end = "–" + clock(block["end"]) if block["end"] is not None else " (end unknown)"
        qualifier = (" · tentative · availability unknown" if block["kind"] == "behavior_away" else
                     " · tentative" if block["kind"] == "behavior" else " · estimated" if block["kind"] == "suggestion" else "")
        rows.append(f"• {clock(block['start'])}{end} {block['title']}{qualifier}")
    if not plan["blocks"]:
        rows.append("No time blocks yet.")
    for item in plan["unscheduled"][:5]:
        rows.append(f"Still to fit: {item['title']} — {item['reason']}")
    rows.extend(plan["warnings"])
    rows.extend(["", "Source coverage"])
    rows.extend(f"• {s['source'].title()}: {s['detail']}" for s in workspace["source_coverage"])
    rows.append(workspace["behavior_detail"])
    rows.extend(f"• Behavior {source}: {detail}" for source, detail in
                workspace.get("behavior_context", {}).get("source_coverage", {}).items())
    return "\n".join(rows)


def collect_evidence(store, state, day, timezone, *, now):
    """Capture existing eligible caches only; never request native/model work.

    Each source failure is isolated and visible. Counts disclose bounded reads;
    an empty or omitted source is never presented as a complete all-source scan.
    """
    from service.tools.everyday import _native_receipt_current
    lo, hi = day_bounds(day, timezone)
    result = {}
    health = build_plan(day, timezone, [], state["commitments"], state["preferences"],
                        state["sources"], now=now)["sources"]
    calendar = [dict(id=c["id"], title=c["title"], start=c["when_ts"], end=c.get("end_ts"),
                     reference="calendar:" + str(c.get("source_id") or c["id"]))
                for c in state["commitments"] if c["source"] == "calendar"]
    calendar_current = health["calendar"]["ready"] and _native_receipt_current(state["sources"].get("calendar", {}), now)
    result["calendar"] = dict(ready=calendar_current, records=calendar,
        detail=(health["calendar"]["reason"] if calendar_current else "Calendar receipt is unavailable, stale, partial or outside the selected day")
                + "; cached occurrences, native completeness unverified.")
    try:
        with store.transaction(write=False) as db:
            receipt_row = db.execute("SELECT payload FROM today_source_sync WHERE source='reminders'").fetchone()
            receipt = json.loads(receipt_row["payload"]) if receipt_row else {}
            # Undated items are included for attention but do not acquire a due date.
            count = db.execute("SELECT COUNT(*) FROM commitments WHERE status='active' AND source='reminders' AND when_ts<? AND kind NOT IN ('event','meeting')", (hi,)).fetchone()[0]
            rows = [dict(row) for row in db.execute(
                "SELECT id,source,source_id,title,when_ts,kind FROM commitments "
                "WHERE status='active' AND source='reminders' AND when_ts<? "
                "AND kind NOT IN ('event','meeting') ORDER BY CASE WHEN when_ts>=? THEN 0 ELSE 1 END,when_ts DESC,id LIMIT 500", (hi, lo))]
            manual_count = db.execute("SELECT COUNT(*) FROM commitments WHERE status='active' AND source='manual' AND kind NOT IN ('event','meeting') AND when_ts>=? AND when_ts<?", (now - 14 * 86400, hi)).fetchone()[0]
            manual = [dict(row) for row in db.execute("SELECT id,title,when_ts FROM commitments WHERE status='active' AND source='manual' AND kind NOT IN ('event','meeting') AND when_ts>=? AND when_ts<? ORDER BY when_ts DESC,id LIMIT 100", (now - 14 * 86400, hi))]
            view, undated = store.undated_listing()
            # Overdue reminders the stale policy hides (a week or more past due, not
            # kept) leave the plan and its counts, along with any Wisp twin of them.
            hidden = store.hidden_commitment_ids(now)
            count -= sum(r["id"] in hidden for r in rows)
            manual_count -= sum(r["id"] in hidden for r in manual)
            rows = [r for r in rows if r["id"] not in hidden]
            manual = [r for r in manual if r["id"] not in hidden]
        current = _native_receipt_current(receipt, now)
        selected = [dict(id=r["id"], title=r["title"], due_ts=r["when_ts"],
                         reference=r["source"] + ":" + str(r.get("source_id") or r["id"]))
                    for r in rows[:500] if r["source"] == "reminders" and current]
        selected.extend(dict(id=r["id"], title=r["title"], due_ts=r["when_ts"], wisp_only=True,
                             reference="wisp-reminder:" + r["id"]) for r in manual)
        if current and view.get("freshness") == "current":
            selected.extend(dict(id="undated:" + str(r.get("source_id") or r.get("id")),
                title=r["title"], due_ts=None, undated=True,
                reference="reminders:" + str(r.get("source_id") or r.get("id"))) for r in undated[:100])
        undated_total = max(len(undated), int(view.get("total") or 0))
        result["reminders"] = dict(ready=current, records=selected, inspected=len(rows) + len(manual),
            omitted=max(0, count - len(rows)) + max(0, manual_count - len(manual)) + max(0, undated_total - min(100, len(undated))),
            detail=("Current cached incomplete reminders; due, overdue and undated items included. "
                    f"Native deletion/completeness unverified; {count} dated cached, undated listing {view.get('status', 'unknown')}." if current else "Reminders unavailable, stale or partial; native tasks withheld."))
    except Exception:
        result["reminders"] = dict(ready=False, records=[], detail="Reminders could not be read.")
    try:
        from service.tools import email_tools as mail
        from service.tools.email_tools import raw_email_snapshot, digest_category
        from service.tools.imessage_tools import redact_summary_codes, important_message_reason
        snapshot = raw_email_snapshot()
        records = []
        header_count, header_eligible = 0, False
        if snapshot is not None:
            # Ranking is a reason to inspect the source, never proof a reply
            # or payment is owed. Bodies cannot control destinations/actions.
            # A bounded current-body excerpt can reveal a request hidden
            # behind an innocuous subject. Quoted/forwarded history is not
            # promoted to current work; every result still says review.
            for row, current_lines in important_email_rows(snapshot):
                subject = redact_summary_codes(row["subject"])
                records.append(dict(id=email_record_id(row),
                    title=subject, quote=redact_summary_codes(current_lines[0]) if current_lines else subject, reference="email:" + row["account_id"] + ":" + row["message_id"],
                    review_reason="Review this source for requests or changes; ownership, reply status and deadlines are unverified.",
                    **_sender_fields(row)))
        elif callable(getattr(mail, "email_source_details", None)):
            # The independently owned direct-mail integration exports this
            # per-account freshness contract. Older builds without it withhold
            # headers rather than inventing source age from a ready flag.
            details = mail.email_source_details(part="headers")
            rows = mail.header_rows()
            after = mail.email_source_details(part="headers")
            signature = lambda values: sorted((d.get("account"), d.get("source"), d.get("fresh"),
                d.get("last_success"), d.get("error")) for d in values)
            if signature(details) == signature(after):
                fresh = {d["account"] for d in details if d.get("fresh") is True and not d.get("error")
                         and finite(d.get("last_success")) and 0 <= now - d["last_success"] <= 20 * 60
                         and all(other.get("fresh") is True and not other.get("error") for other in details if other.get("account") == d["account"])}
                selected = [r for r in rows if r.get("account") in fresh and finite(r.get("ts")) and r["ts"] <= now]
                header_count, header_eligible = len(selected), bool(fresh)
                for row in selected[:200]:
                    if digest_category(row) not in {"attention", "school"}:
                        continue
                    subject = redact_summary_codes(row["subject"])
                    identity = (row.get("account_id"), row.get("account"), row.get("message_id"), row["ts"], subject)
                    records.append(dict(id=key("email", identity), title=subject, quote=subject,
                        reference="email-header-cache:" + key("item", identity),
                        review_reason="Header-only review suggestion; read the original email. Body, ownership, reply status and deadlines are unverified.",
                        **_sender_fields(row)))
        result["email"] = dict(ready=snapshot is not None or header_eligible, records=records,
            inspected=min(200, len(snapshot.rows)) if snapshot else min(200, header_count),
            omitted=max(0, len(snapshot.rows) - 200) if snapshot else max(0, header_count - 200),
            detail="Eligible bounded recent-mail snapshot; mailbox history and sent/reply coverage are unverified." if snapshot else
                   "Fresh per-account headers only; body evidence unavailable. Header scan and mailbox coverage remain bounded/unverified." if header_eligible else
                   "No eligible current email snapshot; mail may be missing.")
    except Exception:
        result["email"] = dict(ready=False, records=[], detail="Email could not be assessed.")
    try:
        from service.tools.imessage_tools import (messages_sync_state, summary_message_rows,
                                                 redact_summary_codes, structured_messages_snapshot,
                                                 latest_outgoing_by_context)
        from service.tools.message_digest import split_sender
        ready = messages_sync_state() == "ready"
        rows = summary_message_rows() if ready else []
        outgoing = latest_outgoing_by_context() if ready else {}
        structured = structured_messages_snapshot() if ready else {"records": [], "coverage": {}}
        native = {}
        for row in structured["records"]:
            try:
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if stamp.utcoffset() is None:
                    continue
                body = redact_summary_codes("Sender: " + row["text"]).partition(":")[2].strip()
                native.setdefault((round(stamp.timestamp(), 3), clean(body, 10000)), []).append(row)
            except (ValueError, TypeError, KeyError):
                continue
        records = []
        for stamp, context, text in rows[:100]:
            if not finite(stamp) or not now - 3 * 86400 <= stamp <= now:
                continue
            matches = native.get((round(stamp, 3), clean(text.partition(":")[2], 10000)), [])
            speaker = split_sender(text)[0]
            label = context[len("Group "):].strip().strip('"') if context.startswith("Group ") else context
            record = dict(id=key("messages", (stamp, context, text)), title=redact_summary_codes(text),
                quote=redact_summary_codes(text), reference="messages-cache:" + key("message", (stamp, context, text)),
                original_timestamp=stamp, received_ts=stamp,
                # The person, never the message: "Priya in Family" in a group.
                sender=clean(label if not context.startswith("Group ") or speaker == "Unknown sender"
                             else f"{speaker} in {label}", 80),
                person=True, replied=outgoing.get(context, float("-inf")) > stamp,
                review_reason="Important conversation context; native item identity unavailable in this summary; review the original before choosing an action.")
            if len(matches) == 1:
                source = matches[0]
                record.update(id="message:" + source["guid"], reference="messages:" + source["guid"] + " · chat:" + source["conversation"],
                    review_reason="Important conversation context; review the original before choosing an action.")
            records.append(record)
        message_coverage = structured.get("coverage") or {}
        skipped = message_coverage.get("skipped", 0)
        result["messages"] = dict(ready=ready, records=records, inspected=min(100, len(rows)),
            omitted=max(0, len(rows) - 100) + (skipped if type(skipped) is int else 0),
            detail=("Current-launch cached conversations; cache publication age and exhaustive coverage unknown. "
                    + f"Feed reports {skipped} skipped, {message_coverage.get('truncated', 0)} shortened and {message_coverage.get('malformed', 0)} malformed records.") if ready else
                   "Messages unavailable or still syncing.")
    except Exception:
        result["messages"] = dict(ready=False, records=[], detail="Messages could not be assessed.")
    try:
        from service.tools.notes_tools import raw_notes_snapshot
        from service.tools.imessage_tools import redact_summary_codes
        snapshot = raw_notes_snapshot()
        stamp = snapshot.body_started_at
        ready = (snapshot.origin == "live" and snapshot.latest_receipt.get("available") is True
                 and finite(stamp) and 0 <= now - stamp <= 20 * 60
                 and snapshot.latest_receipt.get("received_at") == snapshot.body_received_at)
        records = []
        if ready:
            for row in snapshot.records:
                # Only explicit unchecked plaintext tasks. Notes modification
                # time never dates a sentence or resolves "tomorrow".
                for index, line in enumerate(str(row["body"]).splitlines()):
                    match = re.match(r"^\s*(?:[-*]\s+\[ \]\s+|TODO:\s+|Action(?: item)?:\s+)(.+)", line, re.I)
                    if match:
                        quote = redact_summary_codes(match.group(1))
                        records.append(dict(id=row["reference"] + ":" + str(index), title=quote, quote=quote,
                            reference=row["reference"],
                            review_reason="Task candidate in note “" + clean(row["title"], 100) + "”; confirm relevance and timing. No deadline inferred."))
        result["notes"] = dict(ready=ready, records=records[:100], inspected=len(snapshot.records),
            omitted=snapshot.omitted + snapshot.malformed + max(0, len(records) - 100),
            detail=(f"Recent bounded plaintext notes; {snapshot.malformed} malformed and {snapshot.omitted} uninspected records; "
                    "only explicit unchecked tasks surfaced; native links and exhaustive checkbox coverage unavailable.") if ready else
                   "Notes unavailable, stale or not verified in this launch; note tasks withheld.")
    except Exception:
        result["notes"] = dict(ready=False, records=[], detail="Notes could not be assessed.")
    return result


def workspace(store, day, timezone, *, now, behavior=None, ignored_windows=(), profile=None):
    """``profile`` is the user's own profile (service/memory/profile.py), read by
    the API layer: this module stays pure over the records it is handed and
    imports nothing from memory."""
    state = store.today_snapshot(day, timezone)
    evidence = collect_evidence(store, state, day, timezone, now=now)
    return assess(day, timezone, state, evidence, now=now, behavior=behavior,
                  ignored_windows=ignored_windows, profile=profile)
