"""Mac-side runtime for the behavior engine: feed it, run it, act on it.

Off unless ``WISP_BEHAVIOR=1``, ``WISP_FOUNDER_DEMO=1`` or the full remote
build (``WISP_REMOTE_ONLY=1`` + ``WISP_REMOTE_FULL=1``) turns it on;
``WISP_BEHAVIOR=0`` always wins. ``WISP_BEHAVIOR_SYNTHETIC_ONLY=1`` (implied
by the founder demo only) learns from the synthetic persona instead of real
data; the full remote build learns from real activity.

Brain
    ``local`` (default): an in-process ``BehaviorEngine`` at
    ``<WISP_HOME>/behavior.db``. ``remote``: ``RemoteEngine`` at
    ``WISP_BRAIN_URL`` (``WISP_BRAIN_MODE=remote`` or a URL alone selects it),
    bearer token from ``WISP_BRAIN_TOKEN`` or Keychain (service
    ``com.wisp.inference``, account ``wispbrain:<sha256(url)>``, the same
    origin-bound scheme as provider credentials). Any remote failure falls back
    to the local engine for ``REMOTE_RETRY_S`` so the demo never dies.

Ingestion (every ``INGEST_EVERY_S`` and right after a Calendar sync; each
source is independent and never raises into the scheduler)
    real mode: Calendar (replace-set snapshot so deletes/moves are detected),
    Reminders, Messages metadata (direction, contact, time — never text), the
    user's Wisp asks (a coarse topic label — never text), approvals/denials.
    synthetic mode: ``generate_persona(now)`` once per day (idempotent ids)
    plus ONLY Calendar events in a calendar named ``Wisp Demo``; never real
    Messages, Reminders, Notes or asks.

Acting
    New nudges go through the existing hub as durable ``nudge`` events
    (``dedupe_key="behavior:<nudge id>"``, ``expires_at`` from the nudge).
    ``Action(auto=True)`` of type ``add_reminder``/``add_hold`` is performed on
    Wisp's OWN schedule (never Apple Reminders/Calendar; a hold is a
    ``Tentative: …`` event) and recorded so ``undo`` can delete it.
    ``open_prompt`` is never performed automatically: messages, emails and
    changes to real meetings are only drafted, via a prefilled prompt the user
    approves in the overlay.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("wisp.behavior")

ENGINE_DB = "behavior.db"
RUNTIME_DB = "behavior_runtime.db"
CARD_CACHE = "behavior_profile_card.txt"
EVENT_PREFIX = "behavior:"
EVENT_KIND = "nudge"
BEHAVIOR_CONTEXT = "Added by Wisp from your routine"
HOLD_PREFIX = "Tentative: "
DEMO_CALENDAR = "wisp demo"

TICK_S = 30.0
INGEST_EVERY_S = 300.0
MINE_EVERY_S = 1800.0
NUDGE_EVERY_S = 60.0
REMOTE_RETRY_S = 120.0
CALENDAR_GRACE_S = 180.0   # synthetic: wait this long for a first Calendar read
FORCE_WAIT_S = 30.0        # /behavior/run waits this long for a running tick
# Same event: same title, starts within this window. The persona regenerates
# each day from a seeded RNG over a shifted range, so the same routine event
# can come back minutes away from the copy seeded into the calendar earlier.
CAL_MATCH_S = 3 * 3600.0
CARD_TTL_S = 60.0
CARD_MAX_BYTES = 700
HISTORY_S = 56 * 86400
AHEAD_S = 21 * 86400
ASK_LIMIT = 500


# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------

def founder_demo() -> bool:
    from service.behavior.flags import founder_demo_env
    return founder_demo_env()


def enabled() -> bool:
    from service.behavior.flags import env_enabled
    return env_enabled()


def synthetic_only() -> bool:
    # The Aria demo learns the synthetic persona plus its synthetic calendar
    # (mirrored into the demo store as the "Wisp Demo" calendar), never real data.
    from service.demo.aria import env_requested as aria_demo
    return os.environ.get("WISP_BEHAVIOR_SYNTHETIC_ONLY") == "1" or founder_demo() or aria_demo()


def brain_url() -> str:
    url = os.environ.get("WISP_BRAIN_URL", "").strip()
    if url:
        return url
    try:
        from service.config import models_config
        brain = models_config().get("brain") or {}
        return str(brain.get("url") or "").strip() if isinstance(brain, dict) else ""
    except Exception:  # noqa: BLE001
        return ""


def brain_mode() -> str:
    from service.demo.aria import env_requested as aria_demo
    if aria_demo():
        return "local"   # a remote brain is shared with real mode; the demo never feeds it
    mode = os.environ.get("WISP_BRAIN_MODE", "").strip().lower()
    if not mode:
        try:
            from service.config import models_config
            brain = models_config().get("brain") or {}
            mode = str(brain.get("mode") or "").strip().lower() if isinstance(brain, dict) else ""
        except Exception:  # noqa: BLE001
            mode = ""
    if mode not in {"local", "remote"}:
        mode = "remote" if os.environ.get("WISP_BRAIN_URL", "").strip() else "local"
    return mode


def _home() -> Path:
    # The Aria demo's synthetic persona learns into its own state directory
    # (service.paths.state_dir), never the real-mode behavior.db.
    from service.paths import state_dir
    return state_dir()


# --------------------------------------------------------------------------
# Default collaborators (overridable for tests)
# --------------------------------------------------------------------------

def _default_local_factory(path: Path):
    from service.behavior.engine import BehaviorEngine
    phraser = None
    if os.environ.get("WISP_BEHAVIOR_PHRASER", "1") != "0":
        from service.assistant.behavior_phraser import WispPhraser
        phraser = WispPhraser()
    from service.demo.aria import env_requested as aria_demo
    if aria_demo():
        # The persona plants its next gym-morning conflict and Tue/Thu
        # back-to-back run within the coming week; look that far so the demo
        # shows them on any day.
        from service.demo.aria.proactive import DEMO_NUDGE_HORIZON_S
        return BehaviorEngine(path, phraser=phraser, min_horizon_s=DEMO_NUDGE_HORIZON_S)
    return BehaviorEngine(path, phraser=phraser)


def _brain_token(url: str) -> Callable[[], str]:
    def token() -> str:
        value = os.environ.get("WISP_BRAIN_TOKEN", "").strip()
        if value:
            return value
        from service.config.provider_credentials import resolve_keychain
        return resolve_keychain("wispbrain", url)
    return token


def _default_remote_factory(home: Path):
    url = brain_url()
    if not url:
        return None
    from service.behavior.client import RemoteEngine
    return RemoteEngine(url, _brain_token(url), cache_path=home / CARD_CACHE)


def _default_persona(now: float):
    # Same zone the engine mines in (the Mac's), so "gym at 6:45" reads as 6:45.
    from service.behavior.persona import generate_persona
    from service.behavior.timeutil import resolve_tz, tz_name
    return generate_persona(now, tz=tz_name(resolve_tz(None)))


def _default_messages() -> dict:
    from service.tools.imessage_tools import structured_messages_snapshot
    return structured_messages_snapshot()


def _default_calendar_sync() -> dict:
    from service.assistant.scheduler import connectors_status
    return dict(connectors_status().get("calendar") or {})


def _default_session_db():
    from service.memory.store import store
    return store


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

_TOPICS = (
    ("calendar", r"\b(?:calendar|schedule|meeting|agenda|event|free time|availability)\b"),
    ("reminders", r"\b(?:remind|reminder|to-?do|tasks?)\b"),
    ("email", r"\b(?:e-?mails?|inbox|mail)\b"),
    ("messages", r"\b(?:texts?|messages?|imessage|reply to)\b"),
    ("weather", r"\b(?:weather|forecast|rain|temperature)\b"),
    ("notes", r"\bnotes?\b"),
    ("news", r"\b(?:news|headlines|stocks?|market)\b"),
    ("music", r"\b(?:play|music|song|spotify|podcast)\b"),
    ("web", r"\b(?:search|look up|google|who is|what is)\b"),
)


def topic_of(text: str) -> str:
    """A coarse topic label for a Wisp ask. The ask text itself is never kept."""
    low = (text or "").casefold()
    for name, pattern in _TOPICS:
        if re.search(pattern, low):
            return name
    return "general"


def _finite(value) -> bool:
    try:
        return value is not None and float(value) == float(value) and abs(float(value)) < 1e12
    except (TypeError, ValueError):
        return False


def _norm_title(text) -> str:
    return " ".join(str(text or "").split()).casefold()


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------

class TickBusy(RuntimeError):
    """A forced run could not start: another tick held the lock too long."""


class BehaviorRuntime:
    def __init__(self, home: Path | None = None, *, mode: str | None = None,
                 synthetic: bool | None = None, founder: bool | None = None,
                 local_factory: Callable[[Path], Any] | None = None,
                 remote_factory: Callable[[Path], Any] | None = None,
                 persona_fn: Callable[[float], Any] | None = None,
                 messages_fn: Callable[[], dict] | None = None,
                 calendar_sync_fn: Callable[[], dict] | None = None,
                 session_db_fn: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 calendar_grace_s: float | None = None) -> None:
        self.home = Path(home) if home is not None else _home()
        self.mode = mode or brain_mode()
        self.synthetic = synthetic_only() if synthetic is None else synthetic
        self.founder = founder_demo() if founder is None else founder
        self._local_factory = local_factory or _default_local_factory
        self._remote_factory = remote_factory or _default_remote_factory
        self._persona_fn = persona_fn or _default_persona
        self._messages_fn = messages_fn or _default_messages
        self._calendar_sync_fn = calendar_sync_fn or _default_calendar_sync
        self._session_db_fn = session_db_fn or _default_session_db
        self.clock = clock
        self._started = clock()
        self._calendar_grace_s = CALENDAR_GRACE_S if calendar_grace_s is None else float(calendar_grace_s)
        self._calendar_ready = False
        self._local_engine = None
        self._remote_engine = None
        self._remote_built = False
        self._remote_down_until = 0.0
        self.active = "local"
        self._failover = False
        self._stamps: dict[str, dict[str, Any]] = {}
        self._work_lock = threading.Lock()
        self._tick_lock: asyncio.Lock | None = None
        self._pending: deque = deque(maxlen=500)
        self._card: tuple[float, str] | None = None
        self._card_refreshing = False
        self.errors: dict[str, str] = {}
        self.last: dict[str, float] = {}
        self._db_lock = threading.RLock()
        self.home.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.home / RUNTIME_DB), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db_lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
            self._db.execute("""CREATE TABLE IF NOT EXISTS nudges (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered_at REAL, event_id TEXT,
                verdict TEXT)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS actions (
                nudge_id TEXT NOT NULL, idx INTEGER NOT NULL, type TEXT NOT NULL,
                commitment_id TEXT, title TEXT, when_ts REAL, created_at REAL NOT NULL,
                undone_at REAL, PRIMARY KEY (nudge_id, idx))""")

    # ---- runtime-local storage -----------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._db_lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._db_lock, self._db:
            self._db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))

    def close(self) -> None:
        for eng in (self._local_engine, self._remote_engine):
            close = getattr(eng, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- engine selection ----------------------------------------------------
    def local_engine(self):
        if self._local_engine is None:
            self._local_engine = self._local_factory(self.home / ENGINE_DB)
        return self._local_engine

    def _remote(self):
        if not self._remote_built:
            self._remote_built = True
            try:
                self._remote_engine = self._remote_factory(self.home)
            except Exception as exc:  # noqa: BLE001
                self.errors["remote"] = f"{type(exc).__name__}: {exc}"[:200]
                self._remote_engine = None
        return self._remote_engine

    def _target_key(self) -> str:
        if self.mode == "remote" and self.clock() >= self._remote_down_until and self._remote() is not None:
            return "remote"
        return "local"

    def call(self, method: str, *args, **kwargs):
        """Call an engine method on the remote brain when configured and up,
        else (or on any remote transport failure) on the local engine.
        KeyError/ValueError are semantic answers (404/400) and propagate."""
        if self._target_key() == "remote":
            remote = self._remote()
            try:
                result = getattr(remote, method)(*args, **kwargs)
                self.active = "remote"
                return result
            except (KeyError, ValueError):
                raise
            except Exception as exc:  # noqa: BLE001 — BrainUnavailable and friends
                self._remote_down_until = self.clock() + REMOTE_RETRY_S
                self.errors["remote"] = f"{type(exc).__name__}: {exc}"[:200]
                self._failover = True
                # Local must be fully re-fed: forget its ingest/mine stamps.
                self._stamps.pop("local", None)
                log.warning("behavior brain unavailable; using the local engine")
        self.active = "local"
        return getattr(self.local_engine(), method)(*args, **kwargs)

    async def acall(self, method: str, *args, **kwargs):
        return await asyncio.to_thread(self.call, method, *args, **kwargs)

    # ---- external hooks --------------------------------------------------------
    def record_decision(self, tool: str, approved: bool) -> None:
        from service.behavior.types import Observation
        if self.synthetic:
            return   # the synthetic persona never learns from the user's real approvals
        now = self.clock()
        tool = str(tool or "")[:64]
        if not tool:
            return
        self._pending.append(Observation(
            id=f"dec:{tool}:{int(now * 1000)}:{len(self._pending)}",
            kind="approval" if approved else "denial", ts=now, subject=tool, source="wisp"))

    # ---- ingestion -----------------------------------------------------------
    def _ingest(self, store, now: float, stamps: dict) -> bool:
        changed = False
        steps: list[tuple[str, Callable[[], bool]]] = []
        if self.synthetic:
            # Calendar first: whether a "Wisp Demo" calendar exists decides
            # where upcoming events come from (it, not the persona).
            steps.append(("calendar", lambda: self._ingest_calendar(store, now, stamps)))
            steps.append(("persona", lambda: self._ingest_persona(store, now, stamps)))
        else:
            steps.append(("calendar", lambda: self._ingest_calendar(store, now, stamps)))
        if not self.synthetic and not self.founder:
            steps += [("reminders", lambda: self._ingest_reminders(store, now)),
                      ("messages", lambda: self._ingest_messages(now)),
                      ("asks", lambda: self._ingest_asks(now))]
        for name, step in steps:
            try:
                changed = bool(step()) or changed
                self.errors.pop(name, None)
            except Exception as exc:  # noqa: BLE001 — one source never stops the others
                self.errors[name] = f"{type(exc).__name__}: {exc}"[:200]
        return changed

    def _flush_decisions(self) -> bool:
        if not self._pending:
            return False
        if self.synthetic:
            self._pending.clear()   # real approvals never feed the synthetic persona
            return False
        batch = []
        while self._pending:
            batch.append(self._pending.popleft())
        try:
            return bool(self.call("observe", batch))
        except Exception as exc:  # noqa: BLE001
            self.errors["decisions"] = f"{type(exc).__name__}: {exc}"[:200]
            return False

    def _demo_calendar_seen(self) -> bool:
        return self.kv_get("demo_calendar_seen") == "1"

    def _calendar_ready_for(self, sync: dict, now: float) -> bool:
        """Synthetic demo only: whether the first Calendar read has settled.
        Until it has, persona upcoming events are not ingested and no nudge is
        generated: a seeded "Wisp Demo" calendar must win over the persona's
        own copy of the same events, or both would nudge. With Calendar access
        off (a denied read also settles it) or no report at all, the persona's
        upcoming events are used after CALENDAR_GRACE_S of uptime."""
        if not self.synthetic or self._calendar_ready:
            return True
        if _finite(sync.get("last_sync")) and not sync.get("syncing"):
            self._calendar_ready = True
        elif now - self._started >= self._calendar_grace_s:
            self._calendar_ready = True
        return self._calendar_ready

    def _demo_cal_seen(self, key: str) -> dict[str, list]:
        try:
            value = json.loads(self.kv_get(f"demo_cal:{key}", "{}") or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _remember_demo_cal(self, key: str, observations: list, now: float) -> None:
        """Every "Wisp Demo" calendar event fed to engine ``key`` (title, start),
        so a later persona regeneration never adds its own copy of the same
        event: the calendar's copy stays in the engine after it passes, and
        persona history would otherwise double-count that day."""
        seen = {oid: v for oid, v in self._demo_cal_seen(key).items()
                if isinstance(v, list) and len(v) == 2 and _finite(v[1]) and float(v[1]) >= now - HISTORY_S}
        for o in observations:
            seen[o.id] = [_norm_title(o.subject), float(o.ts)]
        self.kv_set(f"demo_cal:{key}", json.dumps(seen, sort_keys=True))

    def _ingest_persona(self, store, now: float, stamps: dict) -> bool:
        """The synthetic persona, regenerated once per local day (ids are stable
        within a day). Rows a previous day's persona contributed that today's no
        longer has are dropped from the local engine so history never doubles.
        With a seeded "Wisp Demo" calendar, upcoming events come from it only."""
        day = datetime.fromtimestamp(now).date().isoformat()
        key = self._target_key()
        seen = self._demo_calendar_seen()
        # Persona upcoming events only once the first Calendar read settled
        # without a demo calendar (see _calendar_ready_for).
        upcoming = not seen and self._calendar_ready
        marker = f"{day}:{int(seen)}:{int(upcoming)}"
        if stamps.get("persona_day") == marker:
            return False
        persona = self._persona_fn(now)
        obs = list(getattr(persona, "observations", ()) or ())
        if upcoming:
            obs += list(getattr(persona, "upcoming", ()) or ())
        # An event the "Wisp Demo" calendar already supplied (same title, start
        # within CAL_MATCH_S) comes from the calendar only, past or upcoming.
        starts: dict[str, list[float]] = {}
        for title, ts in self._demo_cal_seen(key).values():
            starts.setdefault(title, []).append(float(ts))
        if starts:
            obs = [o for o in obs if o.kind != "event" or not any(
                abs(float(o.ts) - ts) <= CAL_MATCH_S for ts in starts.get(_norm_title(o.subject), ()))]
        ids = sorted({o.id for o in obs})
        previous = set(json.loads(self.kv_get(f"persona_ids:{key}", "[]") or "[]"))
        stale = previous - set(ids)
        count = self.call("observe", obs)
        if stale and self.active == "local":
            store_ = getattr(self.local_engine(), "_store", None)
            if store_ is not None and hasattr(store_, "delete_observations"):
                count += store_.delete_observations(sorted(stale))
        self.kv_set(f"persona_ids:{key}", json.dumps(ids))
        stamps["persona_day"] = marker
        return bool(count)

    def _calendar_rows(self, store, start: float, end: float) -> list[dict]:
        with store._lock:
            rows = store._db.execute(
                "SELECT c.id, c.source_id, c.title, c.context, c.organizer, c.when_ts, c.all_day, "
                "e.end_ts FROM commitments c LEFT JOIN calendar_event_ends e ON e.commitment_id=c.id "
                # done/dismissed rows were marked inside Wisp but still exist in
                # Apple Calendar: only an upstream disappearance is a cancellation.
                "WHERE c.source='calendar' AND c.status IN ('active','done','dismissed') "
                "AND c.when_ts >= ? AND c.when_ts < ? "
                "ORDER BY c.when_ts", (start, end)).fetchall()
        return [dict(r) for r in rows]

    def calendar_observations(self, rows: list[dict]) -> list:
        from service.behavior.types import Observation
        out = []
        for r in rows:
            if r.get("all_day") or not _finite(r.get("when_ts")) or not r.get("title"):
                continue
            when = float(r["when_ts"])
            sid = str(r.get("source_id") or r.get("id") or "")
            end = float(r["end_ts"]) if _finite(r.get("end_ts")) and float(r["end_ts"]) >= when else None
            # CalendarReader sets organizer only when it is NOT the current user.
            organizer = str(r.get("organizer") or "").strip()
            attrs = {"calendar": r.get("context") or "", "organizer": organizer,
                     "organizer_is_user": not organizer}
            out.append(Observation(id=f"cal:{sid}:{int(when)}", kind="event", ts=when,
                                   subject=str(r["title"]), source="calendar", end_ts=end,
                                   source_id=sid, attrs=attrs))
        return out

    def _ingest_calendar(self, store, now: float, stamps: dict) -> bool:
        sync = self._calendar_sync_fn() or {}
        last_sync = sync.get("last_sync")
        if not sync.get("available") or not _finite(last_sync):
            return False   # no completed read yet: an empty set is not "everything cancelled"
        start, end = now - HISTORY_S, now + AHEAD_S
        if self.synthetic:
            # Demo: history comes from the persona; the real "Wisp Demo"
            # calendar supplies only what is still ahead, so a live delete or
            # move is diffed while seeded past events never double-count.
            start = now - 3600
        rows = self._calendar_rows(store, start, end)
        if self.synthetic:
            rows = [r for r in rows if (r.get("context") or "").strip().casefold() == DEMO_CALENDAR]
            if rows:
                self.kv_set("demo_calendar_seen", "1")
            elif not self._demo_calendar_seen():
                stamps["cal_sync"] = last_sync
                return False   # no "Wisp Demo" calendar: persona events alone
        observations = self.calendar_observations(rows)
        result = self.call("observe_snapshot", "calendar", observations,
                           window_start=start, window_end=end, taken_at=float(last_sync)) or {}
        if self.synthetic:
            self._remember_demo_cal(self.active, observations, now)
        stamps["cal_sync"] = last_sync
        return any(int(result.get(k) or 0) for k in ("upserted", "cancelled", "moved"))

    def _behavior_ids(self) -> set[str]:
        with self._db_lock:
            return {r[0] for r in self._db.execute(
                "SELECT commitment_id FROM actions WHERE commitment_id IS NOT NULL")}

    def _ingest_reminders(self, store, now: float) -> bool:
        from service.behavior.types import Observation
        mine = self._behavior_ids()
        with store._lock:
            rows = [dict(r) for r in store._db.execute(
                "SELECT id, source, source_id, title, context, when_ts, status, updated_at FROM commitments "
                "WHERE kind='reminder' AND source IN ('reminders','manual') AND when_ts >= ? AND when_ts < ?",
                (now - HISTORY_S, now + AHEAD_S)).fetchall()]
        kinds = {"active": "reminder", "done": "reminder_done", "dismissed": "reminder_dismissed"}
        obs = []
        for r in rows:
            if r["id"] in mine or (r.get("context") or "").startswith(BEHAVIOR_CONTEXT):
                continue
            kind = kinds.get(r.get("status") or "active")
            if kind is None or not _finite(r.get("when_ts")):
                continue
            ts = float(r["when_ts"])
            if kind == "reminder_done" and _finite(r.get("updated_at")):
                ts = float(r["updated_at"])
            obs.append(Observation(
                id=f"rem:{r['source']}:{r.get('source_id') or r['id']}:{int(float(r['when_ts']))}",
                kind=kind, ts=ts, subject=str(r.get("title") or ""), source="reminders",
                source_id=str(r.get("source_id") or ""),
                attrs={"list": "" if r["source"] == "manual" else (r.get("context") or ""),
                       "status": r.get("status") or "active", "due_ts": float(r["when_ts"])}))
        return bool(obs) and bool(self.call("observe", obs))

    def _ingest_messages(self, now: float) -> bool:
        from service.behavior.types import Observation
        snap = self._messages_fn() or {}
        if snap.get("state") != "ready":
            return False
        obs = []
        for r in snap.get("records") or ():
            try:
                ts = float(r["timestamp"])
                if not 0 <= now - ts <= HISTORY_S:
                    continue
                incoming = r.get("direction") == "incoming"
                who = str((r.get("sender") if incoming else (r.get("conversation") or r.get("sender"))) or "").strip()
                if not who:
                    continue
                # Metadata only: who and when. Message text never leaves the reader.
                obs.append(Observation(id="msg:" + str(r["identity"]),
                                       kind="message_in" if incoming else "message_out",
                                       ts=ts, subject=who[:80], source="messages"))
            except (KeyError, TypeError, ValueError):
                continue
        return bool(obs) and bool(self.call("observe", obs))

    def _ingest_asks(self, now: float) -> bool:
        from service.behavior.types import Observation
        sessions = self._session_db_fn()
        with sessions._lock:
            rows = sessions._db.execute(
                "SELECT session_id, idx, content, created_at FROM turns WHERE role='user' "
                "AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (now - HISTORY_S, ASK_LIMIT)).fetchall()
        obs = [Observation(id=f"ask:{r['session_id']}:{r['idx']}", kind="wisp_ask",
                           ts=float(r["created_at"]), subject=topic_of(r["content"] or ""),
                           source="wisp") for r in rows if _finite(r["created_at"])]
        return bool(obs) and bool(self.call("observe", obs))

    # ---- one pass of work (worker thread) ----------------------------------------
    def work(self, store, now: float, *, force: bool = False) -> tuple[list, str]:
        """Ingest/mine/generate as due. Returns (undelivered nudges, engine key)."""
        with self._work_lock:
            self._failover = False
            key = self._target_key()
            stamps = self._stamps.setdefault(key, {"ingest": 0.0, "mine": 0.0, "nudge": 0.0})
            changed = self._flush_decisions()
            sync = {}
            try:
                sync = self._calendar_sync_fn() or {}
            except Exception:  # noqa: BLE001
                pass
            fresh_calendar = _finite(sync.get("last_sync")) and sync.get("last_sync") != stamps.get("cal_sync")
            ready = self._calendar_ready_for(sync, now)
            if (force or now - stamps["ingest"] >= INGEST_EVERY_S or fresh_calendar
                    or stamps.get("cal_ready") != ready):
                changed = self._ingest(store, now, stamps) or changed
                stamps["cal_ready"] = ready
                stamps["ingest"] = now
                self.last["ingest"] = now
            nudges: list = []
            try:
                if force or changed or now - stamps["mine"] >= MINE_EVERY_S:
                    self.call("mine", now)
                    stamps["mine"] = now
                    self.last["mine"] = now
                    self.invalidate_card()   # patterns changed: refreshed below
                # Synthetic: no nudge before the first Calendar read settled
                # (otherwise the persona's copy and the calendar's copy of the
                # same event would each nudge once).
                if ready and (force or changed or now - stamps["nudge"] >= NUDGE_EVERY_S):
                    self.call("generate_nudges", now)
                    stamps["nudge"] = now
                    self.last["nudge"] = now
                    active = self.active
                    cursor = int(self.kv_get(f"cursor:{active}", "0") or 0)
                    nudges, _ = self.call("nudges_since", cursor, 50)
                self.errors.pop("engine", None)
            except Exception as exc:  # noqa: BLE001
                self.errors["engine"] = f"{type(exc).__name__}: {exc}"[:200]
            if self._failover:
                self._stamps.pop("local", None)
            if self._card is None or self.clock() - self._card[0] >= CARD_TTL_S:
                self.refresh_card(now)
            return nudges, self.active

    # ---- delivery (event loop) ---------------------------------------------------
    def _commitment_source(self) -> str:
        from service.assistant.store import MANUAL_SOURCE, QA_SEED_SOURCE
        # QA-seed rows are hidden unless WISP_QA_SEED=1; only use that source
        # when the rehearsal environment can see it. Otherwise the action is a
        # normal Wisp-schedule row, tracked here so clear_demo_state removes it.
        if self.synthetic and os.environ.get("WISP_QA_SEED") == "1":
            return QA_SEED_SOURCE
        return MANUAL_SOURCE

    def perform_action(self, store, nudge_id: str, idx: int, action, now: float) -> dict | None:
        """Create one reversible Wisp-schedule item. Idempotent per (nudge, action)."""
        with self._db_lock:
            row = self._db.execute("SELECT * FROM actions WHERE nudge_id=? AND idx=?",
                                   (nudge_id, idx)).fetchone()
        if row is not None:
            return dict(row) if row["undone_at"] is None else None
        if action.type not in {"add_reminder", "add_hold"}:
            return None
        params = action.params or {}
        title = " ".join(str(params.get("title") or "").split())[:200]
        when = params.get("when_ts")
        if not title or not _finite(when) or float(when) < now - 60:
            return None
        when = float(when)
        kind = "reminder"
        if action.type == "add_hold":
            kind = "event"
            if not title.casefold().startswith(HOLD_PREFIX.strip().casefold()):
                title = HOLD_PREFIX + title
        notes = " ".join(str(params.get("notes") or "").split())[:300]
        context = BEHAVIOR_CONTEXT + (f" — {notes}" if notes else "")
        end = params.get("end_ts")
        import uuid
        cid = uuid.uuid4().hex
        source = self._commitment_source()
        with store.transaction() as db:
            norm = title.casefold()
            for other in db.execute(
                    "SELECT title FROM commitments WHERE status='active' AND when_ts >= ? AND when_ts < ?",
                    (int(when // 60) * 60, int(when // 60) * 60 + 60)).fetchall():
                if " ".join((other["title"] or "").split()).casefold() == norm:
                    return None   # already on file: nothing created, nothing to undo
            db.execute(
                "INSERT INTO commitments (id,source,source_id,kind,title,context,when_ts,status,confidence,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, source, cid, kind, title, context, when, "active", 1.0, now, now))
            if kind == "event" and _finite(end) and float(end) > when:
                db.execute("INSERT INTO calendar_event_ends(commitment_id,end_ts) VALUES (?,?)",
                           (cid, float(end)))
        with self._db_lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO actions VALUES (?,?,?,?,?,?,?,NULL)",
                             (nudge_id, idx, action.type, cid, title, when, now))
        return {"nudge_id": nudge_id, "idx": idx, "type": action.type, "commitment_id": cid,
                "title": title, "when_ts": when}

    def remember_nudge(self, nudge, event_id: str | None = None) -> None:
        with self._db_lock, self._db:
            self._db.execute(
                "INSERT INTO nudges(id,payload,delivered_at,event_id) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
                "event_id=COALESCE(excluded.event_id, nudges.event_id)",
                (nudge.id, json.dumps(nudge.to_dict()), self.clock(), event_id))

    def nudge(self, nudge_id: str):
        from service.behavior.types import Nudge
        with self._db_lock:
            row = self._db.execute("SELECT payload FROM nudges WHERE id=?", (nudge_id,)).fetchone()
        if row is not None:
            return Nudge.from_dict(json.loads(row[0]))
        try:
            found, _ = self.call("nudges_since", 0, 500)
        except Exception:  # noqa: BLE001
            return None
        return next((n for n in found if n.id == nudge_id), None)

    async def deliver(self, store, hub, nudge, now: float) -> dict | None:
        if nudge.expires_at is not None and _finite(nudge.expires_at) and float(nudge.expires_at) <= now:
            return None
        performed = []
        for idx, action in enumerate(nudge.actions):
            if action.auto and action.type in {"add_reminder", "add_hold"}:
                try:
                    done = self.perform_action(store, nudge.id, idx, action, now)
                except Exception as exc:  # noqa: BLE001
                    self.errors["action"] = f"{type(exc).__name__}: {exc}"[:200]
                    done = None
                if done:
                    performed.append({"type": done["type"], "title": done["title"],
                                      "when_ts": done["when_ts"], "action_index": idx})
        event = nudge.to_event()
        # "auto" tells the app an action is already done: true only for the
        # actions actually performed (one skipped as already on file, or past
        # its time, is still offered).
        done_idx = {p["action_index"] for p in performed}
        for idx, action in enumerate(event.get("actions") or []):
            if isinstance(action, dict):
                action["auto"] = idx in done_idx
        event["auto_performed"] = performed
        event["can_undo"] = bool(performed)
        self.remember_nudge(nudge)
        row = await hub.publish(event, dedupe_key=EVENT_PREFIX + nudge.id, expires_at=nudge.expires_at)
        if isinstance(row, dict) and row.get("event_id"):
            with self._db_lock, self._db:
                self._db.execute("UPDATE nudges SET event_id=? WHERE id=?", (row["event_id"], nudge.id))
        if performed:
            try:
                await hub.publish({"type": "changed"}, durable=False)
            except Exception:  # noqa: BLE001
                pass
        return row

    def expire_events(self, store, now: float) -> None:
        """The hub replays every pending event; a nudge is useful only until it expires."""
        try:
            with store.transaction() as db:
                db.execute("UPDATE assistant_events SET state='superseded' WHERE kind=? AND state='pending' "
                           "AND dedupe_key LIKE ? AND expires_at IS NOT NULL AND expires_at <= ?",
                           (EVENT_KIND, EVENT_PREFIX + "%", now))
        except Exception:  # noqa: BLE001
            pass

    def retire_event(self, store, nudge_id: str) -> None:
        try:
            with store.transaction() as db:
                db.execute("UPDATE assistant_events SET state='ack', acknowledged_at=? WHERE kind=? "
                           "AND state='pending' AND dedupe_key=?",
                           (self.clock(), EVENT_KIND, EVENT_PREFIX + nudge_id))
        except Exception:  # noqa: BLE001
            pass

    async def tick(self, store, hub, *, now: float | None = None, force: bool = False) -> list[dict]:
        if self._tick_lock is None:
            self._tick_lock = asyncio.Lock()
        lock = self._tick_lock
        if lock.locked() and not force:
            return []
        if lock.locked():
            # /behavior/run: wait for the running tick instead of silently
            # doing nothing, so a demo drop is really ingested; bounded.
            try:
                await asyncio.wait_for(lock.acquire(), FORCE_WAIT_S)
            except asyncio.TimeoutError:
                raise TickBusy(f"another behavior run is still in progress after {int(FORCE_WAIT_S)}s") from None
        else:
            await lock.acquire()
        try:
            return await self._tick_locked(store, hub, now, force)
        finally:
            lock.release()

    async def _tick_locked(self, store, hub, now: float | None, force: bool) -> list[dict]:
        now = now if now is not None else self.clock()
        self.expire_events(store, now)
        nudges, key = await asyncio.to_thread(self.work, store, now, force=force)
        published: list[dict] = []
        for nudge in sorted(nudges, key=lambda n: n.seq):
            try:
                row = await self.deliver(store, hub, nudge, now)
            except Exception as exc:  # noqa: BLE001 — retried next tick (dedupe + idempotent actions)
                self.errors["deliver"] = f"{type(exc).__name__}: {exc}"[:200]
                break
            self.kv_set(f"cursor:{key}", str(max(int(nudge.seq), int(self.kv_get(f'cursor:{key}', '0') or 0))))
            if row is not None:
                published.append(row)
        self.last["tick"] = now
        return published

    # ---- user verdicts --------------------------------------------------------------
    def feedback(self, store, nudge_id: str, verdict: str) -> None:
        from service.behavior.types import Feedback
        self.call("feedback", Feedback(nudge_id=nudge_id, verdict=verdict, ts=self.clock()))
        with self._db_lock, self._db:
            self._db.execute("UPDATE nudges SET verdict=? WHERE id=?", (verdict, nudge_id))
        self.retire_event(store, nudge_id)

    def undo(self, store, nudge_id: str) -> list[dict]:
        now = self.clock()
        with self._db_lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT * FROM actions WHERE nudge_id=? AND undone_at IS NULL", (nudge_id,))]
        removed = []
        for r in rows:
            if r.get("commitment_id"):
                try:
                    store.delete(r["commitment_id"])
                except Exception as exc:  # noqa: BLE001
                    self.errors["undo"] = f"{type(exc).__name__}: {exc}"[:200]
                    continue
            with self._db_lock, self._db:
                self._db.execute("UPDATE actions SET undone_at=? WHERE nudge_id=? AND idx=?",
                                 (now, nudge_id, r["idx"]))
            removed.append({"type": r["type"], "title": r["title"], "when_ts": r["when_ts"]})
        try:
            self.feedback(store, nudge_id, "undo")
        except Exception as exc:  # noqa: BLE001 — the reversal itself already happened
            self.errors["feedback"] = f"{type(exc).__name__}: {exc}"[:200]
            self.retire_event(store, nudge_id)
        return removed

    def delivered(self, limit: int = 20) -> list[dict]:
        with self._db_lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT * FROM nudges ORDER BY delivered_at DESC LIMIT ?", (limit,))]
        out = []
        for r in rows:
            try:
                item = json.loads(r["payload"])
            except ValueError:
                continue
            actions = self.actions_for(r["id"])
            live = {a["idx"] for a in actions if a["undone_at"] is None}
            for idx, action in enumerate(item.get("actions") or []):
                if isinstance(action, dict):
                    action["auto"] = idx in live   # done (and not undone), as in deliver()
            item.update({"delivered_at": r["delivered_at"], "event_id": r["event_id"],
                         "verdict": r["verdict"],
                         "auto_performed": [{"type": a["type"], "title": a["title"], "when_ts": a["when_ts"],
                                             "undone": a["undone_at"] is not None} for a in actions],
                         "can_undo": any(a["undone_at"] is None for a in actions)})
            out.append(item)
        return out

    def actions_for(self, nudge_id: str) -> list[dict]:
        with self._db_lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM actions WHERE nudge_id=? ORDER BY idx", (nudge_id,))]

    # ---- profile card -------------------------------------------------------------
    def invalidate_card(self) -> None:
        """Mark the card stale but keep serving its text until the refresh lands."""
        card = self._card
        self._card = (float("-inf"), card[1]) if card else None

    def refresh_card(self, now: float | None = None) -> str:
        try:
            text = str(self.call("profile_card", now, max_bytes=CARD_MAX_BYTES) or "")
        except Exception as exc:  # noqa: BLE001
            self.errors["profile"] = f"{type(exc).__name__}: {exc}"[:200]
            text = self._card[1] if self._card else ""
        encoded = text.encode("utf-8")
        if len(encoded) > CARD_MAX_BYTES:
            lines, used = [], 0
            for line in text.splitlines():
                size = len(line.encode("utf-8")) + (1 if lines else 0)
                if used + size > CARD_MAX_BYTES:
                    break
                lines.append(line)
                used += size
            text = "\n".join(lines)
        self._card = (self.clock(), text)
        return text

    def profile_card(self, *, wait: float = 0.0) -> str:
        """Cached card (<= 60s old). Never blocks the event loop on a refresh."""
        card = self._card
        if card is not None and self.clock() - card[0] < CARD_TTL_S:
            return card[1]
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        if on_loop or wait <= 0:
            if not self._card_refreshing:
                self._card_refreshing = True

                def refresh():
                    try:
                        self.refresh_card()
                    finally:
                        self._card_refreshing = False
                threading.Thread(target=refresh, name="wisp-behavior-card", daemon=True).start()
            return card[1] if card else ""
        return self.refresh_card()

    # ---- status ----------------------------------------------------------------
    def status(self) -> dict:
        info: dict[str, Any] = {
            "enabled": True, "synthetic": self.synthetic, "founder_demo": self.founder,
            "mode": self.mode, "active_engine": self.active,
            "remote_url": brain_url() if self.mode == "remote" else "",
            "remote_retry_at": self._remote_down_until or None,
            "last": dict(self.last), "errors": dict(self.errors),
            "cursor": {k: int(self.kv_get(f"cursor:{k}", "0") or 0) for k in ("local", "remote")},
            "pending_decisions": len(self._pending),
            # Synthetic: nudges wait for the first Calendar read (or the grace period).
            "waiting_for_calendar": self.synthetic and not self._calendar_ready,
        }
        with self._db_lock:
            info["delivered_nudges"] = self._db.execute("SELECT COUNT(*) FROM nudges").fetchone()[0]
            info["auto_actions"] = self._db.execute(
                "SELECT COUNT(*) FROM actions WHERE undone_at IS NULL").fetchone()[0]
        try:
            info["engine"] = self.call("status")
        except Exception as exc:  # noqa: BLE001
            info["engine"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        return info


# --------------------------------------------------------------------------
# Module singleton + scheduler-facing API
# --------------------------------------------------------------------------

_runtime: BehaviorRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> BehaviorRuntime | None:
    """The process runtime, or None when behavior learning is off."""
    global _runtime
    if not enabled():
        return None
    with _runtime_lock:
        if _runtime is None:
            _runtime = BehaviorRuntime()
        return _runtime


def set_runtime(runtime: BehaviorRuntime | None) -> None:
    """Install (tests, rehearsal tooling) or drop the process runtime."""
    global _runtime
    with _runtime_lock:
        _runtime = runtime


async def tick(store, hub, *, now: float | None = None, force: bool = False) -> list[dict]:
    """Scheduler entry point. Rate-limited internally; never raises."""
    try:
        runtime = get_runtime()
        if runtime is None:
            return []
        return await runtime.tick(store, hub, now=now, force=force)
    except Exception as exc:  # noqa: BLE001
        log.warning("behavior tick failed: %s", type(exc).__name__)
        return []


async def run_loop(store, hub) -> None:
    """Standalone loop for builds without the assistant scheduler (remote-only)."""
    while True:
        await tick(store, hub)
        await asyncio.sleep(TICK_S)


def poke(store, hub) -> None:
    """A Calendar sync just landed: ingest now instead of on the next tick."""
    try:
        if get_runtime() is None:
            return
        asyncio.get_running_loop().create_task(tick(store, hub))
    except Exception:  # noqa: BLE001
        pass


def record_decision(tool: str, approved: bool) -> None:
    """Agent-loop hook: the user approved/denied a confirm-tier action."""
    try:
        runtime = get_runtime()
        if runtime is not None:
            runtime.record_decision(tool, bool(approved))
    except Exception:  # noqa: BLE001
        pass


def profile_card() -> str:
    """Cached profile card for the system prompt ('' when off/unknown)."""
    try:
        runtime = get_runtime()
        return runtime.profile_card() if runtime is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def clear_demo_state(store=None, home: Path | None = None) -> dict:
    """Remove everything behavior learning added: Wisp-schedule items it
    created, its hub events, ``behavior.db`` and runtime state. The user's own
    commitments are never touched (only ids this runtime recorded, plus rows
    carrying its context marker)."""
    global _runtime
    home = Path(home) if home is not None else _home()
    if store is None:
        from service.assistant.store import assistant_store as store
    ids: set[str] = set()
    with _runtime_lock:
        runtime, _runtime = _runtime, None
    if runtime is not None and runtime.home == home:
        ids |= runtime._behavior_ids()
        runtime.close()
    elif (home / RUNTIME_DB).exists():
        try:
            db = sqlite3.connect(str(home / RUNTIME_DB))
            ids |= {r[0] for r in db.execute("SELECT commitment_id FROM actions WHERE commitment_id IS NOT NULL")}
            db.close()
        except sqlite3.Error:
            pass
    with store.transaction() as db:
        ids |= {r[0] for r in db.execute("SELECT id FROM commitments WHERE context LIKE ?",
                                         (BEHAVIOR_CONTEXT + "%",))}
        for cid in ids:
            db.execute("DELETE FROM commitments WHERE id=?", (cid,))
            db.execute("DELETE FROM notify_log WHERE commitment_id=?", (cid,))
            db.execute("DELETE FROM calendar_event_ends WHERE commitment_id=?", (cid,))
        events = db.execute("DELETE FROM assistant_events WHERE dedupe_key LIKE ?",
                            (EVENT_PREFIX + "%",)).rowcount
    removed_files = []
    for name in (ENGINE_DB, ENGINE_DB + "-wal", ENGINE_DB + "-shm", RUNTIME_DB, CARD_CACHE):
        try:
            (home / name).unlink()
            removed_files.append(name)
        except OSError:
            pass
    return {"commitments": len(ids), "events": events, "files": removed_files}


def day_bounds(day: str, now: float) -> tuple[float, float]:
    """Local [start, end) for 'today' | 'tomorrow' | 'next24h'."""
    base = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if day == "today":
        return now, (base + timedelta(days=1)).timestamp()
    if day == "tomorrow":
        return (base + timedelta(days=1)).timestamp(), (base + timedelta(days=2)).timestamp()
    if day == "next24h":
        return now, now + 86400.0
    raise ValueError("day must be today, tomorrow or next24h")


def predictions_for(runtime: BehaviorRuntime, day: str, now: float | None = None) -> list:
    now = now if now is not None else runtime.clock()
    start, end = day_bounds(day, now)
    preds = runtime.call("predict", now, horizon_s=max(60.0, end - now))
    return sorted((p for p in preds if start <= float(p.ts) < end), key=lambda p: p.ts)
