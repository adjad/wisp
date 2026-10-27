"""Read-only Google Calendar source for Google accounts linked in Settings ▸ Google.

A judge (or the user) who signs in with "Connect Google" grants
`calendar.readonly` next to `gmail.readonly` (gmail_source.CONSENT_SCOPES).
For every connected account that granted it, this module reads the past 7
and next 7 days from the Calendar API (the primary calendar plus the other
calendars the account has selected) and writes the events into the SAME
commitments store EventKit's CalendarReader fills through
POST /assistant/sync/calendar, so Day, Brief, Ask's calendar tools and the
proactive engine see them with no change.

Rules:
- IMPORTANT: rows are `source='calendar'` with `source_id = "gcal:<email>:<event id>"`
  and `account = <email>`. Only rows with that prefix are ever written or
  deleted here; EventKit's rows are never touched.
- IMPORTANT: EventKit's sync replaces the whole 'calendar' set. The
  /assistant/sync/calendar route must pass its items through
  `merge_native_items` before `sync_source`, or every EventKit sync would drop
  the Google rows until the next refresh here.
- IMPORTANT: a Google account that is ALSO in macOS Calendar would show twice.
  EventKit does not send iCalUIDs, so a Google event whose title and start
  match an EventKit event is skipped (the EventKit copy wins: it can be moved).
- Google rows are marked `calendar_writable=False`: Wisp's Cancel/Move go
  through EventKit, which cannot reach a Google-only event.
- Event descriptions are never read or stored (untrusted text, and not needed).
- A fetch that finishes after the account was disconnected or reconnected is
  discarded (gmail_source.bound_account fencing), so an erase stays erased.
- Fetched every CALENDAR_EVERY seconds from gmail_source.run(), and right after
  a new sign-in (gmail_source._after_connect).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from service import gmail_source as G

API = "https://www.googleapis.com/calendar/v3"
CALENDAR_EVERY = 300
DAYS_AHEAD = 7
DAYS_BEHIND = 7                   # the past week too: a meeting that just happened can need a follow-up
MAX_CALENDARS = 12
MAX_EVENTS_PER_CALENDAR = 500
PREFIX = "gcal:"

_status: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}
# Tests inject a temporary AssistantStore here; production uses the shared one.
_store_override = None


def _store():
    if _store_override is not None:
        return _store_override
    from service.assistant.store import assistant_store
    return assistant_store


def _prefix(account: str) -> str:
    return f"{PREFIX}{account.strip().lower()}:"


# --- status -------------------------------------------------------------------

def account_status(account: str) -> dict:
    row = _status.get(account.lower(), {})
    return {"calendar_last_sync": row.get("last_sync"), "calendar_error": row.get("error", ""),
            "calendar_events": row.get("events", 0)}


def record_error(account: str, reason: str) -> None:
    _status.setdefault(account.lower(), {})["error"] = reason


# --- parsing ------------------------------------------------------------------

def _clean(value, cap: int = 1000) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:cap]


def _ts(part: dict) -> float | None:
    """Event start/end: `dateTime` is RFC 3339 with an offset; an all-day
    `date` is local midnight, the same instant EventKit reports."""
    try:
        if part.get("dateTime"):
            return datetime.fromisoformat(str(part["dateTime"]).replace("Z", "+00:00")).timestamp()
        if part.get("date"):
            y, m, d = (int(x) for x in str(part["date"]).split("-"))
            return datetime(y, m, d).timestamp()
    except (TypeError, ValueError):
        return None
    return None


def event_item(event: dict, account: str, calendar_name: str = "") -> dict | None:
    """One Calendar API event -> one commitments item, or None to skip it."""
    if not isinstance(event, dict) or not event.get("id"):
        return None
    if event.get("status") == "cancelled" or event.get("eventType") == "workingLocation":
        return None
    attendees = [a for a in event.get("attendees") or [] if isinstance(a, dict)]
    me = next((a for a in attendees if a.get("self")), None)
    if me and me.get("responseStatus") == "declined":
        return None
    start, end = event.get("start") or {}, event.get("end") or {}
    when_ts, end_ts = _ts(start), _ts(end)
    if when_ts is None:
        return None
    if end_ts is not None and end_ts < when_ts:
        end_ts = when_ts
    all_day = bool(start.get("date")) and not start.get("dateTime")
    organizer = event.get("organizer") if isinstance(event.get("organizer"), dict) else {}
    others = [a for a in attendees if not a.get("self") and not a.get("resource")]
    item = {
        "source_id": _prefix(account) + _clean(event["id"], 300),
        "ical_uid": _clean(event.get("iCalUID"), 300),
        "kind": "event" if all_day else "meeting",
        "title": _clean(event.get("summary")) or "(untitled)",
        "context": _clean(calendar_name, 200),
        "account": account.lower(),
        "when_ts": when_ts,
        "end_ts": end_ts,
        "all_day": all_day,
        "location": _clean(event.get("location"), 300),
        "url": _clean(event.get("htmlLink"), 500) if str(event.get("htmlLink", "")).startswith(
            "https://www.google.com/calendar/") else "",
        "invitee_count": len(others) if attendees else None,
        "organizer_is_user": (organizer.get("self") is True) if organizer else None,
        "calendar_writable": False,
        "confidence": 1.0,
    }
    if organizer and organizer.get("self") is not True:
        name = _clean(organizer.get("displayName") or organizer.get("email"), 200)
        if name:
            item["organizer"] = name
    return item


def _dupe_key(title, when_ts) -> tuple[str, int]:
    return (re.sub(r"\s+", " ", str(title or "")).strip().casefold(), int(round(float(when_ts or 0) / 60.0)))


def window(now: float | None = None) -> tuple[float, float]:
    """Local midnight DAYS_BEHIND days ago .. local midnight after DAYS_AHEAD more days."""
    local = datetime.fromtimestamp(time.time() if now is None else now)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return ((start - timedelta(days=DAYS_BEHIND)).timestamp(),
            (start + timedelta(days=DAYS_AHEAD + 1)).timestamp())


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- fetching -----------------------------------------------------------------

class CalendarScopeLost(G.GmailError):
    """Google says this token may no longer read Calendar."""


def _scope_lost(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    reasons = {str(item.get("reason", "")) for item in (error.get("errors") or []) + (error.get("details") or [])
               if isinstance(item, dict)}
    return bool(reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})


def _api_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    reasons = {str(item.get("reason", "")) for item in error.get("errors") or [] if isinstance(item, dict)}
    reasons |= {str(item.get("reason", "")) for item in error.get("details") or [] if isinstance(item, dict)}
    message = str(error.get("message", ""))
    if reasons & {"accessNotConfigured", "SERVICE_DISABLED"} or "has not been used" in message:
        return ("Turn on the Google Calendar API in your Google Cloud project "
                "(APIs & Services ▸ Library), then wait a minute")
    if reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
        return "Calendar access wasn't granted; reconnect and allow Calendar"
    return f"Google Calendar refused the request (HTTP {response.status_code})"


async def _get(http: httpx.AsyncClient, account: str, path: str, params: dict) -> dict:
    for attempt in range(3):
        if not G.still_connected():
            raise G.GmailError("Google connection changed during refresh")
        token = await G.access_token(http)
        response = await http.get(f"{API}/{path}", params=params,
                                  headers={"Authorization": f"Bearer {token}"})
        if not G.still_connected():
            raise G.GmailError("Google connection changed during refresh")
        if response.status_code == 401 and attempt == 0:
            G._access.pop(account, None)   # stale access token: refresh once
            continue
        if response.status_code in (429, 500, 503) and attempt < 2:
            await asyncio.sleep(1.0 * (attempt + 1))
            continue
        if response.status_code == 403 and _scope_lost(response):
            raise CalendarScopeLost(_api_error(response))
        if response.status_code != 200:
            raise G.GmailError(_api_error(response))
        return response.json()
    raise G.GmailError("Google Calendar sign-in could not be refreshed")


async def fetch_account(http: httpx.AsyncClient, account: str, now: float | None = None) -> list[dict]:
    """Events from today through DAYS_AHEAD days, primary + selected calendars.
    Must run inside gmail_source.bound_account(account)."""
    lo, hi = window(now)
    listing = await _get(http, account, "users/me/calendarList",
                         {"minAccessRole": "reader", "maxResults": "250"})
    calendars = [c for c in listing.get("items") or [] if isinstance(c, dict) and c.get("id")
                 and not c.get("hidden") and not c.get("deleted") and (c.get("primary") or c.get("selected"))]
    calendars.sort(key=lambda c: not c.get("primary"))
    items: list[dict] = []
    seen: set[tuple] = set()
    for calendar in calendars[:MAX_CALENDARS]:
        name = calendar.get("summaryOverride") or calendar.get("summary") or ""
        page, count = "", 0
        while count < MAX_EVENTS_PER_CALENDAR:
            params = {"timeMin": _rfc3339(lo), "timeMax": _rfc3339(hi), "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": "250", "showDeleted": "false"}
            if page:
                params["pageToken"] = page
            data = await _get(http, account, f"calendars/{_quote(calendar['id'])}/events", params)
            for event in data.get("items") or []:
                count += 1
                item = event_item(event, account, name)
                if item is None:
                    continue
                # The same meeting on two of this account's calendars is one event.
                key = (item["ical_uid"] or item["source_id"], round(item["when_ts"]))
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
            page = data.get("nextPageToken") or ""
            if not page:
                break
    return items


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(str(value), safe="")


async def refresh_account(account: str, *, transport: httpx.AsyncBaseTransport | None = None,
                          now: float | None = None) -> int:
    """Fetch one account's week and replace its Google rows. Returns the count."""
    account = account.strip().lower()
    if not G.has_calendar(account):
        _erase_quietly(account, only_if_present=True)
        return 0
    lock = _locks.setdefault(account, asyncio.Lock())
    async with lock:
        state = _status.setdefault(account, {})
        state["attempted"] = time.time()
        try:
            with G.bound_account(account):
                async with httpx.AsyncClient(timeout=10, transport=transport) as http:
                    items = await fetch_account(http, account, now)
                # No await between this check and the write: a disconnect that
                # erased this account cannot be undone by a late fetch.
                if not G.still_connected():
                    return 0
                count = store_events(account, items)
        except CalendarScopeLost as exc:
            # Calendar access was taken back: its events must not linger.
            _erase_quietly(account)
            _status.setdefault(account, {})["error"] = str(exc)
            await _publish_changed()
            return 0
        except G.GmailError as exc:
            if account in G.connected_accounts():
                state["error"] = str(exc)
            return 0
        except httpx.HTTPError:
            state["error"] = "Google Calendar could not be reached; try again when online"
            return 0
        state.update(last_sync=time.time(), error="", events=count)
    await _publish_changed()
    return count


def _erase_quietly(account: str, *, only_if_present: bool = False) -> None:
    """Background erase: a failed behavior-history purge (RuntimeError, raised
    after the calendar rows are already gone) is logged, never allowed to stop
    other accounts from refreshing. Disconnect & erase reports it instead."""
    try:
        (erase_if_present if only_if_present else erase)(account)
    except RuntimeError as exc:
        print(f"[google_calendar] background erase incomplete: {exc}", flush=True)


async def refresh_due(accounts: dict[str, dict], now: float | None = None) -> None:
    now = time.time() if now is None else now

    async def one(account: str) -> None:
        try:
            await asyncio.wait_for(refresh_account(account), 30)
        except asyncio.TimeoutError:
            record_error(account, "Google Calendar timed out; it will retry in a few minutes")

    for account, item in accounts.items():
        if not G.has_calendar(account, item):
            _erase_quietly(account, only_if_present=True)   # the scope was dropped on a reconnect
    due = [account for account, item in accounts.items() if G.has_calendar(account, item)
           and now - _status.get(account, {}).get("attempted", 0) >= CALENDAR_EVERY]
    await asyncio.gather(*(one(account) for account in due))


async def _publish_changed() -> None:
    try:
        from service.assistant.hub import hub
        await hub.publish({"type": "changed"})
    except Exception:  # noqa: BLE001 — the next poll shows it anyway
        pass


# --- the commitments store ----------------------------------------------------

_UPSERT = (
    "INSERT INTO commitments "
    "(id, source, source_id, kind, title, context, organizer, account, when_ts, "
    " all_day, due_date, location, url, status, confidence, created_at, updated_at) "
    "VALUES (?,'calendar',?,?,?,?,?,?,?,?,NULL,?,?,'active',?,?,?) "
    "ON CONFLICT(source, source_id, when_ts) DO UPDATE SET "
    "  kind=excluded.kind, title=excluded.title, context=excluded.context, "
    "  organizer=excluded.organizer, account=excluded.account, "
    "  all_day=excluded.all_day, location=excluded.location, "
    "  url=excluded.url, updated_at=excluded.updated_at, "
    "  status=CASE WHEN commitments.status='dismissed' THEN 'dismissed' ELSE 'active' END")


def _delete_rows(db, ids: list[str]) -> None:
    for rid in ids:
        db.execute("DELETE FROM commitments WHERE id=?", (rid,))
        db.execute("DELETE FROM notify_log WHERE commitment_id=?", (rid,))
        for table in ("calendar_event_ends", "calendar_event_invitees", "calendar_event_writable"):
            db.execute(f"DELETE FROM {table} WHERE commitment_id=?", (rid,))


def store_events(account: str, items: list[dict], store=None) -> int:
    """Replace this account's Google rows with `items` (an account-scoped
    replace-set: other accounts and EventKit rows are untouched). Upserts by
    (source_id, when_ts) like AssistantStore.sync_source, so a row keeps its id
    and notify_log across refreshes."""
    store = store or _store()
    prefix = _prefix(account)
    now = time.time()
    written = 0
    with store.transaction() as db:
        native = {_dupe_key(r["title"], r["when_ts"]) for r in db.execute(
            "SELECT title, when_ts FROM commitments WHERE source='calendar' AND status='active' "
            "AND (source_id IS NULL OR substr(source_id,1,?)<>?)", (len(PREFIX), PREFIX))}
        existing = {(r["source_id"], r["when_ts"]): r["id"] for r in db.execute(
            "SELECT id, source_id, when_ts FROM commitments WHERE source='calendar' "
            "AND substr(source_id,1,?)=?", (len(prefix), prefix))}
        seen = set()
        for it in items:
            sid = str(it.get("source_id") or "")
            if not sid.startswith(prefix) or it.get("when_ts") is None:
                continue
            if _dupe_key(it.get("title"), it["when_ts"]) in native:
                continue   # the EventKit copy of the same event wins
            key = (sid, float(it["when_ts"]))
            if key in seen:
                continue
            seen.add(key)
            cid = existing.get(key) or uuid.uuid4().hex
            db.execute(_UPSERT, (cid, sid, it.get("kind", "meeting"), it.get("title") or "(untitled)",
                                 it.get("context"), it.get("organizer"), account.lower(), key[1],
                                 int(bool(it.get("all_day"))), it.get("location"), it.get("url"),
                                 float(it.get("confidence", 1.0)), now, now))
            cid = db.execute("SELECT id FROM commitments WHERE source='calendar' AND source_id=? AND when_ts=?",
                             key).fetchone()["id"]
            for table in ("calendar_event_ends", "calendar_event_invitees", "calendar_event_writable"):
                db.execute(f"DELETE FROM {table} WHERE commitment_id=?", (cid,))
            if it.get("end_ts") is not None:
                db.execute("INSERT INTO calendar_event_ends VALUES (?,?)", (cid, max(float(it["end_ts"]), key[1])))
            if type(it.get("invitee_count")) is int and it["invitee_count"] >= 0:
                flag = it.get("organizer_is_user")
                db.execute("INSERT INTO calendar_event_invitees VALUES (?,?,?)",
                           (cid, it["invitee_count"], int(flag) if type(flag) is bool else None))
            db.execute("INSERT INTO calendar_event_writable VALUES (?,?)", (cid, 0))
            written += 1
        _delete_rows(db, [rid for key, rid in existing.items() if key not in seen])
    return written


def stored_items(store=None, accounts: set[str] | None = None) -> list[dict]:
    """Google rows currently stored, as sync items, for connected accounts only."""
    store = store or _store()
    accounts = set(G.connected_accounts()) if accounts is None else accounts
    if not accounts:
        return []
    with store.transaction(write=False) as db:
        rows = db.execute(
            "SELECT c.*, e.end_ts AS end_ts, i.invitee_count AS invitee_count, "
            "i.organizer_is_user AS organizer_is_user FROM commitments c "
            "LEFT JOIN calendar_event_ends e ON e.commitment_id=c.id "
            "LEFT JOIN calendar_event_invitees i ON i.commitment_id=c.id "
            # Dismissed rows ride along too: dropping them from EventKit's
            # replace-set would delete the row and with it the dismissal.
            "WHERE c.source='calendar' AND substr(c.source_id,1,?)=?",
            (len(PREFIX), PREFIX)).fetchall()
    items = []
    for r in rows:
        if str(r["account"] or "").lower() not in accounts:
            continue
        flag = r["organizer_is_user"]
        items.append({"source_id": r["source_id"], "kind": r["kind"], "title": r["title"],
                      "context": r["context"], "organizer": r["organizer"], "account": r["account"],
                      "when_ts": float(r["when_ts"]), "end_ts": r["end_ts"], "all_day": bool(r["all_day"]),
                      "location": r["location"], "url": r["url"],
                      "invitee_count": r["invitee_count"],
                      "organizer_is_user": None if flag is None else bool(flag),
                      "calendar_writable": False, "confidence": 1.0})
    return items


def merge_native_items(items: list[dict], store=None) -> list[dict]:
    """Hook for POST /assistant/sync/calendar: EventKit's items plus the stored
    Google rows that don't duplicate an EventKit event, so the replace-set
    keeps them. Never raises; on any error EventKit's items pass unchanged."""
    native = [it for it in items if not str(it.get("source_id") or "").startswith(PREFIX)]
    try:
        google = stored_items(store)
    except Exception as exc:  # noqa: BLE001
        print(f"[google_calendar] merge skipped: {type(exc).__name__}", flush=True)
        return native
    keys = {_dupe_key(it.get("title"), it.get("when_ts")) for it in native}
    return native + [it for it in google if _dupe_key(it["title"], it["when_ts"]) not in keys]


_NATIVE_WRITE_KINDS = frozenset({"create_reminder", "create_calendar_event"})


def _account_row_ids(db, account: str) -> list[str]:
    prefix = _prefix(account)
    return [r["id"] for r in db.execute(
        "SELECT id FROM commitments WHERE source='calendar' AND (substr(source_id,1,?)=? "
        "OR (substr(source_id,1,?)=? AND lower(account)=?))",
        (len(prefix), prefix, len(PREFIX), PREFIX, account))]


def erase_if_present(account: str, store=None) -> int:
    """`erase` only when the account has rows (a read first, no write lock)."""
    account = account.strip().lower()
    store = store or _store()
    with store.transaction(write=False) as db:
        if not _account_row_ids(db, account):
            return 0
    return erase(account, store)


def erase(account: str, store=None) -> int:
    """Delete every Google Calendar row (and its side rows) for one account.

    Best effort beyond the rows themselves: schedule versions of the deleted
    rows (and any orphaned ones), and unclaimed, non-native-write assistant
    events whose JSON payload has `commitment_id` among those rows or a
    `source_id` under the account's gcal: prefix."""
    account = account.strip().lower()
    _status.pop(account, None)
    store = store or _store()
    prefix = _prefix(account)
    with store.transaction() as db:
        ids = _account_row_ids(db, account)
        _delete_rows(db, ids)
        for rid in ids:
            db.execute("DELETE FROM assistant_schedule_versions WHERE commitment_id=?", (rid,))
        db.execute("DELETE FROM assistant_schedule_versions WHERE commitment_id NOT IN "
                   "(SELECT id FROM commitments)")
        # Exact JSON fields only: commitment_id among the erased rows, or a
        # source_id under this account's gcal: prefix. Claimed rows and native
        # write actions are in flight or need their outcome kept: never touched.
        erased_ids = set(ids)
        for row in db.execute("SELECT id, kind, payload, claim_token FROM assistant_events").fetchall():
            if row["claim_token"] is not None or row["kind"] in _NATIVE_WRITE_KINDS:
                continue
            try:
                payload = json.loads(row["payload"] or "")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            cid, sid = payload.get("commitment_id"), payload.get("source_id")
            if (isinstance(cid, str) and cid in erased_ids) or (isinstance(sid, str) and sid.startswith(prefix)):
                db.execute("DELETE FROM assistant_events WHERE id=?", (row["id"],))
    try:
        from service.assistant.behavior_runtime import erase_google_calendar_account
        erase_google_calendar_account(account)
    except Exception as exc:  # noqa: BLE001 — reported by gmail_source.erase_account_data
        raise RuntimeError(f"behavior history purge failed: {type(exc).__name__}") from exc
    return len(ids)
