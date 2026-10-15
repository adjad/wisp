"""BehaviorEngine: the single facade over the behavior package.

``service.assistant.behavior_runtime`` calls it in-process on the Mac.

CONTRACT (implemented by the engine workstream; signatures are fixed):
see each method's docstring. Internals live in sibling modules: ``store``
(SQLite), ``categorize`` (title -> category), ``miners`` (patterns),
``predict`` (next horizon), ``nudges`` (candidates + attention budget),
``profile`` (system-prompt card). The engine only loads, wires and persists.
"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol

from service.behavior import miners, nudges as nudge_mod, predict as predict_mod, profile as profile_mod
from service.behavior.categorize import categorize, label_leftovers, needs_category
from service.behavior.store import Store
from service.behavior.timeutil import day_end, day_start, local_dt, resolve_tz, tz_name
from service.behavior.types import FEEDBACK_VERDICTS, Feedback, Nudge, Observation, Pattern, Prediction

MINE_LOOKBACK_S = 16 * 7 * 86400     # routines older than ~4 months say little about next week
NUDGE_HORIZON_S = 24 * 3600          # minimum look-ahead; see nudge_horizon()
MOVE_PAIR_MAX_S = 7 * 86400
# Calendar ids embed int(start) (cal:<sid>:<int(ts)>), so sub-second float drift
# across an integer boundary changes the id. A "move" smaller than this is the
# same event re-read: no event_moved, and no event_cancelled either.
MOVE_MIN_S = 60
# Phrasing runs under the engine lock, so it must stay bounded:
# per generate pass, phrase at most this many nudges, and start no phrase call
# once this much wall time has passed since the pass began (mining included).
# Worst case is ~budget + one phraser timeout; the rest keep their template text.
PHRASE_MAX_PER_PASS = 2
PHRASE_BUDGET_S = 10.0
_SNAPSHOT_ATTRS = ("organizer", "organizer_is_user", "attendees", "calendar", "location", "all_day")


class Phraser(Protocol):
    """Optional LLM helper. Must be safe to be None or to fail (return None):
    the engine then uses its code-composed text."""

    def phrase(self, kind: str, facts: dict) -> str | None:
        """Rewrite a nudge/profile sentence warmly and briefly from ``facts``.
        Must not add facts that are not in ``facts``."""

    def categorize(self, titles: list[str], categories: tuple[str, ...]) -> dict[str, str]:
        """Map event titles to one of ``categories`` (unknown titles omitted)."""


class BehaviorEngine:
    def __init__(self, db_path: str | Path, *, phraser: Phraser | None = None,
                 clock=None, tz: str | None = None) -> None:
        self._store = Store(db_path)
        self._phraser = phraser
        self._clock = clock or time.time
        self._tz = resolve_tz(tz)
        self.tz = tz_name(self._tz)
        # Snapshot diffing and mining read-modify-write the store; serialize them.
        self._lock = threading.RLock()

    def close(self) -> None:
        self._store.close()

    # ---- helpers ------------------------------------------------------------
    def _now(self, now: float | None) -> float:
        return float(self._clock() if now is None else now)

    @staticmethod
    def _prepare(o: Observation) -> Observation:
        """Fill the keyword category for calendar/reminder kinds at ingest."""
        if needs_category(o.kind) and not o.category:
            return replace(o, category=categorize(o.subject, o.attrs))
        return o

    def _load(self, *, ask_phraser: bool = False, **query: Any) -> list[Observation]:
        """Timeline slice with phraser-labelled categories applied to "other" titles.

        Only ``mine`` asks the phraser (``ask_phraser``); predict/nudges reuse
        its cached labels so they never wait on a model."""
        obs = self._store.observations(**query)
        leftovers = [o.subject for o in obs if needs_category(o.kind) and (o.category or "other") == "other"]
        if not leftovers:
            return obs
        cache: dict[str, str] = self._store.meta_get("category_cache", {}) or {}
        if ask_phraser and self._phraser is not None:
            if label_leftovers(leftovers, cache, getattr(self._phraser, "categorize", None)):
                self._store.meta_set("category_cache", cache)
        out = []
        for o in obs:
            if (needs_category(o.kind) and (o.category or "other") == "other"
                    and cache.get(o.subject, "other") != "other"):
                o = replace(o, category=cache[o.subject])
            out.append(o)
        return out

    def _live_patterns(self) -> list[Pattern]:
        return [p for p in self._store.patterns() if p.status != "rejected"]

    # ---- ingest -----------------------------------------------------------
    def observe(self, observations: Iterable[Observation]) -> int:
        """Upsert by Observation.id (idempotent). Returns rows inserted/updated."""
        with self._lock:
            return self._store.upsert_observations(self._prepare(o) for o in observations)

    def observe_snapshot(self, source: str, observations: Iterable[Observation], *,
                         window_start: float, window_end: float, taken_at: float) -> dict:
        """Replace-set semantics for one source (e.g. the full calendar window).
        Diffs against the previous snapshot of that source and EMITS lifecycle
        observations: an upcoming event (start > taken_at) that vanished ->
        ``event_cancelled``; same source_id at a different time (recurring:
        nearest previous occurrence within 7 days that also vanished) ->
        ``event_moved`` (attrs from_ts/to_ts). Past events that age out of the
        window are NOT cancellations. Returns {"upserted", "cancelled", "moved"}.

        Details: a vanished upcoming event's own ``event`` row is removed (it is
        no longer part of the plan) and replaced by ``cancel:<id>`` /
        ``move:<id>``; an id that re-appears later clears its ``cancel:`` and
        ``move:`` rows. A pairing closer than MOVE_MIN_S (60s) is the same event
        under a drifted id: neither a move nor a cancellation.
        A recurring occurrence that only appeared because the window slid
        forward is never paired as a move (it must fall inside the previous
        window). A vanished event that already started is left alone."""
        current = [self._prepare(o) for o in observations]
        with self._lock:
            prev = self._store.get_snapshot(source)
            upserted = self._store.upsert_observations(current)
            # An id that is back is not cancelled or moved after all: drop both
            # derived rows (a same-id reschedule writes move:<id>:<old ts>, which
            # stays as history).
            self._store.delete_observations(
                prefix + o.id for o in current for prefix in ("cancel:", "move:"))
            lifecycle: list[Observation] = []
            gone: list[str] = []
            if prev is not None:
                lifecycle, gone = self._diff(prev, current, window_start, window_end, taken_at)
            self._store.upsert_observations(lifecycle)
            self._store.delete_observations(gone)
            self._store.put_snapshot(source, taken_at=taken_at, window_start=window_start,
                                     window_end=window_end, items=[_snap_item(o) for o in current])
            if (source=="calendar" and prev is not None
                    and 0<taken_at-prev["taken_at"]<=600):
                self._store.record_coverage("calendar_changes",prev["taken_at"],taken_at,"complete",taken_at)
        return {"upserted": upserted,
                "cancelled": sum(1 for o in lifecycle if o.kind == "event_cancelled"),
                "moved": sum(1 for o in lifecycle if o.kind == "event_moved")}

    def _diff(self, prev: dict, current: list[Observation], window_start: float, window_end: float,
              taken_at: float) -> tuple[list[Observation], list[str]]:
        """Lifecycle observations for one snapshot transition, and the ids of
        vanished upcoming events whose ``event`` rows should go."""
        prev_items = {it["id"]: it for it in prev["items"]}
        cur = {o.id: o for o in current}
        out: list[Observation] = []
        # Same id at a new time (sources whose ids survive a reschedule).
        for oid, o in cur.items():
            it = prev_items.get(oid)
            if (it and it.get("kind", "event") == "event" and o.kind == "event"
                    and float(it["ts"]) > taken_at and abs(float(it["ts"]) - o.ts) >= MOVE_MIN_S):
                out.append(_moved_obs(it, o, taken_at, f"move:{oid}:{int(float(it['ts']))}"))
        vanished = [it for oid, it in prev_items.items()
                    if oid not in cur and it.get("kind", "event") == "event"
                    and float(it["ts"]) > taken_at and window_start <= float(it["ts"]) < window_end]
        fresh = [o for oid, o in cur.items() if oid not in prev_items and o.kind == "event"]
        prev_count: dict[str, int] = {}
        for it in prev_items.values():
            if it.get("source_id"):
                prev_count[it["source_id"]] = prev_count.get(it["source_id"], 0) + 1
        cur_count: dict[str, int] = {}
        for o in current:
            if o.source_id:
                cur_count[o.source_id] = cur_count.get(o.source_id, 0) + 1
        by_sid: dict[str, list[dict]] = {}
        for it in vanished:
            if it.get("source_id"):
                by_sid.setdefault(it["source_id"], []).append(it)
        pairs: list[tuple[dict, Observation]] = []
        used_new: set[str] = set()
        for sid, gone_items in by_sid.items():
            # A one-off event that moved anywhere is a move; a recurring series
            # pairs each vanished occurrence with the nearest new one within 7
            # days that the previous snapshot could have seen.
            single = prev_count.get(sid, 0) == 1 and cur_count.get(sid, 0) == 1
            options = []
            for it in gone_items:
                for o in (x for x in fresh if x.source_id == sid):
                    delta = abs(o.ts - float(it["ts"]))
                    recurring_ok = (delta <= MOVE_PAIR_MAX_S
                                    and prev["window_start"] <= o.ts < prev["window_end"])
                    if single or recurring_ok:
                        options.append((delta, it["id"], o.id, it, o))
            taken_old: set[str] = set()
            for _, old_id, new_id, it, o in sorted(options, key=lambda x: (x[0], x[1], x[2])):
                if old_id in taken_old or new_id in used_new:
                    continue
                taken_old.add(old_id)
                used_new.add(new_id)
                pairs.append((it, o))
        paired = {it["id"] for it, _ in pairs}
        for it, o in pairs:
            if abs(o.ts - float(it["ts"])) >= MOVE_MIN_S:
                out.append(_moved_obs(it, o, taken_at, "move:" + it["id"]))
            # else: float drift renamed the id; the old row still goes (it is in
            # ``gone``), the new row is already upserted, nothing is emitted.
        for it in vanished:
            if it["id"] not in paired:
                out.append(_cancelled_obs(it, taken_at))
        return out, [it["id"] for it in vanished]

    # ---- learning -----------------------------------------------------------
    def mine(self, now: float | None = None) -> list[Pattern]:
        """Run all miners over the timeline; persist; keep user status
        (confirmed/rejected) across re-mining. Returns active+confirmed patterns."""
        now = self._now(now)
        with self._lock:
            obs = self._load(ask_phraser=True, since=now - MINE_LOOKBACK_S, until=now)
            from service.behavior.evidence import pattern_evidence
            mined = [pattern_evidence(p) for p in miners.mine_all(obs, now, self._tz)]
            self._store.replace_patterns(mined, now)
            self._store.meta_set("last_mined_at", now)
            self._store.prune(now)    # retention: observations 180d, nudges 30d
            return self._live_patterns()

    def patterns(self, *, status: str | None = None, kind: str | None = None) -> list[Pattern]:
        return self._store.patterns(status=status, kind=kind)

    def set_pattern_status(self, pattern_id: str, status: str) -> Pattern:
        """status: active | confirmed | rejected. Rejected patterns never drive
        predictions/nudges/profile until set back to active/confirmed."""
        with self._lock:
            return self._store.set_pattern_status(pattern_id, status)

    # ---- predicting / acting ------------------------------------------------
    def nudge_horizon(self, now: float) -> float:
        """Look-ahead for predict()'s default and generate_nudges(): at least 24h,
        and always through the END OF TOMORROW (local). A rolling 24h would hide
        tomorrow afternoon's meetings from an early-afternoon run (a run at 13:00
        drops a 14:00-16:00 run tomorrow, 25-27h out)."""
        tomorrow = local_dt(now, self._tz).date() + timedelta(days=1)
        return max(float(NUDGE_HORIZON_S), day_end(tomorrow, self._tz) - now)

    def _events_for(self, now: float, horizon_s: float) -> list[Observation]:
        """Events from local midnight today to the end of the horizon's last day,
        so upcoming events can be placed in their full back-to-back runs."""
        first = day_start(local_dt(now, self._tz).date(), self._tz)
        last = day_end(local_dt(now + horizon_s, self._tz).date(), self._tz)
        return self._load(kinds=["event"], since=first, until=last)

    def predict(self, now: float | None = None, horizon_s: float | None = None) -> list[Prediction]:
        """Upcoming activities, cancel risks, contacts and habit conflicts,
        using patterns + upcoming ``event`` observations already in the timeline.
        ``horizon_s`` None (default) = ``nudge_horizon(now)``, the same window
        generate_nudges() uses (>= 24h, through the end of tomorrow)."""
        now = self._now(now)
        if horizon_s is None:
            horizon_s = self.nudge_horizon(now)
        return predict_mod.predict(self._live_patterns(), self._events_for(now, horizon_s), now,
                                   horizon_s, self._tz)

    def generate_nudges(self, now: float | None = None) -> list[Nudge]:
        """Create new nudges that are due (budget ~4/day, per-kind cooldown,
        quiet hours 22:00-07:00 except day_preview after 07:00, mute after
        feedback). Persist with increasing ``seq``; dedupe by Nudge.id.
        Returns only newly created ones."""
        now = self._now(now)
        started = time.monotonic()
        with self._lock:
            if self._store.meta_get("last_mined_at") is None:
                self.mine(now)
            patterns = self._live_patterns()
            if not patterns:
                return []
            horizon = self.nudge_horizon(now)
            events = self._events_for(now, horizon)
            preds = predict_mod.predict(patterns, events, now, horizon, self._tz)
            today = local_dt(now, self._tz).date()
            last_messages = {}
            for p in patterns:
                if p.kind == "contact_rhythm":
                    li = self._store.last_observation("message_in", p.subject, before=now)
                    lo = self._store.last_observation("message_out", p.subject, before=now)
                    last_messages[p.subject] = (li.ts if li else None, lo.ts if lo else None)
            inputs = nudge_mod.Inputs(
                now=now, tz=self._tz, patterns=patterns, predictions=preds,
                events={e.id: e for e in events},
                today_events=[e for e in events if local_dt(e.ts, self._tz).date() == today],
                last_messages=last_messages)
            candidates = nudge_mod.build_candidates(inputs)
            fb = nudge_mod.feedback_state(self._store.feedback_since(now - nudge_mod.FEEDBACK_MEMORY_S), now)
            recent = self._store.nudges_created_since(day_start(today - timedelta(days=1), self._tz))
            chosen = nudge_mod.select(candidates, now=now, tz=self._tz, recent=recent, fb=fb,
                                      exists=self._store.has_nudge)
            created = []
            phrased = 0
            for n in chosen:      # highest priority first, so the best ones get phrased
                if (self._phraser is not None and self._can_phrase(n) and phrased < PHRASE_MAX_PER_PASS
                        and time.monotonic() - started < PHRASE_BUDGET_S):
                    phrased += 1
                    n = self._phrase(n)
                stored = self._store.insert_nudge(n)
                if stored is not None:
                    created.append(stored)
            return created

    def _can_phrase(self, n: Nudge) -> bool:
        """Only a message-only rhythm backed by retained message evidence.

        A legacy/mixed contact label, missing evidence or a stale calendar
        reference cannot authorize presenting scheduled calls as communication.
        """
        if n.kind != "contact_window" or not n.pattern_ids:
            return False
        for pid in n.pattern_ids:
            p = self._store.get_pattern(pid)
            from service.behavior.evidence import _contact_source_counts
            calls, messages = _contact_source_counts(p)
            if (p is None or p.kind != "contact_rhythm" or p.status == "rejected"
                    or p.stats.get("basis") != "observed" or calls or not messages
                    or type(p.stats.get("calls")) is not int or p.stats["calls"] != 0 or not p.evidence):
                return False
            for oid in p.evidence:
                o = self._store.get_observation(oid)
                if (o is None or o.kind not in {"message_in", "message_out"}
                        or o.source != "messages" or o.subject != p.subject):
                    return False
        return True

    def _phrase(self, n: Nudge) -> Nudge:
        """Let the optional model reword the body; the title and the evidence
        line stay code-composed. Any failure keeps the code text."""
        if self._phraser is None or not self._can_phrase(n):
            return n
        facts = {"title": n.title, "body": n.body, "because": n.because,
                 "actions": [a.label for a in n.actions if a.label]}
        try:
            text = self._phraser.phrase(n.kind, facts)
        except Exception:  # noqa: BLE001 - the phraser is optional by contract
            return n
        if isinstance(text, str) and text.strip():
            return replace(n, body=text.strip()[:400])
        return n

    def nudges_since(self, cursor: int = 0, limit: int = 50) -> tuple[list[Nudge], int]:
        """Nudges with seq > cursor (oldest first) and the new cursor."""
        items = self._store.nudges_since(cursor, limit)
        return items, (items[-1].seq if items else int(cursor))

    def feedback(self, fb: Feedback) -> None:
        """Record verdict. Two 'wrong'/'not_now' for the same nudge kind+pattern
        within 7 days mutes it for 7 days; 'useful' raises its priority;
        'undo' counts as 'wrong' for the auto action (it also counts toward the
        mute, and that kind+pattern's auto actions become offers)."""
        if fb.verdict not in FEEDBACK_VERDICTS:
            raise ValueError(f"unknown verdict {fb.verdict!r}")
        with self._lock:
            n = self._store.get_nudge(fb.nudge_id)
            kind = n.kind if n else ""
            key = nudge_mod.pattern_key(n) if n else ""
            self._store.add_feedback(fb.nudge_id, fb.verdict, fb.ts, kind, key)
            if n is not None:
                self._store.set_nudge_status(fb.nudge_id, fb.verdict)
            self._store.upsert_observations([Observation(
                id=f"fb:{fb.nudge_id}:{int(fb.ts)}:{fb.verdict}", kind="nudge_feedback", ts=fb.ts,
                subject=kind, source="wisp",
                attrs={"nudge_id": fb.nudge_id, "verdict": fb.verdict, "pattern_id": key})])

    # ---- personal context ---------------------------------------------------
    def history_status(self) -> dict:
        from service.behavior.history import History
        return History(self._store).status()

    def history_next(self, source: str) -> dict:
        from service.behavior.history import History
        with self._lock:
            return History(self._store).next(source, self._now(None))

    def history_control(self, source: str, command: str) -> dict:
        from service.behavior.history import History
        with self._lock:
            return History(self._store).control(source, command, self._now(None))

    def history_ingest(self, source: str, payload: dict) -> dict:
        from service.behavior.history import History
        with self._lock:
            result = History(self._store).ingest(source,payload,self._now(None),self._prepare)
            # Mine only recent evidence; old scans must not revive obsolete routines.
            if result.get("retained_rows",0):
                self.mine()
            return result

    def record_coverage(self, source: str, start: float, end: float, state: str, now: float) -> None:
        self._store.record_coverage(source, start, end, state, now)

    def weekly(self, now: float | None = None) -> dict:
        from service.behavior.weekly import weekly_report, render_report
        now = self._now(now)
        from service.behavior.timeutil import day_start, local_dt
        start = day_start(local_dt(now, self._tz).date() - timedelta(days=35), self._tz)
        with self._lock:
            # Include events beginning before the report if their ends overlap.
            obs = self._load()
            report = weekly_report(obs,self._store.coverage(),now,self._tz)
        report["summary"] = render_report(report)
        return report

    def profile_card(self, now: float | None = None, max_bytes: int = 700) -> str:
        """Compact always-on 'who you are' text for the system prompt: top
        confirmed/high-confidence patterns + today's predictions. UTF-8 bytes
        <= max_bytes, whole lines only. Empty string when nothing is known."""
        now = self._now(now)
        patterns = self._live_patterns()
        if not patterns:
            return ""
        horizon = max(0.0, day_end(local_dt(now, self._tz).date(), self._tz) - now)
        preds = predict_mod.predict(patterns, self._events_for(now, horizon), now, horizon, self._tz)
        return profile_mod.profile_card(patterns, preds, now, self._tz, max_bytes)

    def describe(self, subject: str | None = None, now: float | None = None) -> dict:
        """For the describe_me / my_patterns tools: {"patterns": [...dicts],
        "predictions": [...dicts], "summary": str}. ``subject`` filters
        case-insensitively on pattern subject/statement."""
        now = self._now(now)
        return profile_mod.describe(self._store.patterns(), self.predict(now), subject)

    def status(self) -> dict:
        """Counts per observation kind, pattern count, last mined at, nudge cursor."""
        counts = self._store.count_by_kind()
        by_status: dict[str, int] = {}
        for p in self._store.patterns():
            by_status[p.status] = by_status.get(p.status, 0) + 1
        return {"observations": counts, "observation_total": sum(counts.values()),
                "patterns": sum(by_status.values()), "patterns_by_status": by_status,
                "last_mined_at": self._store.meta_get("last_mined_at"),
                "nudge_cursor": self._store.max_seq(), "nudges": self._store.nudge_count(),
                "schema_version": self._store.meta_get("schema_version"), "tz": self.tz}


# ---------------------------------------------------------------- lifecycle obs

def _snap_item(o: Observation) -> dict:
    return {"id": o.id, "kind": o.kind, "ts": o.ts, "end_ts": o.end_ts, "subject": o.subject,
            "source": o.source, "category": o.category, "source_id": o.source_id,
            "attrs": {k: o.attrs[k] for k in _SNAPSHOT_ATTRS if k in o.attrs}}


def _cancelled_obs(it: dict, taken_at: float) -> Observation:
    start, end = float(it["ts"]), it.get("end_ts")
    attrs = dict(it.get("attrs") or {})
    attrs.update(start_ts=start, end_ts=end, subject=it.get("subject", ""),
                 category=it.get("category", ""), source_id=it.get("source_id", ""),
                 detected_at=taken_at)
    return Observation(id="cancel:" + it["id"], kind="event_cancelled", ts=start,
                       subject=it.get("subject", ""), source=it.get("source", ""), end_ts=end,
                       category=it.get("category", ""), source_id=it.get("source_id", ""), attrs=attrs)


def _moved_obs(it: dict, new: Observation, taken_at: float, obs_id: str) -> Observation:
    start, end = float(it["ts"]), it.get("end_ts")
    attrs = dict(it.get("attrs") or {})
    attrs.update(from_ts=start, to_ts=new.ts, from_end_ts=end, to_end_ts=new.end_ts,
                 subject=it.get("subject", ""), category=it.get("category", ""),
                 source_id=it.get("source_id", ""), new_id=new.id, detected_at=taken_at)
    return Observation(id=obs_id, kind="event_moved", ts=start, subject=it.get("subject", ""),
                       source=it.get("source", ""), end_ts=end, category=it.get("category", ""),
                       source_id=it.get("source_id", ""), attrs=attrs)
