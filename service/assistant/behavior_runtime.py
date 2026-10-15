"""Mac-side runtime for the behavior engine: feed it, run it, act on it.

On by default; off with Settings -> "Learn my routines" or ``WISP_BEHAVIOR=0``
(see ``service.behavior_switch``). Turning it off stops ingestion and nudges
and keeps what was learned.

Engine
    An in-process ``BehaviorEngine`` at ``<WISP_HOME>/behavior.db``.

Ingestion (every ``INGEST_EVERY_S`` and right after a Calendar sync; each
source is independent and never raises into the scheduler)
    Calendar (replace-set snapshot so deletes/moves are detected), Reminders,
    Messages metadata (direction, contact, time — never text), the user's Wisp
    asks (a coarse topic label — never text), approvals/denials.

Acting
    New nudges go through the existing hub as durable ``nudge`` events
    (``dedupe_key="behavior:<nudge id>"``, ``expires_at`` from the nudge).
    ``Action(auto=True)`` of type ``add_reminder``/``add_hold`` is performed on
    Wisp's OWN schedule (never Apple Reminders/Calendar; a hold is a
    ``Tentative: …`` event) and recorded so ``undo`` can delete it.
    ``open_prompt`` is never performed automatically: messages, emails and
    changes to real meetings are only drafted, via a prefilled prompt the user
    approves in the overlay.

Safety policy
    Every write (automatic or tapped) asks ``decide("assistant_write", ...,
    tool="add_reminder")`` first, exactly like the attention runner. Anything
    short of ALLOW (View Only, a remote-profile refusal) means nothing is
    written; an automatic action is downgraded to an offer and the nudge is
    still delivered.

Forgetting and retention
    ``forget_all`` deletes everything learned (``DELETE /behavior/data``).
    ``age_out`` applies the 180-day / 30-day retention even while learning is
    off (the scheduler runs it about every six hours, and on toggle-off).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from types import MappingProxyType

log = logging.getLogger("wisp.behavior")

ENGINE_DB = "behavior.db"
RUNTIME_DB = "behavior_runtime.db"
CARD_CACHE = "behavior_profile_card.txt"
EVENT_PREFIX = "behavior:"
EVENT_KIND = "nudge"
BEHAVIOR_CONTEXT = "Added by Wisp from your routine"
HOLD_PREFIX = "Tentative: "

TICK_S = 30.0
INGEST_EVERY_S = 300.0
MINE_EVERY_S = 1800.0
NUDGE_EVERY_S = 60.0
FORCE_WAIT_S = 30.0        # /behavior/run waits this long for a running tick
STOP_WAIT_S = 30.0         # stop() waits this long for a delivery/action already in flight
CARD_TTL_S = 60.0
CARD_MAX_BYTES = 700
HISTORY_S = 180 * 86400   # full retained history; older native pages are scanned separately
AHEAD_S = 21 * 86400
ASK_LIMIT = 500


# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------

def enabled() -> bool:
    from service.behavior_switch import enabled as flag_enabled
    return flag_enabled()


def _home() -> Path:
    from service.paths import MOE_DIR
    return MOE_DIR


# --------------------------------------------------------------------------
# Default collaborators (overridable for tests)
# --------------------------------------------------------------------------

def _default_local_factory(path: Path):
    from service.behavior.engine import BehaviorEngine
    phraser = None
    from service.behavior_switch import phraser_enabled
    if phraser_enabled():     # off by default under remote inference (titles would leave the Mac)
        from service.assistant.behavior_phraser import WispPhraser
        phraser = WispPhraser()
    return BehaviorEngine(path, phraser=phraser)


def _default_messages() -> dict:
    from service.tools.imessage_tools import structured_messages_snapshot
    return structured_messages_snapshot()


def _default_contact_name(handle: str) -> str:
    """The Contacts name for a phone/email handle, or the handle itself when unknown."""
    from service.tools.imessage_tools import resolve_contact
    return resolve_contact(handle)


def _message_ts(value) -> float | None:
    """Epoch seconds from a structured Messages record. The normalized feed sends UTC RFC 3339
    strings ("2026-10-14T03:54:22.027827Z"); plain numbers are accepted too."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.timestamp() if parsed.tzinfo is not None else None
    return float(value) if _finite(value) else None


_SHORT_CODE = re.compile(r"\+?\d{3,6}")   # SMS short codes (bank, delivery, 2FA senders), not people
_TOLL_FREE = re.compile(r"\+?1?(?:800|833|844|855|866|877|888)\d{7}")


def _is_person_handle(handle: str) -> bool:
    """A phone number or email a person texts from. Automated senders are not people to have a
    rhythm with: SMS short codes, toll-free numbers, alphanumeric sender ids ("AMAZON", "Chase")
    and business chats (urn:biz:...)."""
    if "@" in handle:
        return True
    digits = re.sub(r"[\s().-]", "", handle)
    if handle.lower().startswith("urn:") or not re.fullmatch(r"\+?\d+", digits):
        return False
    return not (_SHORT_CODE.fullmatch(digits) or _TOLL_FREE.fullmatch(digits))


def _counterpart(record: dict) -> str:
    """Who the user exchanged a message with: the sender of an incoming message, the other side
    of a 1:1 chat for an outgoing one. chat.db names a chat "<service>;-;<handle>" (1:1) or
    "<service>;+;<id>" (group); an outgoing group message has no single counterpart."""
    if record.get("direction") == "incoming":
        who = str(record.get("sender") or "").strip()
    else:
        chat = str(record.get("conversation") or "").strip()
        who = chat.split(";-;", 1)[1].strip() if ";-;" in chat else ""   # group (;+;) or unknown shape
    return who if who and _is_person_handle(who) else ""


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


class RuntimeStopped(RuntimeError):
    """The runtime was stopped (its learned data is being or was erased): nothing may be
    written through it any more. Callers holding a stale reference refuse cleanly."""


class BehaviorRuntime:
    def __init__(self, home: Path | None = None, *,
                 local_factory: Callable[[Path], Any] | None = None,
                 messages_fn: Callable[[], dict] | None = None,
                 contact_name_fn: Callable[[str], str] | None = None,
                 calendar_sync_fn: Callable[[], dict] | None = None,
                 session_db_fn: Callable[[], Any] | None = None,
                 behavior_sources_fn=None, behavior_interpreter=None, role_key_fn=None,
                 gmail_history_backend=None,
                 clock: Callable[[], float] = time.time) -> None:
        self.home = Path(home) if home is not None else _home()
        self._local_factory = local_factory or _default_local_factory
        self._messages_fn = messages_fn or _default_messages
        self._contact_name_fn = contact_name_fn or _default_contact_name
        self._calendar_sync_fn = calendar_sync_fn or _default_calendar_sync
        self._session_db_fn = session_db_fn or _default_session_db
        self.clock = clock
        self._sources_fn = behavior_sources_fn
        self._interpreter = behavior_interpreter
        self._role_key_fn = role_key_fn
        from service.assistant.gmail_history_runtime import HistoryHost
        self._email_history = HistoryHost(backend=gmail_history_backend)
        self._availability_cache = None
        self._seed_status = None
        self._planning_store = None
        self._availability_attempt = 0.0
        self._availability_cancel = threading.local()
        self._local_engine = None
        self._stamps: dict[str, dict[str, Any]] = {}
        self._work_lock = threading.Lock()
        self._tick_lock: asyncio.Lock | None = None
        self._pending: deque = deque(maxlen=500)
        self._card: tuple[float, str] | None = None
        self._card_refreshing = False
        self.errors: dict[str, str] = {}
        self.last: dict[str, float] = {}
        self._db_lock = threading.RLock()
        self._stopped = False
        # Guards ``_stopped`` and the count of deliveries/actions in flight. stop() sets the
        # flag and waits for the count to reach zero under this one lock, so no write that
        # began before stop() can land after it returns, and none can begin afterwards.
        self._gate = threading.Condition()
        self._inflight = 0
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

    @contextmanager
    def _in_flight(self):
        """Register one delivery/action/user write. Raises ``RuntimeStopped`` when the
        runtime is already stopped; otherwise stop() waits for it to finish."""
        with self._gate:
            if self._stopped:
                raise RuntimeStopped("behavior runtime was stopped")
            self._inflight += 1
        try:
            yield
        finally:
            with self._gate:
                self._inflight -= 1
                self._gate.notify_all()

    def stop(self) -> None:
        """Refuse all further work and wait for a pass already running on the worker
        thread and for any delivery or action already in flight to finish. When it
        returns nothing can write through this runtime. Used before the learned data
        is deleted."""
        self._email_history.invalidate("Behavior runtime stopped", close=True)
        with self._gate:
            self._stopped = True
            self._availability_cache = None
            self._seed_status = None
            self._gate.wait_for(lambda: self._inflight == 0, timeout=STOP_WAIT_S)
        with self._work_lock:
            pass

    def live(self) -> bool:
        """False once stopped or once learning is switched off: nothing may be
        published or performed after that, even by a pass already in flight."""
        return not self._stopped and enabled()

    def close(self) -> None:
        self._email_history.invalidate("Behavior runtime closed", close=True)
        close = getattr(self._local_engine, "close", None)
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
        if self._stopped:
            raise RuntimeStopped("behavior runtime was stopped")
        if self._local_engine is None:
            self._local_engine = self._local_factory(self.home / ENGINE_DB)
        return self._local_engine

    def call(self, method: str, *args, **kwargs):
        """Call an engine method. KeyError/ValueError are semantic answers
        (404/400) and propagate."""
        if method == "configure_signals":
            with self._in_flight(), self._gate:
                if not self.live():
                    raise RuntimeStopped("behavior learning stopped")
                self._email_history.invalidate("Source preferences changed")
                self._availability_cache = None
                self._seed_status = None
                self._availability_attempt = 0.0
                return self.local_engine().configure_signals(*args, **kwargs)
        if method.startswith("history_") or method in {"observe_activity", "activity_lease"}:
            with self._in_flight(), self._work_lock:
                if not self.live():
                    raise RuntimeStopped("behavior learning stopped")
                if method == "configure_signals":
                    self._availability_cache = None
                    self._availability_attempt = 0.0
                return getattr(self.local_engine(), method)(*args, **kwargs)
        return getattr(self.local_engine(), method)(*args, **kwargs)

    def _analysis_role(self):
        if self._role_key_fn is not None:
            return self._role_key_fn()
        from service.config.endpoints import role_target
        from service.behavior_switch import remote_active
        target = role_target("fast")
        return (target.identity, remote_active())

    def _bind_history(self, settings=None, role=None):
        from service.config import authority_changes
        settings = settings if settings is not None else self.local_engine().signal_settings()
        live = self.live()
        before = authority_changes.current()
        resolved = self._analysis_role()
        after = authority_changes.current()
        if (not before.ready or before is not after or (role is not None and role != resolved)):
            self._email_history.invalidate("Configuration authority is changing")
            return
        self._email_history.bind(enabled=live, settings=settings["settings"],
                                 epoch=settings["epoch"], role=resolved)

    def invalidate_history(self, reason="Application permission withdrawn"):
        self._email_history.invalidate(reason)

    def email_history_status(self):
        with self._in_flight(), self._gate:
            # Server permission projection is separate from the source's pure
            # metadata getter and the zero-I/O installed admission callback.
            self._bind_history()
            return self._email_history.status()

    def email_history_control(self, body):
        # No await before the serialized control withdraws the old source lease.
        # Opening a bridge may only install source-owned in-memory authority;
        # it cannot construct a transport or fetch.
        with self._in_flight(), self._gate:
            self._bind_history()
            return self._email_history.control(body)

    @staticmethod
    def _seed_epoch(settings, role):
        return settings["epoch"] + ":" + hashlib.sha256(repr(role).encode()).hexdigest()

    @staticmethod
    def _pattern_proof(pattern):
        try:
            return hashlib.sha256(json.dumps(pattern, sort_keys=True, allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError, UnicodeError, OverflowError):
            return None

    def _source_batch(self, settings, now):
        if self._sources_fn is not None:
            return self._sources_fn(settings)
        from service.assistant.behavior_sources import capture
        from service.assistant.behavior_seed import Snapshot
        from zoneinfo import ZoneInfo
        engine = self.local_engine()
        retained = []
        store = self._planning_store
        day = datetime.fromtimestamp(now, ZoneInfo(engine.tz)).date().isoformat()
        def permission(source):
            if source == "calendar":
                value = self._calendar_sync_fn() or {}
            elif store is not None:
                value = store.today_snapshot(day, engine.tz)["sources"].get(source, {})
            else:
                return None
            if (value.get("available") is not True or value.get("syncing") or not _finite(value.get("last_sync"))
                    or not 0 <= self.clock()-value["last_sync"] <= 1200):
                return None
            diagnostics = value.get("diagnostics") or {}
            if diagnostics.get("complete") is False or diagnostics.get("partial"):
                return None
            start = diagnostics.get("snapshot_started_at") if source == "calendar" else value.get("snapshot_started_at")
            if (not _finite(start) or float(start) <= 0 or not 0 <= self.clock()-start <= 1200
                    or (source == "calendar" and diagnostics.get("authorized") is not True)):
                return None
            # A receipt contains no calendar title/event diagnostic lists.
            return json.dumps(dict(last_sync=value["last_sync"], snapshot_started_at=start,
                                   coverage_start=diagnostics.get("coverage_start"),
                                   coverage_end=diagnostics.get("coverage_end")), sort_keys=True, allow_nan=False)
        for source, flag in (("calendar", "calendar_context"), ("reminders", "reminders_context")):
            if not settings.get(flag):
                retained.append(Snapshot(source))
                continue
            token = None
            try:
                token = permission(source)
                expected = json.loads(token)["last_sync"] if token is not None and source == "calendar" else None
                context = engine.semantic_history(source, now, expected_sync=expected, metadata=True) if token is not None else None
                records = context["records"] if context is not None else []
                digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
                def current(s=source, token=token, digest=digest):
                    if token is None or permission(s) != token:
                        return False
                    expected = json.loads(token)["last_sync"] if s == "calendar" else None
                    rows = engine.semantic_history(s, now, expected_sync=expected, metadata=True)
                    return rows is not None and hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest() == digest
                valid = token is not None and context is not None and current()
            except Exception:
                records, context, valid = [], None, False
                current = lambda: False
            retained.append(Snapshot(source, tuple(records or []), enabled=True, available=valid,
                                     current=current, publication_complete=bool(context and context["complete"]),
                                     omitted=context["omitted"] if context else 0,
                                     requested_range=(now-HISTORY_S, now+AHEAD_S if source == "calendar" else now), scope="retained_history",
                                     published_at=json.loads(token)["snapshot_started_at"] if token is not None else None))
        return capture(settings, retained=retained, now=now,
                       gmail_history_getters=self._email_history.cached_getters)

    def _refresh_availability(self, now):
        """Worker-only bounded interpretation. Never called by a planning GET."""
        self._availability_attempt = now
        cancellation = getattr(self._availability_cancel, "event", None) or threading.Event()
        engine = self.local_engine()
        if not hasattr(engine, "signal_settings"):
            return
        settings = engine.signal_settings()
        if not settings["settings"]["interpretation"] or not self.live():
            self._availability_cache = None
            return
        try:
            from service.assistant.behavior_interpreter import Interpreter
            from service.assistant.behavior_seed import Seed
            from zoneinfo import ZoneInfo
            zone = engine.tz
            role = self._analysis_role()
            self._bind_history(settings, role)
            batch = self._source_batch(settings["settings"], now)
            day = datetime.fromtimestamp(now, ZoneInfo(zone)).date()
            patterns = [p for offset in range(7) for p in engine.availability_patterns(
                (day+timedelta(days=offset)).isoformat(), zone, batch.visits, now)]
            if not batch.current():
                self._availability_cache = None
                return
            # Bind the complete input before interpretation. Stable IDs alone
            # cannot admit revised cohorts, bounds, sites or descriptors.
            proofs = {}
            for pattern in patterns:
                ident, proof = pattern["id"], self._pattern_proof(pattern)
                proofs[ident] = proof if ident not in proofs or proofs[ident] == proof else None
            def admit():
                with self._gate:
                    return (not cancellation.is_set() and self.live() and engine.signal_settings() == settings
                            and batch.current() and self._analysis_role() == role)
            interpreter = self._interpreter or Interpreter()
            result = None
            if batch.snapshots:
                seed = Seed(engine.seed_index())
                def analyze(context, *, admit):
                    nonlocal result
                    result = interpreter.interpret(context, patterns, now=now, timezone=zone,
                        default_minutes=settings["settings"]["default_block_minutes"],
                        remote_allowed=settings["settings"]["remote_analysis"], admit=admit, associations=True)
                    if result.get("state") != "tentative":
                        raise ValueError("Seed interpretation unavailable")
                    work = [{k:d.get(k) for k in ("source_id", "quote", "kind", "ownership", "due_range", "importance_hypothesis")}
                            for d in result.get("demands", [])]
                    return dict(links=result.get("link_proposals", []), demands=work, processed_ids=result.get("record_ids", []))
                progress = seed.run(batch.snapshots, epoch=self._seed_epoch(settings, role), now=now, timezone=zone,
                                    analysis_enabled=True, analyze=analyze, admit=admit, publication=lambda: self._gate)
                with self._gate:
                    if admit():
                        self._seed_status = dict(progress, epoch=settings["epoch"], role_digest=self._seed_epoch(settings, role), at=now)
            if result is None and (patterns or (batch.records and not batch.snapshots)):
                # Empty/finished seed: at most one guarded call to interpret
                # current activity patterns, never a second source-history call.
                result = interpreter.interpret([] if batch.snapshots else batch.records, patterns, now=now, timezone=zone,
                    default_minutes=settings["settings"]["default_block_minutes"],
                    remote_allowed=settings["settings"]["remote_analysis"], admit=admit)
            result = result or dict(state="tentative", patterns=[], demands=[])
            # Drop bodies before keeping a receipt. No source text persisted.
            batch.records = []
            batch.visits = []
            batch.snapshots = ()
            with self._gate:
                if (not cancellation.is_set() and self.live() and engine.signal_settings() == settings and batch.current()
                        and self._analysis_role() == role and result.get("state") == "tentative"):
                    self._availability_cache = dict(epoch=settings["epoch"], batch=batch, result=result,
                                                    at=now, role=role, timezone=zone, cancellation=cancellation,
                                                    pattern_proofs=MappingProxyType(proofs))
                else:
                    self._availability_cache = None
        except Exception:
            self._availability_cache = None
            self.errors["availability"] = "Interpretation unavailable; no availability assumed."

    def seed_progress(self):
        with self._in_flight(), self._gate:
            engine = self.local_engine()
            if not hasattr(engine, "signal_settings"):
                return dict(state="unavailable", sources={}, detail="Source interpretation is unavailable.")
            settings = engine.signal_settings()
            if not self.live() or not settings["settings"]["interpretation"]:
                return dict(state="off", detail="Source interpretation is off.", sources={})
            if (self._seed_status is None or self._seed_status["epoch"] != settings["epoch"]
                    or self._seed_status["role_digest"] != self._seed_epoch(settings, self._analysis_role())):
                return dict(state="not_started", sources={}, detail="Waiting for a permitted current publication.")
            return {k:v for k,v in self._seed_status.items() if k not in {"epoch", "role_digest"}}

    def planning_context(self, day, timezone, now=None):
        """Read current evidence; neither analysis nor receipt freshness is renewed."""
        from service.behavior.availability import apply_interpretations
        from service.assistant.behavior_sources import Batch
        from datetime import date
        from zoneinfo import ZoneInfo
        target = date.fromisoformat(day)
        if target.isoformat() != day:
            raise ValueError("A local date is required")
        ZoneInfo(timezone)
        now = self.clock() if now is None else now
        empty = dict(version=1, ready=False, timezone=timezone, generated_at=now,
                     evidence_checked_at=now, windows=[], demands=[], patterns=[], associations=[], source_coverage={},
                     detail="No current inferred availability; an empty calendar does not prove free time.")
        with self._in_flight(), self._gate:
            if not self.live():
                return empty
            engine = self.local_engine()
            settings = engine.signal_settings()
            cache = self._availability_cache
            first = datetime.fromtimestamp(now, ZoneInfo(timezone)).date()
            in_horizon = first <= target <= first+timedelta(days=6)
            if in_horizon:
                empty["patterns"] = engine.availability_patterns(day, timezone, now=now)
            def cache_available():
                return (in_horizon and cache is not None and self._availability_cache is cache
                        and "pattern_proofs" in cache and not cache["cancellation"].is_set()
                        and self.live() and engine.signal_settings() == settings
                        and settings["settings"]["interpretation"] and cache["epoch"] == settings["epoch"]
                        and cache["timezone"] == timezone and 0 <= now-cache["at"] <= 300
                        and cache["batch"].current())
            index = engine.seed_index(create=False) if settings["settings"]["interpretation"] else None
            if index is None and not cache_available():
                return empty
            role = self._analysis_role()
            self._bind_history(settings, role)
            def cache_current():
                return cache_available() and self._analysis_role() == role == cache["role"]
            cache_ok = cache_current()
            # Durable work needs fresh source proof even after pattern analysis
            # expires. Pattern-only reads capture nothing until cache admission.
            if index is None and not cache_ok:
                return empty
            batch = None
            def withdrawn():
                return dict(empty, ready=False, windows=[], demands=[], associations=[])
            try:
                batch = self._source_batch(settings["settings"], now)
                def current():
                    return (isinstance(batch, Batch) and self.live() and engine.signal_settings() == settings
                            and self._analysis_role() == role and batch.current())
                if not current():
                    return withdrawn()
                patterns = engine.availability_patterns(day, timezone, batch.visits, now) if in_horizon else []
                empty["patterns"] = patterns
                durable_demands, links = [], []
                if index is not None and batch.snapshots:
                    from service.assistant.behavior_seed import Seed
                    seed = Seed(index)
                    epoch = self._seed_epoch(settings, role)
                    durable_demands = seed.demands(batch.snapshots, epoch=epoch, now=now,
                        default_minutes=settings["settings"]["default_block_minutes"], admit=current)
                    links = seed.materialize(batch.snapshots, epoch=epoch, admit=current)
                if not current():
                    return withdrawn()
                empty.update(demands=durable_demands, associations=links, source_coverage=batch.detail,
                    ready=bool(durable_demands or links),
                    generated_at=min((v["analyzed_at"] for v in durable_demands+links if _finite(v.get("analyzed_at"))), default=now),
                    detail="Source-grounded tentative hypotheses; uncertainty and source limits apply.")
                if not cache_ok or not cache_current():
                    return empty if current() else withdrawn()
                if (len(batch.receipts) < len(cache["batch"].receipts) or batch.detail != cache["batch"].detail):
                    return empty if current() else withdrawn()
                proofs = cache["pattern_proofs"]
                matching = [p for p in patterns if isinstance(proofs.get(p["id"]), str)
                            and proofs[p["id"]] == self._pattern_proof(p)]
                result = cache["result"]
                from copy import deepcopy
                demands = durable_demands or deepcopy(result["demands"])
                for demand in demands:
                    if demand.get("due_range") and demand["due_range"][1] <= now:
                        demand["status"] = "unresolved"
                windows = apply_interpretations(matching, result["patterns"])
                if not current() or not cache_current():
                    return withdrawn()
                return dict(empty, ready=True, windows=windows, demands=demands,
                    generated_at=min(empty["generated_at"], cache["at"]),
                    detail="Tentative source-grounded hypotheses; not attendance or availability guarantees.",
                    observed_context=engine.availability_context(timezone, now=now))
            except Exception:
                return withdrawn()
            finally:
                if isinstance(batch, Batch):
                    batch.records, batch.visits, batch.snapshots = [], [], ()

    async def acall(self, method: str, *args, **kwargs):
        return await asyncio.to_thread(self.call, method, *args, **kwargs)

    # ---- external hooks --------------------------------------------------------
    def record_decision(self, tool: str, approved: bool) -> None:
        from service.behavior.types import Observation
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
        steps: list[tuple[str, Callable[[], bool]]] = [
            ("calendar", lambda: self._ingest_calendar(store, now, stamps)),
            ("reminders", lambda: self._ingest_reminders(store, now)),
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
        batch = []
        while self._pending:
            batch.append(self._pending.popleft())
        try:
            return bool(self.call("observe", batch))
        except Exception as exc:  # noqa: BLE001
            self.errors["decisions"] = f"{type(exc).__name__}: {exc}"[:200]
            return False

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
            if not _finite(r.get("when_ts")) or not r.get("title"):
                continue
            when = float(r["when_ts"])
            sid = str(r.get("source_id") or r.get("id") or "")
            end = float(r["end_ts"]) if _finite(r.get("end_ts")) and float(r["end_ts"]) >= when else None
            # CalendarReader sets organizer only when it is NOT the current user.
            organizer = str(r.get("organizer") or "").strip()
            attrs = {"calendar": r.get("context") or "", "organizer": organizer,
                     "organizer_is_user": not organizer, "all_day": bool(r.get("all_day")),
                     "evidence_basis": "scheduled", "outcome": "unknown"}
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
        rows = self._calendar_rows(store, start, end)
        observations = self.calendar_observations(rows)
        result = self.call("observe_snapshot", "calendar", observations,
                           window_start=start, window_end=end, taken_at=float(last_sync)) or {}
        diagnostics = sync.get("diagnostics") or {}
        lo, hi = diagnostics.get("coverage_start"), diagnostics.get("coverage_end")
        if _finite(lo) and _finite(hi) and float(hi) > float(lo):
            # Source-declared coverage only; never derive it from the first event.
            self.call("record_coverage", "calendar", max(start,float(lo)), min(end,float(hi)),
                      "complete", float(last_sync))
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
            # Generic updated_at and due time do not establish completion time.
            # Native completionDate arrives through the separate history reader.
            obs.append(Observation(
                id=f"rem:{r['source']}:{r.get('source_id') or r['id']}:{int(float(r['when_ts']))}",
                kind=kind, ts=ts, subject=str(r.get("title") or ""), source="reminders",
                source_id=str(r.get("source_id") or ""),
                attrs={"list": "" if r["source"] == "manual" else (r.get("context") or ""),
                       "status": r.get("status") or "active", "due_ts": float(r["when_ts"]),
                       "completion_time_known": False}))
        return bool(obs) and bool(self.call("observe", obs))

    def _ingest_messages(self, now: float) -> bool:
        from service.behavior.types import Observation
        snap = self._messages_fn() or {}
        if snap.get("state") != "ready":
            return False
        obs = []
        for r in snap.get("records") or ():
            try:
                ts = _message_ts(r.get("timestamp"))
                if ts is None or not 0 <= now - ts <= HISTORY_S:
                    continue
                incoming = r.get("direction") == "incoming"
                handle = _counterpart(r)
                if not handle:
                    continue
                # One subject per person: the Contacts name when known, so texts, replies and
                # calendar 1:1s that name them count toward the same rhythm.
                try:
                    who = (self._contact_name_fn(handle) or handle).strip()
                except Exception:  # noqa: BLE001 — a Contacts hiccup must not drop the whole pass
                    who = handle
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
            key = "local"
            if not self.live():
                return [], key
            self._planning_store = store
            stamps = self._stamps.setdefault(key, {"ingest": 0.0, "mine": 0.0, "nudge": 0.0})
            changed = self._flush_decisions()
            sync = {}
            try:
                sync = self._calendar_sync_fn() or {}
            except Exception:  # noqa: BLE001
                pass
            fresh_calendar = _finite(sync.get("last_sync")) and sync.get("last_sync") != stamps.get("cal_sync")
            if force or now - stamps["ingest"] >= INGEST_EVERY_S or fresh_calendar:
                changed = self._ingest(store, now, stamps) or changed
                stamps["ingest"] = now
                self.last["ingest"] = now
            if now - self._availability_attempt >= INGEST_EVERY_S:
                self._refresh_availability(now)
            nudges: list = []
            try:
                if force or changed or now - stamps["mine"] >= MINE_EVERY_S:
                    self.call("mine", now)
                    stamps["mine"] = now
                    self.last["mine"] = now
                    self.invalidate_card()   # patterns changed: refreshed below
                if force or changed or now - stamps["nudge"] >= NUDGE_EVERY_S:
                    self.call("generate_nudges", now)
                    stamps["nudge"] = now
                    self.last["nudge"] = now
                    cursor = int(self.kv_get(f"cursor:{key}", "0") or 0)
                    nudges, _ = self.call("nudges_since", cursor, 50)
                self.errors.pop("engine", None)
            except Exception as exc:  # noqa: BLE001
                self.errors["engine"] = f"{type(exc).__name__}: {exc}"[:200]
            if self._card is None or self.clock() - self._card[0] >= CARD_TTL_S:
                self.refresh_card(now)
            return nudges, key

    # ---- delivery (event loop) ---------------------------------------------------
    def policy_block(self, action) -> str | None:
        """Why the safety policy forbids this Wisp-schedule write, or None when it
        is ALLOW. Same door as the model's ``add_reminder`` and the attention
        runner: View Only, remote-profile checks and grants all apply. Anything
        short of ALLOW (a CONFIRM has nobody to ask here) and a policy that cannot
        be evaluated are refusals. A refusal is audited."""
        params = getattr(action, "params", None) or {}
        args = {"title": " ".join(str(params.get("title") or "").split())[:200],
                "kind": "event" if getattr(action, "type", "") == "add_hold" else "reminder"}
        try:
            from service.safety import Tier, decide
            dec = decide("assistant_write", args, tool="add_reminder")
            reason = None if dec.tier is Tier.ALLOW else (dec.reason or dec.tier.value)
        except Exception as exc:  # noqa: BLE001
            reason = f"safety policy unavailable ({type(exc).__name__})"
        if reason is not None:
            try:
                from service.safety import audit
                audit("deny", tool="add_reminder", source="behavior", reason=reason)
            except Exception:  # noqa: BLE001 — the refusal stands even if it cannot be logged
                pass
        return reason

    def perform_action(self, store, nudge_id: str, idx: int, action, now: float) -> dict | None:
        """Create one reversible Wisp-schedule item. Idempotent per (nudge, action).
        Raises ``RuntimeStopped`` (before touching anything) once the runtime is stopped."""
        with self._in_flight():
            return self._perform_action(store, nudge_id, idx, action, now)

    def _perform_action(self, store, nudge_id: str, idx: int, action, now: float) -> dict | None:
        with self._db_lock:
            row = self._db.execute("SELECT * FROM actions WHERE nudge_id=? AND idx=?",
                                   (nudge_id, idx)).fetchone()
        if row is not None:
            return dict(row) if row["undone_at"] is None else None
        if action.type not in {"add_reminder", "add_hold"}:
            return None
        if self.policy_block(action) is not None:
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
        from service.assistant.store import MANUAL_SOURCE
        source = MANUAL_SOURCE
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
        """Publish one nudge (and perform its automatic actions). A forget/stop waits for
        a delivery already in flight, and a stopped runtime delivers nothing."""
        try:
            with self._in_flight():
                return await self._deliver(store, hub, nudge, now)
        except RuntimeStopped:
            return None

    async def _deliver(self, store, hub, nudge, now: float) -> dict | None:
        if nudge.expires_at is not None and _finite(nudge.expires_at) and float(nudge.expires_at) <= now:
            return None
        if not self.live():
            return None
        performed = []
        for idx, action in enumerate(nudge.actions):
            if action.auto and action.type in {"add_reminder", "add_hold"}:
                if self.policy_block(action) is not None:
                    continue           # downgraded to an offer: the nudge still goes out, nothing is written
                if not self.live():    # switched off mid-delivery: check right before the write
                    return None
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
        if not self.live():            # last check before anything becomes visible
            return None
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
            # doing nothing, so a fresh change is really ingested; bounded.
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
        cancellation = threading.Event()
        def run_work():
            self._availability_cancel.event = cancellation
            try:
                return self.work(store, now, force=force)
            finally:
                del self._availability_cancel.event
        try:
            nudges, key = await asyncio.to_thread(run_work)
        except asyncio.CancelledError:
            cancellation.set()
            self._availability_cache = None
            raise
        published: list[dict] = []
        for nudge in sorted(nudges, key=lambda n: n.seq):
            if not self.live():
                break    # cursor not advanced: it is delivered if learning is turned back on
            try:
                row = await self.deliver(store, hub, nudge, now)
            except Exception as exc:  # noqa: BLE001 — retried next tick (dedupe + idempotent actions)
                self.errors["deliver"] = f"{type(exc).__name__}: {exc}"[:200]
                break
            if row is None and not self.live():
                break    # switched off mid-delivery: leave the cursor so this nudge is not lost
            self.kv_set(f"cursor:{key}", str(max(int(nudge.seq), int(self.kv_get(f'cursor:{key}', '0') or 0))))
            if row is not None:
                published.append(row)
        self.last["tick"] = now
        return published

    # ---- user verdicts --------------------------------------------------------------
    def feedback(self, store, nudge_id: str, verdict: str) -> None:
        from service.behavior.types import Feedback
        with self._in_flight():
            self.call("feedback", Feedback(nudge_id=nudge_id, verdict=verdict, ts=self.clock()))
            with self._db_lock, self._db:
                self._db.execute("UPDATE nudges SET verdict=? WHERE id=?", (verdict, nudge_id))
            self.retire_event(store, nudge_id)

    def undo(self, store, nudge_id: str) -> list[dict]:
        with self._in_flight():
            return self._undo(store, nudge_id)

    def _undo(self, store, nudge_id: str) -> list[dict]:
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

    @staticmethod
    def _untouched(current: dict, action: dict) -> bool:
        """True while a Wisp-created item is exactly as Wisp made it: still active,
        same source/context/title/time, and never updated since creation. Anything the
        user edited, completed, moved or dismissed is theirs now."""
        from service.assistant.store import MANUAL_SOURCE
        try:
            return (current.get("status") == "active"
                    and current.get("source") == MANUAL_SOURCE
                    and str(current.get("context") or "").startswith(BEHAVIOR_CONTEXT)
                    and current.get("title") == action.get("title")
                    and abs(float(current["when_ts"]) - float(action["when_ts"])) < 1.0
                    and abs(float(current["updated_at"]) - float(current["created_at"])) < 1e-6)
        except (KeyError, TypeError, ValueError):
            return False

    def remove_untouched_auto_items(self, store) -> dict:
        """Delete the still-active reminders/holds Wisp created on its own schedule that
        the user has not touched (same identity record ``undo`` uses). Edited, completed,
        moved or dismissed items are kept: they are the user's now."""
        with self._db_lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT * FROM actions WHERE undone_at IS NULL AND commitment_id IS NOT NULL")]
        removed: list[dict] = []
        kept = 0
        for r in rows:
            try:
                current = store.get(r["commitment_id"])
                if current is None:
                    continue          # already gone
                if not self._untouched(current, r):
                    kept += 1
                    continue
                store.delete(r["commitment_id"])
                removed.append({"type": r["type"], "title": r["title"], "when_ts": r["when_ts"]})
            except Exception as exc:  # noqa: BLE001 — one stubborn item never blocks the forget
                self.errors["forget"] = f"{type(exc).__name__}: {exc}"[:200]
                kept += 1
        return {"removed": removed, "kept": kept}

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
            from service.behavior.types import Nudge
            item = Nudge.from_dict(item).to_dict()
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
            "enabled": True,
            "last": dict(self.last), "errors": dict(self.errors),
            "cursor": {"local": int(self.kv_get("cursor:local", "0") or 0)},
            "pending_decisions": len(self._pending),
        }
        with self._db_lock:
            info["delivered_nudges"] = self._db.execute("SELECT COUNT(*) FROM nudges").fetchone()[0]
            info["auto_actions"] = self._db.execute(
                "SELECT COUNT(*) FROM actions WHERE undone_at IS NULL").fetchone()[0]
        try:
            info["engine"] = self.call("status")
            info["seed"] = self.seed_progress()
        except Exception as exc:  # noqa: BLE001
            info["engine"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        return info


# --------------------------------------------------------------------------
# Module singleton + scheduler-facing API
# --------------------------------------------------------------------------

_runtime: BehaviorRuntime | None = None
_runtime_lock = threading.Lock()
# True from the moment forget_all detaches the runtime until its files are gone. While set,
# get_runtime() returns None, so nothing (a chat turn's profile card, a Calendar poke,
# /behavior/status, a tool, an approval) can build a new runtime on the old files mid-erase.
_forgetting = False
_forget_lock = threading.Lock()      # one forget at a time


def forgetting() -> bool:
    """Whether Wisp is erasing what it learned right now."""
    with _runtime_lock:
        return _forgetting


def invalidate_email_history(reason="Application permission withdrawn"):
    """Withdraw an existing history host without creating a runtime or reading config.

    Supported configuration mutation hooks must call this before changing
    global/role authority. An absent runtime requires no source work.
    """
    with _runtime_lock:
        runtime = _runtime
    if runtime is not None:
        runtime.invalidate_history(reason)


def get_runtime() -> BehaviorRuntime | None:
    """The process runtime, or None when behavior learning is off or being erased."""
    global _runtime
    if not enabled():
        return None
    with _runtime_lock:
        if _forgetting:
            return None
        if _runtime is None:
            _runtime = BehaviorRuntime()
        return _runtime


def _drop_nudge_events(store) -> int:
    """Remove delivered behavior nudges from the hub's durable queue: their text is learned data."""
    try:
        with store.transaction() as db:
            return int(db.execute("DELETE FROM assistant_events WHERE kind=? AND dedupe_key LIKE ?",
                                  (EVENT_KIND, EVENT_PREFIX + "%")).rowcount or 0)
    except Exception:  # noqa: BLE001
        return 0


def forget_all(store, home: Path | None = None) -> dict:
    """Delete everything Wisp learned about the user's routines. Works whether
    learning is on or off. Stops the runtime (waiting for a pass in flight),
    removes the untouched Wisp-created reminders/holds, closes every connection,
    deletes ``behavior.db``, ``behavior_runtime.db`` (with ``-wal``/``-shm``) and the
    profile card cache, and drops pending nudge events. Items the user edited,
    completed or moved are kept. If learning is still on, Wisp starts over from
    the next ingest."""
    global _runtime, _forgetting
    explicit_home = Path(home) if home is not None else None
    with _forget_lock:
        with _runtime_lock:
            _forgetting = True            # get_runtime() answers None until the files are gone
            runtime, _runtime = _runtime, None
        try:
            return _forget_locked(store, runtime, explicit_home)
        finally:
            with _runtime_lock:
                _forgetting = False


def _forget_locked(store, runtime: BehaviorRuntime | None, explicit_home: Path | None) -> dict:
    home = explicit_home or (runtime.home if runtime is not None else _home())
    if runtime is None and (home / RUNTIME_DB).exists():
        try:
            runtime = BehaviorRuntime(home)   # only to read the undo identities; never opens the engine
        except Exception:  # noqa: BLE001 — an unreadable runtime db still gets deleted below
            runtime = None
    items: dict = {"removed": [], "kept": 0}
    if runtime is not None:
        runtime.stop()
        try:
            items = runtime.remove_untouched_auto_items(store)
        finally:
            runtime.close()
    deleted = 0
    failed: list[str] = []
    names = [CARD_CACHE]
    for base in (ENGINE_DB, RUNTIME_DB):
        names += [base, base + "-wal", base + "-shm"]
    try:
        names += [p.name for p in home.glob(ENGINE_DB + ".schema-*")]   # moved-aside old-schema copies
    except OSError:
        failed.append(ENGINE_DB + ".schema-*")
    for name in names:
        try:
            (home / name).unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError:
            failed.append(name)    # keep going: delete everything else, then say what is left
    events = _drop_nudge_events(store)
    return {"removed_items": items["removed"], "kept_items": items["kept"],
            "files_deleted": deleted, "files_failed": failed, "nudge_events_deleted": events}


AGE_OUT_EVERY_S = 6 * 3600.0
_last_age_out: float | None = None


def age_out(home: Path | None = None, now: float | None = None) -> dict[str, int]:
    """Apply the retention limits (observations 180 days, nudges 30 days) to what is
    already on disk, whether or not learning is on. Plain SQL on its own connections:
    it never starts the engine, the phraser or a runtime, and never migrates a database."""
    from service.behavior.store import NUDGE_RETENTION_S, OBSERVATION_RETENTION_S, OBSERVATION_EXPIRY_SQL
    home = Path(home) if home is not None else _home()
    now = time.time() if now is None else float(now)
    out = {"observations": 0, "nudges": 0, "delivered": 0}

    def run(name: str, statements: list[tuple[str, str, float]]) -> None:
        path = home / name
        if not path.exists():
            return
        conn = None
        try:
            # mode=rw: never create the file (an erase may have just removed it).
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
            with conn:
                for key, sql, cutoff in statements:
                    if "kind=" in sql and "kind" not in {r[1] for r in conn.execute("PRAGMA table_info(observations)")}:
                        continue
                    out[key] += int(conn.execute(sql, (cutoff,)).rowcount or 0)
        except sqlite3.DatabaseError:
            pass          # unreadable or a different schema: leave it alone
        finally:
            if conn is not None:
                conn.close()

    run(ENGINE_DB, [("observations", OBSERVATION_EXPIRY_SQL, now - OBSERVATION_RETENTION_S),
                    ("observations", "DELETE FROM observations WHERE kind='device_activity' AND ts < ?", now-7*86400),
                    ("observations", "DELETE FROM observations WHERE kind='activity_bin' AND ts < ?", now-56*86400),
                    ("nudges", "DELETE FROM nudges WHERE created_at < ?", now - NUDGE_RETENTION_S)])
    run(RUNTIME_DB, [("delivered", "DELETE FROM nudges WHERE delivered_at < ?", now - NUDGE_RETENTION_S)])
    path = home / ENGINE_DB
    if path.exists():
        conn = None
        try:
            conn = sqlite3.connect(path.resolve().as_uri()+"?mode=rw", uri=True, timeout=30)
            from service.behavior.source_index import prune_connection
            with conn:
                removed = prune_connection(conn, now-OBSERVATION_RETENTION_S)
            if removed:
                out["seed_metadata"] = removed
        except sqlite3.DatabaseError:
            pass
        finally:
            if conn is not None:
                conn.close()
    return out


async def _maybe_age_out(home: Path | None = None) -> None:
    """Retention, at most about every six hours; the first tick after launch runs it."""
    global _last_age_out
    if forgetting():
        return
    mark = time.monotonic()
    if _last_age_out is not None and mark - _last_age_out < AGE_OUT_EVERY_S:
        return
    _last_age_out = mark
    await asyncio.to_thread(age_out, home)


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
            await _maybe_age_out()    # retention does not depend on the switch (skipped mid-erase)
            return []
        await _maybe_age_out(runtime.home)
        return await runtime.tick(store, hub, now=now, force=force)
    except Exception as exc:  # noqa: BLE001
        log.warning("behavior tick failed: %s", type(exc).__name__)
        return []


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


def planning_context(day: str, timezone: str, now: float | None = None):
    """Public planner adapter; disabled/forgetting is unavailable, never free."""
    runtime = get_runtime()
    return None if runtime is None else runtime.planning_context(day, timezone, now)
