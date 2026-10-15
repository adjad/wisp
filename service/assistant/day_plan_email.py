"""Scoped, default-off daily self-email consent and durable occurrence claims.

The host adapter uses the existing native bridge after exact feature consent,
current configured-self identity, a freshly composed plan and policy checks.
Tests use isolated state and fakes for every one of those boundaries.
The external contract is claim-once, not guaranteed inbox delivery.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
import asyncio
from zoneinfo import ZoneInfo

from service.assistant.today import RevisionConflict, day_bounds, wall_time

_PROCESS = secrets.token_hex(16)
_PREVIEW_TTL = 10 * 60
_IDENTITY_TTL = 10 * 60
_LATE_WINDOW = 30 * 60
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")
_CONSENT = dict(feature="daily_self_plan", generator_version=1,
                source_scope=["calendar", "reminders", "email", "messages", "notes", "wisp"])


def address(value):
    if not isinstance(value, str) or len(value) > 254 or not _EMAIL.fullmatch(value):
        raise ValueError("Select one exact connected email address")
    return value.casefold()


@dataclass(frozen=True)
class SelfIdentity:
    addresses: tuple[str, ...] = ()
    received_at: float = 0
    complete: bool = False
    epoch: str = "synthetic"

    def verified(self, recipient, now):
        return (self.complete is True and type(self.received_at) in (int, float)
                and 0 <= now - self.received_at <= _IDENTITY_TTL
                and recipient in {address(a) for a in self.addresses})

    def signature(self):
        return hashlib.sha256(json.dumps(sorted(address(a) for a in self.addresses)).encode()).hexdigest()


def options(recipient, timezone, minute):
    recipient = address(recipient)
    day_bounds("2026-01-01", timezone)
    if type(minute) is not int or not 0 <= minute < 1440:
        raise ValueError("Choose a daily time from 00:00 through 23:59")
    return dict(recipient=recipient, timezone=timezone, minute=minute)


class EmailStore:
    def __init__(self, path: Path, *, process=_PROCESS):
        self.path, self.process = Path(path), process
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Daily email state must be a private regular file")
        else:
            os.close(fd)
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS previews (token TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS health (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL, checked_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS ignored_windows (day TEXT NOT NULL, timezone TEXT NOT NULL, window TEXT NOT NULL,
                PRIMARY KEY(day,timezone,window));
            CREATE TABLE IF NOT EXISTS occurrences (
                day TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
                scheduled_at REAL, claimed_at REAL NOT NULL, process TEXT NOT NULL,
                recipient TEXT NOT NULL, subject TEXT NOT NULL, body_hash TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '');
        """)
        self._db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (json.dumps(dict(
            enabled=False, revision=0, recipient="", timezone="UTC", minute=480, start_day="9999-12-31")),))
        # Other-process claims are outcomes we cannot know after a restart.
        # They remain terminal for automatic delivery, even if another process
        # was still running: the occurrence can never be claimed a second time.
        self._db.execute("UPDATE occurrences SET status='unknown', detail=? WHERE status='submitting' AND process!=?",
                         ("Submission was interrupted; check Mail before sending again.", process))
        self._db.commit()

    def close(self):
        self._db.close()

    def settings(self):
        with self._lock:
            return json.loads(self._db.execute("SELECT payload FROM settings WHERE id=1").fetchone()[0])

    def status(self):
        with self._lock:
            row = self._db.execute("SELECT day,status,detail,scheduled_at FROM occurrences ORDER BY claimed_at DESC LIMIT 1").fetchone()
            health = self._db.execute("SELECT state,checked_at FROM health WHERE id=1").fetchone()
            return dict(settings=self.settings(), last_result=dict(row) if row else None,
                        health=dict(health) if health else None)

    def note(self, state, now):
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO health VALUES(1,?,?)", (state, now))
        return state

    def ignored(self, day, timezone):
        day_bounds(day, timezone)
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT window FROM ignored_windows WHERE day=? AND timezone=?", (day, timezone))]

    def ignore(self, day, timezone, window, ignored=True):
        day_bounds(day, timezone)
        if not isinstance(window, str) or not 1 <= len(window) <= 200 or type(ignored) is not bool:
            raise ValueError("Invalid availability correction")
        with self._lock, self._db:
            if ignored:
                count = self._db.execute("SELECT COUNT(*) FROM ignored_windows WHERE day=? AND timezone=?", (day, timezone)).fetchone()[0]
                if count >= 100:
                    raise ValueError("Too many corrections for one day")
                self._db.execute("INSERT OR IGNORE INTO ignored_windows VALUES(?,?,?)", (day, timezone, window))
            else:
                self._db.execute("DELETE FROM ignored_windows WHERE day=? AND timezone=? AND window=?", (day, timezone, window))
            cutoff = (date.fromisoformat(day) - timedelta(days=30)).isoformat()
            self._db.execute("DELETE FROM ignored_windows WHERE day<?", (cutoff,))

    def preview(self, requested, subject, body, identity, *, now):
        candidate = options(**requested)
        if not identity.verified(candidate["recipient"], now):
            raise ValueError("Refresh your connected Mail accounts before enabling self-email")
        if not isinstance(body, str) or not 1 <= len(body) <= 40_000 or not isinstance(subject, str) or not 1 <= len(subject) <= 200 or "\n" in subject or "\r" in subject:
            raise ValueError("The complete day plan is required for preview")
        token = secrets.token_urlsafe(32)
        with self._lock, self._db:
            current = self.settings()
            payload = dict(options=candidate, revision=current["revision"], subject=subject,
                           body_hash=hashlib.sha256(body.encode()).hexdigest(),
                           identity_epoch=identity.epoch, identity_signature=identity.signature(),
                           consent={**_CONSENT, "source_scope": list(_CONSENT["source_scope"])})
            self._db.execute("DELETE FROM previews WHERE created<?", (now - _PREVIEW_TTL,))
            self._db.execute("INSERT INTO previews VALUES(?,?,?)", (token, json.dumps(payload), now))
        return dict(token=token, expires_at=now + _PREVIEW_TTL, body=body, **payload,
                    explanation="A fresh plan will be composed each day. Wisp must be running and Mail connected. Pausing stops future submissions; a submission already in progress may finish.")

    def enable(self, token, identity, *, confirmed, now, policy_allows=None):
        if confirmed is not True:
            raise ValueError("Review the preview and explicitly enable daily self-email")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT payload,created FROM previews WHERE token=?", (token,)).fetchone()
                if row is None or not 0 <= now - row["created"] <= _PREVIEW_TTL:
                    raise ValueError("Preview expired; review a new preview")
                preview, current = json.loads(row["payload"]), self.settings()
                if preview["revision"] != current["revision"]:
                    raise RevisionConflict("Email settings changed; review a new preview")
                if preview.get("consent") != _CONSENT:
                    raise ValueError("Day plan format changed; review a new preview")
                candidate = preview["options"]
                if not identity.verified(candidate["recipient"], now):
                    raise ValueError("The selected self address is no longer verified")
                if preview["identity_epoch"] != identity.epoch or preview["identity_signature"] != identity.signature():
                    raise ValueError("Connected accounts changed; review a new preview")
                if policy_allows is not None and not policy_allows(candidate["recipient"]):
                    raise ValueError("Wisp's current access policy blocks automatic email; review its access settings")
                local = datetime.fromtimestamp(now, ZoneInfo(candidate["timezone"]))
                first = local.date()
                try:
                    due = wall_time(first.isoformat(), candidate["timezone"], candidate["minute"])
                except ValueError:
                    due = now
                if due <= now:
                    first += timedelta(days=1)
                updated = dict(**candidate, enabled=True, revision=current["revision"] + 1,
                               start_day=first.isoformat(), identity_epoch=identity.epoch,
                               identity_signature=identity.signature(), consent=preview["consent"])
                self._db.execute("UPDATE settings SET payload=? WHERE id=1", (json.dumps(updated),))
                self._db.execute("DELETE FROM previews")
                self._db.commit()
                return updated
            except BaseException:
                self._db.rollback()
                raise

    def pause(self, revision):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self.settings()
                if type(revision) is not int or revision != current["revision"]:
                    raise RevisionConflict("Email settings changed; refresh before pausing")
                current.update(enabled=False, revision=revision + 1)
                self._db.execute("UPDATE settings SET payload=? WHERE id=1", (json.dumps(current),))
                self._db.execute("DELETE FROM previews")
                self._db.commit()
                return current
            except BaseException:
                self._db.rollback()
                raise

    def claim(self, expected, day, scheduled_at, subject, body, *, now, status="submitting", detail=""):
        """Linearization point: Pause before claim wins, after claim may finish."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self.settings()
                if current != expected or current["enabled"] is not True:
                    self._db.rollback()
                    return False
                # Also bound rapid repeated sends when the user changes zone.
                recent = self._db.execute("SELECT 1 FROM occurrences WHERE status IN ('submitting','submitted','unknown') AND claimed_at>?", (now - 20 * 3600,)).fetchone()
                if recent:
                    self._db.rollback()
                    return False
                changed = self._db.execute("INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (day, current["revision"], status, scheduled_at, now, self.process, current["recipient"],
                     subject, hashlib.sha256(body.encode()).hexdigest(), detail)).rowcount
                self._db.execute("DELETE FROM previews WHERE created<?", (now - _PREVIEW_TTL,))
                self._db.execute("DELETE FROM occurrences WHERE claimed_at<?", (now - 90 * 86400,))
                self._db.commit()
                return bool(changed)
            except BaseException:
                self._db.rollback()
                raise

    def finish(self, day, status, detail):
        if status not in {"submitted", "not_attempted", "unknown"}:
            raise ValueError("Invalid submission result")
        with self._lock, self._db:
            self._db.execute("UPDATE occurrences SET status=?,detail=? WHERE day=? AND status='submitting' AND process=?",
                             (status, detail, day, self.process))

    def occurrence(self, day):
        with self._lock:
            row = self._db.execute("SELECT * FROM occurrences WHERE day=?", (day,)).fetchone()
            return dict(row) if row else None


async def deliver_due(store, *, identity, compose, sender, policy_allows, now=None, clock=None):
    """One sweep using host-supplied verified identity, composer and fakeable sender.

    No retry follows any claimed attempt, including an unconfirmed native
    receipt. Source holdback is checked before claim and may recover in-window.
    """
    read_clock = clock or (time.time if now is None else lambda: now)
    now = read_clock()
    config = store.settings()
    if config["enabled"] is not True:
        return store.note("off", now)
    if config.get("consent") != _CONSENT:
        return store.note("day_plan_scope_changed_review_settings", now)
    day = datetime.fromtimestamp(now, ZoneInfo(config["timezone"])).date().isoformat()
    if day < config["start_day"]:
        return store.note("not_due", now)
    existing = store.occurrence(day)
    if existing:
        return store.note(existing["status"], now)
    try:
        due = wall_time(day, config["timezone"], config["minute"])
    except ValueError:
        store.claim(config, day, None, "", "", now=now, status="missed",
                    detail="Daily time falls in a daylight-saving gap or repeated hour; skipped today.")
        return store.note("missed", now)
    if now < due:
        return store.note("not_due", now)
    if now - due > _LATE_WINDOW:
        store.claim(config, day, due, "", "", now=now, status="missed", detail="Wisp was not ready within the 30-minute delivery window.")
        return store.note("missed", now)
    current_identity = identity()
    if not current_identity.verified(config["recipient"], now):
        return store.note("waiting_for_self_identity", now)
    if config.get("identity_epoch") != current_identity.epoch or config.get("identity_signature") != current_identity.signature():
        return store.note("connected_accounts_changed_review_settings", now)
    if not policy_allows(config["recipient"]):
        return store.note("blocked_by_policy", now)
    plan = compose(day, config["timezone"], now)
    if plan.get("ready") is not True:
        return store.note("waiting_for_sources", now)
    subject, body = plan["subject"], plan["body"]
    if not isinstance(body, str) or not 1 <= len(body) <= 40_000 or not isinstance(subject, str) or not 1 <= len(subject) <= 200 or any(c in subject for c in "\r\n"):
        return store.note("invalid_plan", now)
    dispatch_now = read_clock()
    current_identity = identity()
    if (datetime.fromtimestamp(dispatch_now, ZoneInfo(config["timezone"])).date().isoformat() != day
            or not 0 <= dispatch_now - due <= _LATE_WINDOW):
        return store.note("delivery_window_changed", dispatch_now)
    if (not current_identity.verified(config["recipient"], dispatch_now)
            or config.get("identity_epoch") != current_identity.epoch
            or config.get("identity_signature") != current_identity.signature()):
        return store.note("waiting_for_self_identity", dispatch_now)
    if not policy_allows(config["recipient"]):
        return store.note("blocked_by_policy", dispatch_now)
    if not store.claim(config, day, due, subject, body, now=dispatch_now):
        return store.note("already_claimed_or_settings_changed", dispatch_now)
    try:
        result = await sender("send_email", dict(to=[config["recipient"]], cc=[], subject=subject, body=body))
    except BaseException:
        store.finish(day, "unknown", "Submission outcome was not recorded; check Mail before sending again.")
        raise
    result = result if isinstance(result, dict) else {}
    if result.get("ok") is True and result.get("status", "succeeded") == "succeeded":
        status, detail = "submitted", "Mail accepted the submission; inbox delivery is not confirmed."
    elif result.get("ok") is False and result.get("status") == "not_attempted" and result.get("dispatch_started") is False:
        status, detail = "not_attempted", "Mail submission did not start. No automatic retry today."
    else:
        status, detail = "unknown", "Submission was not confirmed; check Mail before sending again."
    store.finish(day, status, detail)
    return store.note(status, now)


def _policy_allows(recipient):
    from service.safety import Tier, decide
    try:
        decision = decide("email_send", {"to": recipient, "confirmed_self_send": True}, tool="send_email")
        return decision.tier in {Tier.ALLOW, Tier.CONFIRM}
    except Exception:
        return False


async def _approved_native_submission(operation, payload):
    """Final scoped admission immediately before the existing native bridge.

    CONFIRM is answered by the exact reviewed recurring feature consent, never
    by a general tool grant. No await/reconnect wait occurs before publication:
    the current public outbox implementation publishes synchronously when a
    subscriber is present. A lost connection is a non-attempt, not a replay.
    """
    from service.assistant.daily_workspace_api import email_store, self_identity
    from service.assistant.hub import hub
    from service.assistant.outbox import request
    store, now = email_store(), time.time()
    config, own = store.settings(), self_identity()
    day = datetime.fromtimestamp(now, ZoneInfo(config["timezone"])).date().isoformat()
    row = store.occurrence(day)
    try:
        due = wall_time(day, config["timezone"], config["minute"])
    except ValueError:
        due = now + 1
    allowed = (operation == "send_email" and set(payload) == {"to", "cc", "subject", "body"}
        and payload["to"] == [config["recipient"]] and payload["cc"] == []
        and config["enabled"] is True and config.get("consent") == _CONSENT
        and own.verified(config["recipient"], now) and config.get("identity_epoch") == own.epoch
        and config.get("identity_signature") == own.signature() and 0 <= now - due <= _LATE_WINDOW
        and row is not None and row["status"] == "submitting" and row["process"] == store.process
        and row["revision"] == config["revision"] and row["recipient"] == config["recipient"]
        and row["subject"] == payload["subject"] and row["body_hash"] == hashlib.sha256(payload["body"].encode()).hexdigest()
        and _policy_allows(config["recipient"]) and hub.has_subscribers)
    # Consent/native-identity mutations run on this same event loop. After
    # potentially slower policy reads, refresh the admission inputs once more;
    # there is no coroutine suspension from here until outbox publication.
    fresh_now, fresh_own = time.time(), self_identity()
    allowed = (allowed and store.settings() == config and fresh_own.verified(config["recipient"], fresh_now)
        and fresh_own.epoch == config.get("identity_epoch")
        and fresh_own.signature() == config.get("identity_signature")
        and 0 <= fresh_now - due <= _LATE_WINDOW)
    if not allowed:
        return dict(ok=False, status="not_attempted", dispatch_started=False)
    return await request(operation, payload)


_schedule_task = None


def schedule_tick():
    """Nonblocking single-flight scheduling hook; no work while default OFF."""
    global _schedule_task
    from service.assistant.daily_workspace_api import email_store
    if _schedule_task is not None and not _schedule_task.done():
        return
    if email_store().settings()["enabled"] is not True:
        return
    _schedule_task = asyncio.get_running_loop().create_task(_scheduled_sweep())


async def _scheduled_sweep():
    from service.assistant.daily_workspace_api import email_store, self_identity, email_contents
    from service.assistant.hub import hub
    store = email_store()
    try:
        if not hub.has_subscribers:
            store.note("waiting_for_wisp_app", time.time())
            return
        await asyncio.wait_for(deliver_due(store, identity=self_identity, compose=email_contents,
            sender=_approved_native_submission, policy_allows=_policy_allows), timeout=55)
    except Exception:
        # If a send was claimed, deliver_due has already persisted unknown.
        # Preparation errors never manufacture a successful delivery notice.
        store.note("temporarily_unavailable", time.time())
