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
import math
import os
import re
import sqlite3
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from types import MappingProxyType
from service.behavior.store import NewerSchemaError
from service.publication_authority import Authority, PublicationBusy, PublicationUnavailable, Witness, assert_drain_allowed, assert_mutation_allowed, mutating, publication, revocation

log = logging.getLogger("wisp.behavior")

ENGINE_DB = "behavior.db"
RUNTIME_DB = "behavior_runtime.db"
CARD_CACHE = "behavior_profile_card.txt"
EVENT_PREFIX = "behavior:"
EVENT_KIND = "nudge"
BEHAVIOR_CONTEXT = "Added by Wisp from your routine"
HOLD_PREFIX = "Tentative: "

TICK_S = 30.0
# A new install offers routine actions (tap to add) instead of performing them,
# until the user has tapped Useful or Undo once, or this long after first launch.
AUTO_ACTION_TRIAL_S = 3 * 86400
_ENGAGED_KEY = "auto_actions_engaged"
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


GOOGLE_CALENDAR_PREFIX = "gcal:"


def purge_google_calendar_rows(behavior_store, prefix: str) -> int:
    """Remove observations (events and their cancel:/move: lifecycle rows) and
    snapshot entries for Google Calendar source ids starting with `prefix`.
    Removing them from the snapshot too means the next diff cannot report
    them as cancelled."""
    if not prefix.startswith(GOOGLE_CALENDAR_PREFIX):
        raise ValueError("Only Google Calendar rows can be purged this way")
    ids = ["cal:" + prefix, "cancel:cal:" + prefix, "move:cal:" + prefix]
    conn = behavior_store._conn
    with behavior_store._lock, conn:
        before = conn.total_changes
        conn.execute("DELETE FROM observations WHERE substr(source_id,1,?)=? OR substr(id,1,?)=? "
                     "OR substr(id,1,?)=? OR substr(id,1,?)=?",
                     (len(prefix), prefix, *(x for i in ids for x in (len(i), i))))
        removed = conn.total_changes - before
    snap = behavior_store.get_snapshot("calendar")
    if snap is not None:
        items = [it for it in snap["items"] if not str(it.get("source_id") or "").startswith(prefix)
                 and not str(it.get("id") or "").startswith(ids[0])]
        if len(items) != len(snap["items"]):
            behavior_store.put_snapshot("calendar", taken_at=snap["taken_at"], window_start=snap["window_start"],
                                        window_end=snap["window_end"], items=items)
    return removed


def erase_google_calendar_account(account: str, home: Path | None = None) -> int:
    """Disconnect & erase: drop everything learned from one Google account's
    calendar, whether learning is on (live runtime) or off (file on disk)."""
    prefix = f"{GOOGLE_CALENDAR_PREFIX}{account.strip().lower()}:"
    with _runtime_lock:
        runtime = _runtime
    if runtime is not None and runtime._local_engine is not None:
        return runtime.purge_google_calendar(prefix)
    path = Path(home or _home()) / ENGINE_DB
    if not path.exists():
        return 0
    from service.behavior.store import Store
    behavior_store = Store(path)
    try:
        return purge_google_calendar_rows(behavior_store, prefix)
    finally:
        behavior_store.close()


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


class DeliveryIncomplete(RuntimeError):
    """A logical delivery entered its write sequence; retry/reconciliation is required.

    The assistant and runtime databases are separate transactions. A failure
    cannot be reported as an ordinary withheld nudge or advance its cursor.
    """


class ForgetIncomplete(RuntimeError):
    """Known Store cleanup may finish while legacy ownership remains unknown."""
    def __init__(self, reason, *, removed=(), kept=0):
        super().__init__(reason)
        self.known_removed_items = tuple(dict(item) for item in removed)
        self.known_kept_items = kept
        self.legacy_cleanup = "unknown"


_delivery_issuer = object()


@dataclass(frozen=True,eq=False,repr=False)
class _PreparedDelivery:
    runtime: object
    store: object
    nudge_id: str
    payload: str
    witness: Witness
    plan: object
    ledger: tuple
    now: float
    issuer: object

    def __reduce_ex__(self, protocol):
        raise TypeError("Prepared delivery cannot restore authority")


class BehaviorRuntime:
    def __init__(self, home: Path | None = None, *,
                 local_factory: Callable[[Path], Any] | None = None,
                 messages_fn: Callable[[], dict] | None = None,
                 contact_name_fn: Callable[[str], str] | None = None,
                 calendar_sync_fn: Callable[[], dict] | None = None,
                 session_db_fn: Callable[[], Any] | None = None,
                 behavior_sources_fn=None, behavior_interpreter=None, role_key_fn=None,
                 gmail_history_backend=None, gmail_history_client_factory=None, nudge_sources_fn=None,
                 history_monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], float] = time.time) -> None:
        self.home = (Path(home) if home is not None else _home()).expanduser().resolve()
        self._local_factory = local_factory or _default_local_factory
        self._messages_fn = messages_fn or _default_messages
        self._contact_name_fn = contact_name_fn or _default_contact_name
        self._calendar_sync_fn = calendar_sync_fn or _default_calendar_sync
        self._session_db_fn = session_db_fn or _default_session_db
        self.clock = clock
        self._sources_fn = behavior_sources_fn
        self._interpreter = behavior_interpreter
        self._role_key_fn = role_key_fn
        from service.assistant.gmail_history_runtime import HistoryHost, PublicHistoryBackend
        from service.assistant.behavior_history_driver import HistoryDriver
        self._email_history = HistoryHost(backend=gmail_history_backend if gmail_history_backend is not None
            else PublicHistoryBackend(clock=clock,monotonic=history_monotonic))
        self._history_driver = HistoryDriver(self._email_history,client_factory=gmail_history_client_factory,
            monotonic=history_monotonic)
        self._history_discovery_at = None
        self._history_monotonic = history_monotonic
        self._availability_cache = None
        self._seed_status = None
        self._planning_store = None
        self._availability_attempt = 0.0
        self._availability_cancel = threading.local()
        self._local_engine = None
        self._nudge_sources_fn = nudge_sources_fn
        self._queued_delivery = {}
        self._prepared_deliveries = weakref.WeakSet()
        self._incomplete_deliveries = set()
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
        self._publication = Authority("behavior-runtime",owner=self)
        self.home.mkdir(parents=True, exist_ok=True)
        from service.assistant.store import behavior_owner_id
        self._owner_id = behavior_owner_id(self.home)
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
        self._publication.publish()

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
        with revocation():
            self._publication.withdraw()
            self._email_history.invalidate("Behavior runtime stopped", close=True)
            with self._gate:
                self._stopped = True
                self._availability_cache = None
                self._seed_status = None
        # Never retain the outer barrier while waiting for worker/inflight work.
        with self._gate:
            self._gate.wait_for(lambda: self._inflight == 0, timeout=STOP_WAIT_S)
        with self._work_lock:
            pass

    def live(self) -> bool:
        """False once stopped or once learning is switched off: nothing may be
        published or performed after that, even by a pass already in flight."""
        return not self._stopped and enabled()

    def close(self) -> None:
        with revocation():
            self._publication.withdraw()
            self._email_history.invalidate("Behavior runtime closed", close=True)
            with self._gate:
                self._stopped = True
                self._availability_cache = None
                self._seed_status = None
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
            with revocation(),self._gate:
                if self._stopped:
                    raise RuntimeStopped("behavior runtime was stopped")
                if self._local_engine is None:
                    self._local_engine = self._local_factory(self.home / ENGINE_DB)
        return self._local_engine

    def call(self, method: str, *args, **kwargs):
        """Call an engine method. KeyError/ValueError are semantic answers
        (404/400) and propagate."""
        if method == "configure_signals":
            from service.publication_authority import revocation
            with revocation(), self._in_flight(), self._gate:
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
        from service.config import authority_changes, models_config
        settings = settings if settings is not None else self.local_engine().signal_settings()
        # Settle an explicit trusted config read before taking the revision we
        # bind. This also covers a role resolver or env override that does not
        # itself read models_config; admission callbacks remain zero-I/O.
        models_config()
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
        with revocation(),self._in_flight(), self._gate:
            # Server permission projection is separate from the source's pure
            # metadata getter and the zero-I/O installed admission callback.
            self._bind_history()
            return self._email_history.status()

    def email_history_control(self, body):
        # No await before the serialized control withdraws the old source lease.
        # Opening a bridge may only install source-owned in-memory authority;
        # it cannot construct a transport or fetch.
        with revocation(),self._in_flight(), self._gate:
            self._bind_history()
            return self._email_history.control(body)

    def _history_backlog(self):
        """Cached source proofs plus metadata-only progress; no source I/O/model."""
        from service.assistant.behavior_sources import capture
        from service.assistant.behavior_seed import record_metadata
        batch = capture({"email":True},getters={"email":lambda **kwargs:None},now=self.clock(),
                        gmail_history_getters=self._email_history.cached_getters)
        records = [metadata for snapshot in batch.snapshots for raw in snapshot.records
                   if (metadata := record_metadata(snapshot.source,raw)) is not None]
        index = self.local_engine().seed_index(create=False)
        analyzed = index.analyzed_ids(records) if index is not None else set()
        counts,done = {},{}
        for record in records:
            account = record["reference"].get("account_id")
            counts[account] = counts.get(account,0)+1
            if record["id"] in analyzed:
                done[account] = done.get(account,0)+1
        return {row["account_id"]:max(0,counts.get(row["account_id"],0)-done.get(row["account_id"],0))
                for row in self._email_history.status()["accounts"]}

    def _history_worker_state(self):
        """Worker-only discovery; construction, status and planning never call it."""
        with self._work_lock:
            if not self.live():
                return None
            engine = self.local_engine()
            if not hasattr(engine,"signal_settings"):
                return None
            settings = engine.signal_settings()
            if not settings["settings"]["email"]:
                return None
            self._bind_history(settings)
            moment = self._history_monotonic()
            if (type(moment) not in (int,float) or not math.isfinite(moment)):
                return None
            if self._history_discovery_at is None or moment-self._history_discovery_at >= TICK_S:
                self._history_discovery_at = moment
                self._email_history.observe_accounts()
                self._bind_history(settings)
            return self._history_backlog() if self.live() else None

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
        def permission(source, *, prepare=False):
            if source == "calendar":
                value = self._calendar_sync_fn() or {}
            elif store is not None:
                value = store.today_snapshot(day, engine.tz)["sources"].get(source, {})
            else:
                return (None,None) if prepare else None
            if (value.get("available") is not True or value.get("syncing") or not _finite(value.get("last_sync"))
                    or not 0 <= self.clock()-value["last_sync"] <= 1200):
                return (None,None) if prepare else None
            diagnostics = value.get("diagnostics") or {}
            if diagnostics.get("complete") is False or diagnostics.get("partial"):
                return (None,None) if prepare else None
            start = diagnostics.get("snapshot_started_at") if source == "calendar" else value.get("snapshot_started_at")
            if (not _finite(start) or float(start) <= 0 or not 0 <= self.clock()-start <= 1200
                    or (source == "calendar" and diagnostics.get("authorized") is not True)):
                return (None,None) if prepare else None
            # A receipt contains no calendar title/event diagnostic lists.
            receipt = json.dumps(dict(last_sync=value["last_sync"], snapshot_started_at=start,
                                   coverage_start=diagnostics.get("coverage_start"),
                                   coverage_end=diagnostics.get("coverage_end")), sort_keys=True, allow_nan=False)
            if not prepare:
                return receipt
            if source == "calendar":
                from service.assistant.scheduler import prepare_behavior_source_publication_witness
                witness = prepare_behavior_source_publication_witness(source,value)
            else:
                witness = store.prepare_behavior_source_publication_witness(source,value)
            return receipt,witness
        for source, flag in (("calendar", "calendar_context"), ("reminders", "reminders_context")):
            if not settings.get(flag):
                retained.append(Snapshot(source))
                continue
            token = None
            try:
                token,permission_witness = permission(source,prepare=True)
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
                content_token = engine.prepare_semantic_publication_token(source,context,now,expected_sync=expected) if valid else None
                witness = Witness.combine(permission_witness,content_token)
            except Exception:
                records, context, valid = [], None, False
                current = lambda: False
                witness = None
            retained.append(Snapshot(source, tuple(records or []), enabled=True, available=valid,
                                     current=current, publication_complete=bool(context and context["complete"]),
                                     omitted=context["omitted"] if context else 0,
                                     requested_range=(now-HISTORY_S, now+AHEAD_S if source == "calendar" else now), scope="retained_history",
                                     published_at=json.loads(token)["snapshot_started_at"] if token is not None else None,
                                     publication_witness=witness))
        return capture(settings, retained=retained, now=now,
                       gmail_history_getters=self._email_history.cached_getters)

    def _prepare_publication_witness(self,settings,role,batch=None,*,cancellation=None,extra=()):
        """Full preparation outside the application lease and Runtime gate."""
        from service.config import authority_changes
        before = authority_changes.current()
        if (not before.ready or not self.live() or cancellation is not None and cancellation.is_set()
                or self._analysis_role() != role):
            return None
        engine = self.local_engine()
        if engine.signal_settings() != settings or batch is not None and not batch.current():
            return None
        settings_token = engine.prepare_settings_publication_token(settings)
        if authority_changes.current() is not before:
            return None
        return Witness.combine(self._publication.token(cancelled_by=cancellation),settings_token,
            authority_changes.publication_token(before),*(batch.prepared_witnesses() if batch is not None else ()),*extra)

    def _publish_prepared_view(self,value,settings,role,batch=None,*,extra=(),cache=None):
        witness = self._prepare_publication_witness(settings,role,batch,extra=extra)
        try:
            with publication(witness) as lease,self._gate:
                if lease.current() and (cache is None or self._availability_cache is cache):
                    return value
        except PublicationUnavailable:
            pass
        return None

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
            seed_epoch = self._seed_epoch(settings,role)
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
                return (not cancellation.is_set() and self.live() and engine.signal_settings() == settings
                        and batch.current() and self._analysis_role() == role)
            seed_index = engine.seed_index() if batch.snapshots else None
            index_tokens = ((seed_index.publication_token(),) if seed_index is not None and
                any(s.enabled and s.available and s.records for s in batch.snapshots) else ())
            baseline = None
            def prepare():
                extra = index_tokens
                if settings["settings"]["activity"]:
                    extra += (engine.prepare_availability_publication_witness(
                        patterns,day.isoformat(),zone,batch.visits,now),)
                witness = self._prepare_publication_witness(settings,role,batch,cancellation=cancellation,extra=extra)
                return Witness.combine(baseline,witness) if baseline is not None else witness
            baseline = prepare()
            if type(baseline) is not Witness or not baseline.current():
                self._availability_cache = None
                return
            interpreter = self._interpreter or Interpreter()
            result = None
            if batch.snapshots:
                seed = Seed(seed_index)
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
                progress = seed.run(batch.snapshots, epoch=seed_epoch, now=now, timezone=zone,
                                    analysis_enabled=True, analyze=analyze, admit=admit, publication=lambda: self._gate,
                                    prepare_publication=prepare)
                status_witness = self._prepare_publication_witness(settings,role,batch,cancellation=cancellation,
                    extra=(baseline,*index_tokens))
                try:
                    with publication(status_witness) as lease,self._gate:
                        self._seed_status = dict(progress, epoch=settings["epoch"], role_digest=seed_epoch, at=now,
                            receipt_checks=tuple(batch.receipts),publication_witness=status_witness)
                except PublicationUnavailable:
                    self._seed_status = None
            if result is None and (patterns or (batch.records and not batch.snapshots)):
                # Empty/finished seed: at most one guarded call to interpret
                # current activity patterns, never a second source-history call.
                result = interpreter.interpret([] if batch.snapshots else batch.records, patterns, now=now, timezone=zone,
                    default_minutes=settings["settings"]["default_block_minutes"],
                    remote_allowed=settings["settings"]["remote_analysis"], admit=admit)
            result = result or dict(state="tentative", patterns=[], demands=[])
            witness = prepare()
            # Drop bodies before keeping a receipt. No source text persisted.
            batch.publication_witnesses = batch.prepared_witnesses()
            batch.records = []
            batch.visits = []
            batch.snapshots = ()
            with publication(witness) as lease,self._gate:
                if lease.current() and result.get("state") == "tentative":
                    self._availability_cache = dict(epoch=settings["epoch"], batch=batch, result=result,
                                                    at=now, role=role, timezone=zone, cancellation=cancellation,
                                                    pattern_proofs=MappingProxyType(proofs),publication_witness=witness)
                else:
                    self._availability_cache = None
        except PublicationUnavailable:
            self._availability_cache = None
        except Exception:
            self._availability_cache = None
            self.errors["availability"] = "Interpretation unavailable; no availability assumed."

    def seed_progress(self):
        with self._in_flight():
            engine = self.local_engine()
            if not hasattr(engine, "signal_settings"):
                return dict(state="unavailable", sources={}, detail="Source interpretation is unavailable.")
            settings = engine.signal_settings()
            if not self.live() or not settings["settings"]["interpretation"]:
                return dict(state="off", detail="Source interpretation is off.", sources={})
            progress = self._seed_status
            if (progress is None or progress["epoch"] != settings["epoch"]
                    or progress["role_digest"] != self._seed_epoch(settings, self._analysis_role())):
                return dict(state="not_started", sources={}, detail="Waiting for a permitted current publication.")
            try:
                current = all(check() is True for check in progress["receipt_checks"])
            except Exception:
                current = False
            if not current:
                return dict(state="unavailable",sources={},
                    detail="Earlier processing is not current coverage; source evidence is unavailable or expired.")
            try:
                with publication(progress.get("publication_witness")) as lease,self._gate:
                    if self._seed_status is progress and lease.current():
                        return {k:v for k,v in progress.items() if k not in {
                            "epoch", "role_digest", "receipt_checks", "publication_witness"}}
            except PublicationUnavailable:
                pass
            return dict(state="unavailable",sources={},detail="Source evidence was withdrawn before progress readback.")

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
        def withdrawn():
            # Never copy the enriched working view into a refused response.
            # New containers also prevent caller edits from crossing requests.
            return dict(version=1, ready=False, timezone=timezone, generated_at=now,
                        evidence_checked_at=now, windows=[], demands=[], patterns=[], associations=[], source_coverage={},
                        detail="No current inferred availability; an empty calendar does not prove free time.")
        empty = withdrawn()
        with self._in_flight():
            if not self.live():
                return empty
            engine = self.local_engine()
            settings = engine.signal_settings()
            if not settings["settings"]["interpretation"]:
                return empty
            cache = self._availability_cache
            role = self._analysis_role()
            def raw_view():
                proof = engine.prepare_availability_publication_witness(
                    empty["patterns"],day,timezone,(),now,days=1)
                return self._publish_prepared_view(empty,settings,role,extra=(proof,)) or withdrawn()
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
                        and type(cache.get("publication_witness")) is Witness and cache["publication_witness"].current()
                        and cache["batch"].current())
            index = engine.seed_index(create=False) if settings["settings"]["interpretation"] else None
            index_tokens = (index.publication_token(),) if index is not None else ()
            if index is None and not cache_available():
                return raw_view()
            self._bind_history(settings, role)
            def cache_current():
                return cache_available() and self._analysis_role() == role == cache["role"]
            cache_ok = cache_current()
            # Durable work needs fresh source proof even after pattern analysis
            # expires. Pattern-only reads capture nothing until cache admission.
            if index is None and not cache_ok:
                return raw_view()
            batch = None
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
                    return (self._publish_prepared_view(empty,settings,role,batch,extra=index_tokens) or withdrawn()) if current() else withdrawn()
                if (len(batch.receipts) < len(cache["batch"].receipts) or batch.detail != cache["batch"].detail):
                    return (self._publish_prepared_view(empty,settings,role,batch,extra=index_tokens) or withdrawn()) if current() else withdrawn()
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
                value = dict(empty, ready=True, windows=windows, demands=demands,
                    generated_at=min(empty["generated_at"], cache["at"]),
                    detail="Tentative source-grounded hypotheses; not attendance or availability guarantees.",
                    observed_context=engine.availability_context(timezone, now=now))
                pattern_witness = engine.prepare_availability_publication_witness(patterns,day,timezone,batch.visits,now,days=1)
                return self._publish_prepared_view(value,settings,role,batch,
                    extra=(*index_tokens,pattern_witness,cache["publication_witness"]),cache=cache) or withdrawn()
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
                # Google Calendar rows (google_calendar_source) belong to a linked
                # Google account, possibly a guest's: never learned from.
                "AND (c.source_id IS NULL OR substr(c.source_id,1,?)<>?) "
                "AND c.when_ts >= ? AND c.when_ts < ? "
                "ORDER BY c.when_ts", (len(GOOGLE_CALENDAR_PREFIX), GOOGLE_CALENDAR_PREFIX,
                                       start, end)).fetchall()
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
        # Google rows learned before they were excluded: drop them (and their
        # snapshot entries) first, so leaving the feed is not read as cancellations.
        self.purge_google_calendar(GOOGLE_CALENDAR_PREFIX)
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

    def purge_google_calendar(self, prefix: str) -> int:
        """Delete learned rows whose calendar source_id starts with `prefix`."""
        engine = self.local_engine()
        store, lock = getattr(engine, "_store", None), getattr(engine, "_lock", None)
        if store is None or lock is None:
            return 0   # an injected engine without a local store (tests, remote)
        with lock:
            return purge_google_calendar_rows(store, prefix)

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
            # A reminder the stale policy hides (a week or more overdue, not kept) is not
            # learned from either: it is neither mentioned nor used.
            hidden = store.hidden_commitment_ids(now)
        rows = [r for r in rows if r["id"] not in hidden]
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
        # A corpus test chat's prompts are not the user's asks.
        not_corpus = getattr(sessions, "not_corpus_sql", None)
        not_corpus = not_corpus() if not_corpus else "1"
        with sessions._lock:
            rows = sessions._db.execute(
                "SELECT session_id, idx, content, created_at FROM turns WHERE role='user' "
                f"AND created_at >= ? AND {not_corpus} ORDER BY created_at DESC LIMIT ?",
                (now - HISTORY_S, ASK_LIMIT)).fetchall()
        obs = [Observation(id=f"ask:{r['session_id']}:{r['idx']}", kind="wisp_ask",
                           ts=float(r["created_at"]), subject=topic_of(r["content"] or ""),
                           source="wisp") for r in rows if _finite(r["created_at"])]
        return bool(obs) and bool(self.call("observe", obs))

    def _prepare_nudge_inputs(self, store, now):
        """Capture original source grants before ingestion or generation, never on GET."""
        from service.config import authority_changes, models_config
        models_config()
        snapshot = authority_changes.current()
        if not snapshot.ready or not self.live():
            return None
        engine = self.local_engine()
        settings = engine.signal_settings()
        base = Witness.combine(self._publication.token(),authority_changes.publication_token(snapshot),
                               engine.prepare_settings_publication_token(settings))
        if base is None:
            return None
        if self._nudge_sources_fn is not None:
            sources = dict(self._nudge_sources_fn(store,now))
        else:
            sources = {}
            try:
                from service.assistant.scheduler import prepare_behavior_source_publication_witness
                sources["calendar"] = prepare_behavior_source_publication_witness("calendar",self._calendar_sync_fn())
            except Exception:
                sources["calendar"] = None
            try:
                day = datetime.fromtimestamp(now).date().isoformat()
                metadata = store.today_snapshot(day,engine.tz)["sources"]["reminders"]
                sources["reminders"] = store.prepare_behavior_source_publication_witness("reminders",metadata)
            except Exception:
                sources["reminders"] = None
            try:
                from service.tools import imessage_tools
                captured = imessage_tools.behavior_messages_snapshot_with_receipt()
                sources["messages"] = imessage_tools.prepare_publication_witness(captured)
            except Exception:
                sources["messages"] = None
        sources["wisp"] = base
        if not base.current():
            return None
        return base,sources

    def _stage_queued_nudge(self, nudge, inputs, now):
        if inputs is None:
            return
        base,sources = inputs
        original = self.local_engine().queued_nudge_authority(nudge,now)
        if original is None:
            return
        payload,policy,required = original
        witness = Witness.combine(base,policy,*(sources.get(name) for name in required))
        if witness is None:
            return
        with revocation(),self._gate:
            if not self._stopped and witness.current() and nudge.id not in self._queued_delivery:
                self._queued_delivery[nudge.id] = payload,witness
                while len(self._queued_delivery)>256:
                    self._queued_delivery.pop(next(iter(self._queued_delivery)))

    def _prepare_delivery(self, store, nudge, now):
        """Full admission and canonicalization; none of it runs in the commit lease."""
        from service.behavior.types import Nudge
        from service.safety import policy,grants
        if type(nudge) is not Nudge or not _finite(now) or not self.live():
            return None
        original = self._queued_delivery.get(nudge.id)
        if original is None or not original[1].current():
            return None
        payload = json.dumps(nudge.to_dict(),sort_keys=True,allow_nan=False)
        if original[0]!=payload:
            return None
        queued = self.local_engine().queued_nudge_authority(nudge,now)
        if queued is None or queued[0]!=payload:
            return None
        policy_proof,grant_proof = policy.publication_witness(),grants.publication_witness()
        if policy_proof is None or grant_proof is None:
            return None
        with self._db_lock:
            if self._db.in_transaction:
                raise PublicationUnavailable("Delivery cannot join a runtime caller transaction")
            prior = [dict(row) for row in self._db.execute("SELECT * FROM actions WHERE nudge_id=? ORDER BY idx",(nudge.id,))]
            dismissed = self._db.execute("SELECT verdict FROM nudges WHERE id=?",(nudge.id,)).fetchone()
            if dismissed is not None and dismissed[0] in {"wrong","not_now","undo"}:
                return None
            ledger = tuple(tuple(row.values()) for row in prior)
        # Parsing the already canonical JSON detaches every mutable action map.
        canonical = Nudge.from_dict(json.loads(payload))
        actions = []
        namespace = str(self.home.resolve())+":"+canonical.id
        held = self.auto_actions_held(store, now)
        for idx,action in enumerate(canonical.actions):
            if not action.auto or action.type not in {"add_reminder","add_hold"}:
                continue
            if held and not any(row["idx"]==idx for row in prior):
                continue   # offered with "auto": false; a tap adds it
            if self.policy_block(action) is not None:
                continue
            params = action.params
            title = " ".join(str(params.get("title") or "").split())[:200]
            when = params.get("when_ts")
            if not title or not _finite(when) or when<now-60:
                continue
            kind = "event" if action.type=="add_hold" else "reminder"
            if kind=="event" and not title.casefold().startswith(HOLD_PREFIX.strip().casefold()):
                title = HOLD_PREFIX+title
            cid = "behavior_"+hashlib.sha256((namespace+":"+str(idx)).encode()).hexdigest()[:32]
            notes = " ".join(str(params.get("notes") or "").split())[:300]
            # The commitment id is its identity; it never goes into text the user reads.
            # Rows written before this keep their " [behavior_…]" suffix (see
            # AssistantStore.prepare_nudge_write, which accepts both forms).
            context = BEHAVIOR_CONTEXT+(" — "+notes if notes else "")
            end = params.get("end_ts")
            actions.append(dict(idx=idx,type=action.type,id=cid,title=title,when_ts=float(when),kind=kind,
                context=context,end_ts=float(end) if kind=="event" and _finite(end) and end>when else None))
        if not self.live() or not policy_proof.current() or not grant_proof.current() or not original[1].current():
            return None
        plan = store.prepare_nudge_write(self,payload,canonical.to_event(),actions,prior,float(now),canonical.expires_at)
        witness = Witness.combine(original[1],queued[1],policy_proof,grant_proof,plan.binding.token)
        if witness is None or not witness.current():
            return None
        prepared = _PreparedDelivery(self,store,nudge.id,payload,witness,plan,ledger,float(now),_delivery_issuer)
        self._prepared_deliveries.add(prepared)
        return prepared

    def auto_actions_held(self, store, now: float) -> bool:
        """Whether automatic routine actions are still only offered.

        A new install learns from a few days of history at most, so its first
        guesses go to the user as suggestions. Automatic adds start once the
        user has marked any nudge Useful (tapping an offered action does that)
        or used Undo, or AUTO_ACTION_TRIAL_S after first launch. An install that
        predates the first-launch marker never waits."""
        with self._db_lock:
            engaged = self._db.execute("SELECT 1 FROM kv WHERE key=?", (_ENGAGED_KEY,)).fetchone() or self._db.execute(
                "SELECT 1 FROM nudges WHERE verdict IN ('useful','undo') LIMIT 1").fetchone()
        if engaged is not None:
            return False
        try:
            first = store.first_launch_at()
        except Exception:  # noqa: BLE001 — unknown history: offer, never act
            return True
        return first is not None and now - first < AUTO_ACTION_TRIAL_S

    def _commit_delivery(self, store, hub, prepared, loop):
        """One explicit synchronous logical publication; no await or live callbacks."""
        if (type(prepared) is not _PreparedDelivery or prepared.issuer is not _delivery_issuer
                or prepared.runtime is not self or prepared.store is not store
                or prepared not in self._prepared_deliveries):
            raise PublicationUnavailable("Exact prepared runtime delivery required")
        # Once publication accepts this witness it owns the entire ordered
        # sequence. Later physical expiry is not reported as 'not delivered'.
        with publication(prepared.witness,writes=(prepared.plan.binding,)) as lease,self._gate,self._db_lock:
            if self._stopped or self._db.in_transaction:
                raise PublicationUnavailable("Runtime cannot commit this delivery")
            rows = tuple(tuple(row) for row in self._db.execute("SELECT * FROM actions WHERE nudge_id=? ORDER BY idx",(prepared.nudge_id,)))
            if rows!=prepared.ledger:
                raise PublicationUnavailable("Action ledger changed")
            try:
                result = self._write_delivery(lease,store,hub,prepared,loop)
            except Exception as exc:
                self._incomplete_deliveries.add(prepared.nudge_id)
                raise DeliveryIncomplete("Local delivery requires retry or reconciliation") from exc
            self._incomplete_deliveries.discard(prepared.nudge_id)
            return result

    def _write_delivery(self, lease, store, hub, prepared, loop):
        performed = store.commit_nudge_actions(lease,prepared.plan,self)
        # Preserve real action identities before any event can claim Undo.
        # These are two databases, not a distributed crash transaction.
        with self._db:
            for row in performed:
                self._db.execute("INSERT OR IGNORE INTO actions VALUES(?,?,?,?,?,?,?,NULL)",
                    (row["nudge_id"],row["idx"],row["type"],row["commitment_id"],row["title"],row["when_ts"],row["created_at"]))
                actual = self._db.execute("SELECT * FROM actions WHERE nudge_id=? AND idx=?",(prepared.nudge_id,row["idx"])).fetchone()
                if actual is None or actual["undone_at"] is not None or any(actual[k]!=row[k] for k in
                        ("type","commitment_id","title","when_ts","created_at")):
                    raise PublicationUnavailable("Action receipt was not recorded")
        receipt = store.commit_nudge_event(lease,prepared.plan,self)
        with self._db:
            self._db.execute("INSERT INTO nudges(id,payload,delivered_at,event_id) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET event_id=COALESCE(nudges.event_id,excluded.event_id)",
                (prepared.nudge_id,prepared.payload,prepared.now,receipt.event_id))
        return hub.publish_committed_sync(lease,receipt,self,loop)

    # ---- one pass of work (worker thread) ----------------------------------------
    def work(self, store, now: float, *, force: bool = False) -> tuple[list, str]:
        """Ingest/mine/generate as due. Returns (undelivered nudges, engine key)."""
        with self._work_lock:
            key = "local"
            if not self.live():
                return [], key
            self._planning_store = store
            try:
                self.local_engine()
            except NewerSchemaError as exc:
                # A rollback to an older Wisp: the newer file is kept and nothing runs.
                self.errors["engine"] = str(exc)[:300]
                return [], key
            stamps = self._stamps.setdefault(key, {"ingest": 0.0, "mine": 0.0, "nudge": 0.0})
            nudge_inputs = self._prepare_nudge_inputs(store,now)
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
            nudges: list = []
            try:
                if force or changed or now - stamps["mine"] >= MINE_EVERY_S:
                    self.call("mine", now)
                    stamps["mine"] = now
                    self.last["mine"] = now
                    self.invalidate_card()   # patterns changed: refreshed below
                if force or changed or now - stamps["nudge"] >= NUDGE_EVERY_S:
                    for created in self.call("generate_nudges", now):
                        self._stage_queued_nudge(created,nudge_inputs,now)
                    stamps["nudge"] = now
                    self.last["nudge"] = now
                    nudges, _ = self.call("nudges_since", self._delivery_cursor(key), 50)
                self.errors.pop("engine", None)
            except Exception as exc:  # noqa: BLE001
                self.errors["engine"] = f"{type(exc).__name__}: {exc}"[:200]
            explicit = bool(self._email_history.worker_jobs())
            fast_pending = explicit and any(self._history_backlog().values())
            if self._history_driver.analysis_due(
                    wanted=fast_pending or now-self._availability_attempt >= INGEST_EVERY_S,explicit=explicit):
                self._refresh_availability(now)
            if self._card is None or self.clock() - self._card[0] >= CARD_TTL_S:
                self.refresh_card(now)
            return nudges, key

    def _delivery_cursor(self, key: str) -> int:
        """The delivery cursor, rebased to 0 when the engine's store was replaced.

        The engine's nudge seq restarts at 1 in a fresh behavior.db (schema
        move-aside, deletion), while ``cursor:<key>`` lives in behavior_runtime.db
        and survives; left alone it would silently skip every new nudge up to
        the old cursor. The cursor is bound to the store_id it was advanced on:
        a different id means a different seq space. A cursor with no binding
        yet (written before store ids existed) is adopted unless it exceeds the
        store's max_seq, which an intact store can never do (seq never goes
        backwards). Against the same intact store nothing is rebased, so
        nothing already delivered is offered again."""
        cursor = int(self.kv_get(f"cursor:{key}", "0") or 0)
        space = self.call("nudge_seq_space")
        store_id, max_seq = str(space["store_id"]), int(space["max_seq"])
        bound = self.kv_get(f"cursor_store:{key}")
        if bound == store_id and cursor <= max_seq:
            return cursor
        rows = [(f"cursor_store:{key}", store_id)]
        if (bound is not None and bound != store_id) or cursor > max_seq:
            log.warning("behavior store replaced (cursor %d, bound %s, store %s, max_seq %d); "
                        "rebasing delivery cursor to 0", cursor, bound, store_id, max_seq)
            cursor = 0
            rows.append((f"cursor:{key}", "0"))
        else:
            # Adopting only binds: the cursor value is left as tick advanced it.
            log.info("binding behavior delivery cursor %d to store %s", cursor, store_id)
        with self._db_lock, self._db:
            self._db.executemany(
                "INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", rows)
        return cursor

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
        assert_mutation_allowed()
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
        from service.assistant.hub import Hub
        if type(hub) is not Hub:
            return None
        loop = hub.prepare_nudge_delivery(store)
        prepared = self._prepare_delivery(store,nudge,now)
        if prepared is None:
            if nudge.id in self._incomplete_deliveries:
                raise DeliveryIncomplete("Incomplete local action cannot resume without its original evidence")
            return None
        try:
            return self._commit_delivery(store,hub,prepared,loop)
        except PublicationUnavailable:
            # Only before-entry refusals reach here. A failure in the owned
            # sequence raises DeliveryIncomplete so tick retains its cursor.
            return None

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
            try:
                try:
                    backlog = await asyncio.to_thread(self._history_worker_state)
                except NewerSchemaError as exc:
                    backlog = None                # paused: work() records why and does nothing
                    self.errors["engine"] = str(exc)[:300]
                if backlog is not None and self.live():
                    await self._history_driver.advance(pending=lambda account:backlog.get(account,0))
            except Exception:
                # A source/cleanup failure withdraws email history without
                # preventing ingestion of other permitted sources this tick.
                self._email_history.invalidate("History worker is unavailable")
                self.errors["email_history"] = "History is unavailable; other permitted sources can still be used."
            nudges, key = await asyncio.to_thread(run_work)
        except asyncio.CancelledError:
            with revocation():
                cancellation.set()
                self._email_history.invalidate("Behavior tick cancelled")
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
                self.errors.pop("deliver",None)
        self.last["tick"] = now
        return published

    # ---- user verdicts --------------------------------------------------------------
    def feedback(self, store, nudge_id: str, verdict: str) -> None:
        assert_mutation_allowed()
        from service.behavior.types import Feedback
        with revocation(),self._in_flight():
            self.call("feedback", Feedback(nudge_id=nudge_id, verdict=verdict, ts=self.clock()))
            with self._db_lock, self._db:
                self._db.execute("UPDATE nudges SET verdict=? WHERE id=?", (verdict, nudge_id))
                if verdict in {"useful", "undo"}:
                    # Kept apart from the per-nudge verdict, which a later answer overwrites.
                    self._db.execute("INSERT OR IGNORE INTO kv VALUES (?,?)", (_ENGAGED_KEY, "1"))
            self.retire_event(store, nudge_id)

    def undo(self, store, nudge_id: str) -> list[dict]:
        assert_mutation_allowed()
        with revocation(),self._in_flight():
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
                except PublicationBusy:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.errors["undo"] = f"{type(exc).__name__}: {exc}"[:200]
                    continue
            with self._db_lock, self._db:
                self._db.execute("UPDATE actions SET undone_at=? WHERE nudge_id=? AND idx=?",
                                 (now, nudge_id, r["idx"]))
            removed.append({"type": r["type"], "title": r["title"], "when_ts": r["when_ts"]})
        try:
            self.feedback(store, nudge_id, "undo")
        except PublicationBusy:
            raise
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

    def remove_untouched_auto_items(self, store, *, excluded=()) -> dict:
        """Delete the still-active reminders/holds Wisp created on its own schedule that
        the user has not touched (same identity record ``undo`` uses). Edited, completed,
        moved or dismissed items are kept: they are the user's now."""
        with self._db_lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT * FROM actions WHERE undone_at IS NULL AND commitment_id IS NOT NULL")]
        removed: list[dict] = []
        kept = 0
        excluded = frozenset(excluded)
        for r in rows:
            if r["commitment_id"] in excluded:
                continue
            try:
                current = store.get(r["commitment_id"])
                if current is None:
                    continue          # already gone
                if not self._untouched(current, r):
                    kept += 1
                    continue
                store.delete(r["commitment_id"])
                removed.append({"type": r["type"], "title": r["title"], "when_ts": r["when_ts"]})
            except PublicationBusy:
                raise
            except Exception as exc:  # noqa: BLE001 — retain the ledger for a truthful retry
                self.errors["forget"] = f"{type(exc).__name__}: {exc}"[:200]
                raise RuntimeError("Behavior item cleanup incomplete; ownership evidence retained") from exc
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
_forget_lock = threading.Lock()      # serializes brief reservation transitions
_forget_done = threading.Event()
_forget_done.set()
_forget_owner_thread = None


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
    with revocation(),_runtime_lock:
        if _forgetting:
            return None
        if _runtime is None:
            _runtime = BehaviorRuntime()
        return _runtime


def _drop_nudge_events(store) -> int:
    """Remove delivered behavior nudges from the hub's durable queue: their text is learned data."""
    try:
        return store.drop_behavior_events()
    except PublicationBusy:
        raise
    except Exception as exc:  # noqa: BLE001 — zero deleted is not evidence of successful erasure
        raise RuntimeError("Behavior event cleanup incomplete") from exc


def forget_all(store, home: Path | None = None) -> dict:
    """Delete everything Wisp learned about the user's routines. Works whether
    learning is on or off. Stops the runtime (waiting for a pass in flight),
    removes the untouched Wisp-created reminders/holds, closes every connection,
    deletes ``behavior.db``, ``behavior_runtime.db`` (with ``-wal``/``-shm``) and the
    profile card cache, and drops pending nudge events. Items the user edited,
    completed or moved are kept. If learning is still on, Wisp starts over from
    the next ingest."""
    global _runtime, _forgetting, _forget_owner_thread
    assert_drain_allowed()
    if _forget_owner_thread==threading.get_ident():
        raise PublicationBusy("This thread already owns Forget; retry outside it")
    store.assert_behavior_cleanup_available()
    from service.assistant.store import behavior_owner_id
    explicit_home = Path(home).expanduser().resolve() if home is not None else None
    requested_owner = behavior_owner_id(explicit_home) if explicit_home is not None else None
    while True:
        # Never wait for a previous Forget while holding the application
        # barrier: it may need that barrier to finish an in-flight worker.
        # Every actual private-lock acquisition retains the required order.
        with revocation(),_forget_lock,_runtime_lock:
            if _forgetting:
                reserved = False
            else:
                reserved = True
                store.assert_behavior_cleanup_available()
                runtime = _runtime
                if (runtime is not None and requested_owner is not None
                        and requested_owner!=runtime._owner_id):
                    raise ValueError("Requested home does not match the active behavior home")
                if runtime is not None:
                    runtime._publication.withdraw()
                    runtime.invalidate_history("Behavior data is being forgotten")
                _forgetting = True
                _forget_owner_thread = threading.get_ident()
                _forget_done.clear()
                _runtime = None
        if reserved:
            break
        _forget_done.wait()
    try:
        return _forget_locked(store, runtime, explicit_home)
    finally:
        with revocation(),_forget_lock,_runtime_lock:
            _forgetting = False
            _forget_owner_thread = None
            _forget_done.set()


def stop_for_reset() -> None:
    """/resetwisp (service/wisp_reset.py): stop learning for the rest of this process
    without deleting anything; the reset deletes the behavior files itself. Waits for
    a pass already running, and get_runtime() builds no new runtime afterwards."""
    global _runtime, _forgetting
    with revocation(), _runtime_lock:
        runtime = _runtime
        _runtime = None
        _forgetting = True
    if runtime is not None:
        runtime.invalidate_history("Wisp is being reset")
        runtime.stop()


def _forget_locked(store, runtime: BehaviorRuntime | None, explicit_home: Path | None) -> dict:
    home = Path(explicit_home or (runtime.home if runtime is not None else _home())).expanduser().resolve()
    from service.assistant.store import behavior_owner_id
    owner_id = behavior_owner_id(home)
    legacy_error = None
    if runtime is None and (home / RUNTIME_DB).exists():
        try:
            runtime = BehaviorRuntime(home)   # only to read the undo identities; never opens the engine
        except Exception as exc:  # noqa: BLE001 — known Store cleanup still runs; legacy evidence survives
            legacy_error = exc
            runtime = None
    if runtime is not None:
        runtime.stop()
    try:
        # Durable Store ownership also covers the action-before-runtime-ledger
        # failure window, including a missing/corrupt runtime database. It is
        # cleanup authority only; no Engine/source grant is opened or rearmed.
        items = store.forget_behavior_actions(owner_id)
        if legacy_error is not None:
            raise ForgetIncomplete("Legacy behavior cleanup incomplete; runtime ownership could not be opened. "
                                   "Surviving database evidence was preserved for retry.",
                                   removed=items["removed"],kept=items["kept"]) from legacy_error
        if runtime is not None:
            try:
                legacy = runtime.remove_untouched_auto_items(store,excluded=items["covered"])
            except PublicationBusy:
                raise
            except Exception as exc:
                raise ForgetIncomplete("Legacy behavior cleanup incomplete; ownership could not be read or erased. "
                                       "Surviving database evidence was preserved for retry.",
                                       removed=items["removed"],kept=items["kept"]) from exc
            items["removed"].extend(legacy["removed"])
            items["kept"] += legacy["kept"]
    finally:
        if runtime is not None:
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


@mutating
def age_out(home: Path | None = None, now: float | None = None) -> dict[str, int]:
    """Apply the retention limits (observations 180 days, nudges 30 days) to what is
    already on disk, whether or not learning is on. Plain SQL on its own connections:
    it never starts the engine, the phraser or a runtime, and never migrates a database."""
    from service.behavior.store import NUDGE_RETENTION_S, OBSERVATION_RETENTION_S, OBSERVATION_EXPIRY_SQL
    home = Path(home) if home is not None else _home()
    now = time.time() if now is None else float(now)
    out = {"observations": 0, "nudges": 0, "delivered": 0}
    from service.behavior.engine import database_mutation as engine_database_mutation

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

    from service.behavior.source_index import database_mutation
    from service.behavior.store import written_by_newer_build
    # A file written by a newer Wisp is that build's to age out, under its own rules.
    newer = written_by_newer_build(home / ENGINE_DB)
    with engine_database_mutation(home/ENGINE_DB),database_mutation(home/ENGINE_DB):
        if not newer:
            run(ENGINE_DB, [("observations", OBSERVATION_EXPIRY_SQL, now - OBSERVATION_RETENTION_S),
                            ("observations", "DELETE FROM observations WHERE kind='device_activity' AND ts < ?", now-7*86400),
                            ("observations", "DELETE FROM observations WHERE kind='activity_bin' AND ts < ?", now-56*86400),
                            ("nudges", "DELETE FROM nudges WHERE created_at < ?", now - NUDGE_RETENTION_S)])
        run(RUNTIME_DB, [("delivered", "DELETE FROM nudges WHERE delivered_at < ?", now - NUDGE_RETENTION_S)])
        path = home / ENGINE_DB
        if path.exists() and not newer:
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
    with revocation(),_runtime_lock:
        if _runtime is not None and _runtime is not runtime:
            _runtime._publication.withdraw()
            _runtime.invalidate_history("Behavior runtime replaced")
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
