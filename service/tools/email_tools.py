"""Header-only sender digests and separate verbatim email lookups.

The inbox is READ by the Swift app (Mail.app AppleScript, clean Wisp.app identity
+ Automation grant) and pushed to /assistant/sync/emails — same split as the
calendar, because the Python backend can't get Automation access to Mail. The
digest uses pushed headers only and renders them deterministically.
  • `summarize_emails` — agent tool for "what's in my inbox?" / "summarize my
    emails from yesterday" on demand, with optional day filtering.
  • `run_daily_email_summary()` — scheduler entry (~8am); summarizes YESTERDAY
    (the full day just finished) and pushes a notification.

Three separate caches, three separate cadences (see MailReader.swift):
  • `_headers` — recent ~200 PER LINKED ACCOUNT, header-only, every 5 min. Fast
    path for "today"/"yesterday" and the default "recent inbox" summary. Depth
    is per-account because `_filter_account` narrows AFTER reading this cache:
    a shared top-200 would let a noisy account crowd a quiet one (a school
    address) out entirely, leaving an account-scoped question nothing to answer
    from.
  • `_history` — up to TWO YEARS back (raised from one, 2026-08-19), header-only,
    every ~30 min. Read from Mail's local Envelope Index when it is readable
    and its account labels can be trusted; otherwise an AppleScript walk that
    is incremental between full walks (see MailHistoryMerge.swift). Only
    old-mail lookups use it — `summarize_inbox_for_day` falls back to this when
    a requested day isn't covered by `_headers` — and the Daily Summary and
    mail readiness never read it (tests/test_daily_summary_history_independence.py).
  • `_raw_emails` — recent ~50, FULL body content, every 15 min. Stays small
    deliberately: `content of m` is the slow AppleScript call, so two years'
    worth of bodies isn't practical the way two years of headers is — verbatim
    lookups (view_emails) stay recent-only.

All three now carry an `account` field (which linked Mail.app account a message
came from) so a question can be scoped to one account once more than one is
linked — see `summarize_emails`/`view_emails`'s `account` parameter. With a
single account it's just noise Wisp filters out naturally. MailReader scans each
account's own inbox and merges by date before pushing here; the unified Mail.app
inbox it used to scan is a per-account CONCATENATION, so any top-N of it saw
only the first account.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import math
from service.publication_authority import Authority, Clock, Witness, mutating
import re
import threading
import time
from types import MappingProxyType
from typing import Mapping
from datetime import datetime, timedelta
from email.utils import parseaddr
from functools import wraps

from service.tools.timeranges import PERIOD_ARG, BadPeriod, resolve_span
from service.tools import cache_store
from service.tools.registry import register


# Legacy shared token ceiling imported by imessage_tools.
_SUMMARY_MAX_TOKENS = 2500
_RECENT_HEADER_CAP_PER_ACCOUNT = 200

# Latest inbox headers pushed by the Swift app in H2 control-separated records.
# Legacy pipe rows remain readable (see _parse_pipe_lines). Merged newest-first across every linked
# account. Recent-only (~200 per account) — see module docstring. Readers that
# care about ordering sort anyway; the merge is what makes that sort meaningful
# rather than something that has to be trusted.
_headers: str = ""
_headers_at: float = 0.0

# A persisted cache is useful after a backend restart, but it is not proof that
# Mail has completed a sync in THIS Wisp process.  That distinction matters most
# immediately after launch: the Daily Summary used to see restored rows, call
# the cache "ready", and confidently omit mail that arrived since the saved
# snapshot.  Only cache_emails(), reached by a live Swift MailReader push, moves
# this generation above zero.
_headers_sync_generation: int = 0

# Same header format, but the ~2-year-back scan (MailDBReader.readHistory, or
# MailReader.swift's historyBatchScript walk) — a separate cache so its slower
# cadence can't race/clobber the fast recent one above.
_history: str = ""
_history_at: float = 0.0

# Whether the last Mail sync could actually READ the inbox (Automation granted).
# None = never synced yet (just launched, or backend just started); True =
# AppleScript ran fine (permission is OK — an empty inbox still reports True);
# False = it errored (usually Automation not granted). Lets the "no data"
# message stop crying "grant permission" when permission is actually fine and
# the inbox is just still syncing or genuinely empty — the reported bug where
# "check my emails" asked for permission that was already granted, then
# "from yesterday" worked seconds later once the sync landed.
_email_available: bool | None = None
_email_reason: str = ""
# Reserved for a reader explicitly reporting work in flight. A successful
# local-index read is NOT still syncing, regardless of its modification time.
_email_sync_pending: bool = False
_email_read_source: str = ""

# Raw, full-content emails for view_emails (verbatim lookups) — a SEPARATE,
# smaller batch than _headers: MailReader.swift's rawScript pulls ~50 messages
# total, split evenly across linked accounts (fetching `content of m` for all
# 200 header-scan messages would make every 5-min sync noticeably slower).
# Unlike _headers this is a shared budget, not per-account depth: full bodies
# are the expensive fetch, so linking a second account shouldn't double the
# cost of every sync. Records are
# separated by ASCII \x02, fields within a record by \x01 — a raw email body
# routinely contains "|" and newlines, which the header format gets away with
# only because subjects don't.
_RAW_FS = "\x01"
_RAW_RS = "\x02"
_raw_emails: str = ""
_raw_emails_at: float = 0.0
# Reference resolution requires a live, successful scan, unlike display-only
# caches restored from disk. Empty-but-successful is distinct from cold.
_raw_reference_scan: dict = {}
# Reference consumers must read this single publication, never independently
# combine the compatibility body globals with coverage from another refresh.
_raw_snapshot_lock = threading.RLock()
_raw_snapshot_generation = 0
_raw_mail_snapshot: RawEmailSnapshot | None = None
_raw_publication = Authority("native-mail-body")
_raw_availability = Authority("native-mail-availability")
_raw_publication_clock = None
_raw_publication_monotonic = None
# Raw bodies carry real PII (addresses, order numbers, party plans) — purge
# after a week rather than let it sit in process memory indefinitely.
# MailReader.swift re-syncs raw content every 15 minutes, so in practice this
# is always fresh; the TTL is a backstop for the case where syncing itself has
# stalled (app not running, FDA revoked, etc.) — it now allows a week of
# staleness before dropping the cache outright, up from the previous 1 day.

# Restore whatever the last run had synced, so a backend restart doesn't leave
# these empty until each source's next (potentially very slow) sync lands —
# see cache_store's module docstring for the profile-build bug this fixes.
# Header readiness is separate from server freshness. Raw bodies retain their
# original persisted age so restarts and recomposition cannot extend the TTL.
_headers = cache_store.load("email_headers")
_history = cache_store.load("email_history")
_raw_emails = cache_store.load("email_raw")
def _source_receipt(value) -> float:
    """Unknown or corrupt source metadata cannot attest a successful refresh."""
    try:
        stamp = float(value or 0)
    except (ValueError, TypeError, OverflowError):
        return 0.0
    return stamp if math.isfinite(stamp) and 0 < stamp <= time.time() else 0.0


def _cache_file_exists(name: str) -> bool | None:
    """None means this adapter cannot attest filesystem provenance."""
    path = getattr(cache_store, "_path", None)
    if not callable(path):
        return None
    try:
        return path(name).exists()
    except OSError:
        return None


def _restored_native(part: str, fallback: str) -> str:
    name = "email_mail_" + part
    exists = _cache_file_exists(name)
    if exists is None:
        # Read-only staged adapters need not expose private metadata keys.
        # Unknown provenance cannot promote display data into native data.
        return ""
    if exists:
        return cache_store.load(name)
    # Once source ownership was recorded, an unlabelled display cache may
    # contain provider data. Missing native provenance must never adopt it.
    return "" if _cache_file_exists("email_sources") is not False else fallback


_swift_headers = _restored_native("headers", _headers)
_swift_history = _restored_native("history", _history)
_swift_raw = _restored_native("raw", _raw_emails)
if _headers:
    _headers_at = time.time()
if _history:
    _history_at = time.time()
if _raw_emails:
    try:
        path = getattr(cache_store, "_path", None)
        _raw_emails_at = path("email_raw").stat().st_mtime if callable(path) else 0.0
    except OSError:
        _raw_emails_at = 0.0

# Managed QA's staged-import contract checks that summaries never construct a
# model client at import time. Sender digests are deterministic, so this stays
# empty; retain the sentinel for that compatibility check.
_client = None


def cache_emails(headers: str) -> None:
    global _headers, _headers_at, _headers_sync_generation, _swift_headers
    _swift_headers = headers or ""
    _refresh_native_bindings()
    _headers = _compose_source(_swift_headers, "headers")
    _headers_at = time.time()
    _headers_sync_generation += 1
    _persist_composed_sources()


def cache_history_headers(history: str) -> None:
    global _history, _history_at, _swift_history, _native_history_at
    _swift_history = history or ""
    _history = _compose_source(_swift_history, "history")
    _history_at = time.time()
    _native_history_at = _history_at
    _persist_composed_sources()


# Provider snapshots are separate from native Mail snapshots. A failed refresh
# must preserve the last bytes and their original age, including after restart.
_external_accounts: dict[str, dict] = {}
_EXTERNAL_MAX_AGE = 300.0
_native_headers_at = 0.0
_native_source = ""
# Only a cache predating both provenance files may inherit display-file age.
# A modern composed cache (even with a fresh mtime) cannot attest its source.
_native_raw_at = (_raw_emails_at if _cache_file_exists("email_mail_raw") is False
                  and _cache_file_exists("email_sources") is False else 0.0)
_native_history_at = 0.0


def _restore_email_sources() -> None:
    global _external_accounts, _native_headers_at, _native_source, _native_raw_at, _native_history_at
    import json
    if _cache_file_exists("email_sources") is None:
        return
    try:
        saved = json.loads(cache_store.load("email_sources") or "{}")
        _native_headers_at = _source_receipt(saved.get("native_headers_at"))
        _native_source = str(saved.get("native_source") or "")
        _native_raw_at = _source_receipt(saved.get("native_raw_at") or _native_raw_at)
        _native_history_at = _source_receipt(saved.get("native_history_at"))
        for address, item in saved.get("accounts", {}).items():
            if isinstance(item, dict) and "@" in address:
                _external_accounts[address] = {**item, "verified": False,
                    "headers_verified": False, "history_verified": False, "raw_verified": False,
                    **{part + "_at": _source_receipt(item.get(part + "_at"))
                       for part in ("headers", "history", "raw")}}
    except (ValueError, TypeError, AttributeError):
        _external_accounts = {}


def _save_email_sources() -> bool:
    import json
    if _cache_file_exists("email_sources") is None:
        return False
    saved = json.dumps({
        "native_headers_at": _native_headers_at, "native_source": _native_source,
        "native_raw_at": _native_raw_at, "native_history_at": _native_history_at,
        "accounts": _external_accounts})
    cache_store.save("email_sources", saved)
    return cache_store.load("email_sources") == saved


def _persist_composed_sources(*, purge: bool = True) -> None:
    """Keep native provenance durable before writing any merged display data."""
    if purge:
        _purge_raw_if_expired(persist=False)
    if _cache_file_exists("email_sources") is None:
        return  # Read-only staged adapters cannot persist private provenance.
    durable = True
    for part, data in (("headers", _swift_headers), ("history", _swift_history),
                       ("raw", _swift_raw), ("identity", "\n".join(_swift_identity))):
        name = "email_mail_" + part
        cache_store.save(name, data)
        durable = (_cache_file_exists(name) is True and cache_store.load(name) == data) and durable
    durable = _save_email_sources() and durable
    if durable:
        for name, data in (("email_headers", _headers), ("email_history", _history),
                           ("email_raw", _raw_emails), ("identity_emails", "\n".join(_identity_emails))):
            cache_store.save(name, data)


def native_email_accounts() -> list[dict]:
    found = {}
    for row in _parse_header_records(_swift_headers) + _scan_marker_accounts(_swift_headers):
        key = row.get("account_id") or row["account"]
        if key:
            found[key] = {"id": key, "name": row["account"]}
    return list(found.values())


def configure_external_accounts(accounts: dict[str, dict]) -> None:
    """Only exact addresses or an explicit user-selected native ID are covered."""
    global _external_accounts
    selected = {}
    for address, config in accounts.items():
        key = address.strip().lower()
        old = _external_accounts.get(key, {})
        native_id = config.get("mail_account_id", "")
        native_name = next((row["name"] for row in native_email_accounts() if row["id"] == native_id), "")
        selected[key] = {**old, "mail_account_id": native_id,
                         "mail_account_name": native_name or old.get("mail_account_name", ""),
                         "headers": old.get("headers"), "history": old.get("history"),
                         "raw": old.get("raw")}
    _external_accounts = selected
    _refresh_native_bindings()
    _recompose_all()
    cache_identity(_swift_identity)


def _source_identity(record: str, *, raw: bool = False) -> tuple[str, str]:
    if raw:
        fields = record.split(_RAW_FS)
        if len(fields) == 9:
            return fields[2], fields[3]
        if len(fields) == 8:
            return fields[2], ""
        return (fields[1], "") if len(fields) > 1 else ("", "")
    if record.startswith(("H2\x01", "C2\x01", "C3\x01")):
        fields = record.split("\x01")
        if fields[0] == "H2":
            return (fields[3], fields[4]) if len(fields) >= 5 else ("", "")
        return (fields[1], fields[2]) if len(fields) >= 3 else ("", "")
    fields = record.split(" | ", 4)
    return ((fields[2] if fields[1] in ("R", "U") else fields[1]), "") if len(fields) > 2 else ("", "")


def _refresh_native_bindings() -> None:
    observed = native_email_accounts()
    for address, item in _external_accounts.items():
        exact = {row["id"] for row in observed if row["name"].casefold() == address}
        item["inferred_native_id"] = next(iter(exact)) if len(exact) == 1 else ""


def _covered_source(label: str, native_id: str, address: str, item: dict) -> bool:
    bound = item.get("mail_account_id") or item.get("inferred_native_id")
    return (not native_id and label.casefold() == address or native_id.casefold() == address or
            bool(bound and native_id == bound))


def _provider_selected(address: str, item: dict, part: str) -> bool:
    if item.get(part) is None:
        return False
    stamp = _source_receipt(item.get(part + "_at"))
    if part == "raw" and not stamp:
        return False  # Rejected body receipts must not shadow valid native data.
    if item.get(part + "_verified") and not item.get(part + "_error") and 0 <= time.time() - stamp <= _EXTERNAL_MAX_AGE:
        return True
    # A successful later native read is a better fallback than a failed provider
    # snapshot. Its server freshness is still disclosed separately.
    native_stamp = _source_receipt({"raw": _native_raw_at, "history": _native_history_at}.get(part, _native_headers_at))
    native = {"raw": _swift_raw, "history": _swift_history}.get(part, _swift_headers)
    records = native.split(_RAW_RS if part == "raw" else "\n")
    matched = any(_covered_source(*_source_identity(record, raw=part == "raw"), address, item)
                  for record in records if record.strip())
    if matched and _native_source == "mail_app" and native_stamp > stamp:
        return False
    return True


def _compose_source(native: str, part: str) -> str:
    if not any(item.get(part) is not None for item in _external_accounts.values()):
        return native
    raw = part == "raw"
    separator = _RAW_RS if raw else "\n"
    kept = []
    for record in native.split(separator):
        if not record.strip():
            continue
        label, native_id = _source_identity(record, raw=raw)
        # No successful provider snapshot for this part? Keep its native fallback.
        if not any(_provider_selected(address, item, part) and
                   _covered_source(label, native_id, address, item)
                   for address, item in _external_accounts.items()):
            kept.append(record)
    for address, item in _external_accounts.items():
        if _provider_selected(address, item, part):
            kept.extend(r for r in (item.get(part) or "").split(separator) if r.strip())
    if not raw:
        kept.sort(key=_row_ts, reverse=True)
    return separator.join(kept)


def _row_ts(row: str) -> float:
    try:
        return float(row.split("\x01", 2)[1] if row.startswith("H2\x01") else row.split(" | ", 1)[0])
    except (ValueError, IndexError):
        return 0.0


def _recompose_all() -> None:
    global _headers, _history, _raw_emails
    _headers = _compose_source(_swift_headers, "headers")
    _history = _compose_source(_swift_history, "history")
    _raw_emails = _compose_source(_swift_raw, "raw")


def cache_external_emails(*, account: str, headers: str | None = None,
                          history: str | None = None, raw: str | None = None) -> None:
    global _headers_at, _history_at, _raw_emails_at, _headers_sync_generation
    if account not in _external_accounts:
        return  # Disconnected or superseded while the request was in flight.
    now = time.time()
    item = _external_accounts[account]
    for part, data in (("headers", headers), ("history", history), ("raw", raw)):
        if data is not None:
            item[part] = data
            item[part + "_at"] = now
            item[part + "_error"] = ""
            item[part + "_verified"] = True
    if headers is not None:
        item["verified"] = True
        _headers_at = now
        _headers_sync_generation += 1
    if history is not None:
        _history_at = now
    if raw is not None:
        _raw_emails_at = now
    _recompose_all()
    _persist_composed_sources()


def external_email_error(account: str, part: str, reason: str) -> None:
    if account in _external_accounts:
        _external_accounts[account][part + "_error"] = reason
        _recompose_all()
        _persist_composed_sources()


def _external_fresh() -> bool:
    now = time.time()
    return bool(_external_accounts) and all(
        item.get("headers_verified") and not item.get("headers_error") and
        0 < _source_receipt(item.get("headers_at")) and
        0 <= now - _source_receipt(item.get("headers_at")) <= _EXTERNAL_MAX_AGE
        for item in _external_accounts.values())


_restore_email_sources()


# The user's OWN email addresses, from Mail.app's configured accounts (pushed
# by MailReader.swift). Ground truth for identity.py — without it, brief.py
# and the summarizers can't tell the user's own outgoing mail apart from mail
# sent to them.
_identity_emails: list[str] = []

# RFC 2606 / RFC 6761 reserve these names precisely so they can never resolve to
# a real host, which makes them unambiguous placeholder data — nobody's Mail.app
# account lives at one. Filtered because scripts/install_cache_fixtures.sh
# writes a fixture identity_emails.txt into the LIVE cache, and unlike every
# other cache file it has no real counterpart to overwrite it: the real mail
# syncs replaced email_headers/raw/history and left this one asserting the
# user's address is testuser@example.com — and this is stated as ground truth
# in every mail summary. Wrong identity is worse than no identity, so drop it.
_PLACEHOLDER_DOMAINS = (".example.com", ".example.net", ".example.org",
                        "@example.com", "@example.net", "@example.org",
                        ".test", ".invalid", ".localhost", ".example")


def _is_placeholder(email: str) -> bool:
    return email.lower().strip().endswith(_PLACEHOLDER_DOMAINS)


_swift_identity: list[str] = []


def cache_identity(emails: list[str]) -> None:
    global _identity_emails, _swift_identity
    _swift_identity = [e.strip() for e in (emails or [])
                       if e and e.strip() and not _is_placeholder(e)]
    _identity_emails = list(_swift_identity)
    for address in _external_accounts:
        if address not in (e.lower() for e in _identity_emails):
            _identity_emails.append(address)
    _persist_composed_sources()


def get_identity_emails() -> list[str]:
    return list(_identity_emails)


_identity_emails = [e for e in cache_store.load("identity_emails").splitlines()
                    if e and not _is_placeholder(e)]
_swift_identity = _restored_native("identity", "\n".join(
    e for e in _identity_emails if e.casefold() not in _external_accounts)).splitlines()


@mutating
def set_email_availability(available: bool, reason: str = "",
                           syncing: bool = False, read_source: str = "") -> None:
    """Record whether the Swift MailReader's last sync could read the inbox."""
    global _email_available, _email_reason, _email_sync_pending, _email_read_source
    global _native_headers_at, _native_source
    with _raw_snapshot_lock:
        changed = _email_available is not available or _raw_availability.token() is None
        if changed:
            _raw_availability.withdraw()
        _email_available = available
        _email_reason = reason
        _email_sync_pending = syncing and available
        _email_read_source = read_source if read_source in {"local_index", "mail_app"} else ""
        if available and not syncing:
            _native_headers_at = time.time()
            _native_source = _email_read_source
            if _external_accounts:
                _recompose_all()
            _save_email_sources()
        if available is True and changed:
            _raw_availability.publish()


def _no_inbox_message() -> str:
    """The right 'no data' message for the CURRENT sync state — only blames
    permission when the sync actually failed."""
    if _external_accounts and not any(item.get("headers") is not None for item in _external_accounts.values()):
        return ("I couldn’t refresh email from Google. No verified provider snapshot is available. "
                + email_freshness_warning())
    if _email_available is False and _external_fresh():
        return "No emails found."
    if _email_available is False:
        reason = _email_reason or "Mail Automation access isn't granted"
        guidance = ("Grant Wisp access in System Settings ▸ Privacy & Security ▸ Automation ▸ Wisp ▸ Mail, then try again."
                    if "automation" in reason.casefold() else "Open Mail and let it refresh, then try again.")
        return (f"I couldn’t refresh Mail: {reason}. No usable cached emails are available, "
                f"so I couldn’t verify your inbox. {guidance}")
    if _email_available is True:
        # Local read completion cannot prove that the server inbox is empty.
        if email_freshness_warning():
            return "No emails found in the available cache; your current inbox could not be verified."
        return "No emails found."
    # Never synced yet — could be a just-launched race OR missing permission; don't
    # assert either. (Mail syncs within ~15s of launch; a retry usually resolves it.)
    return ("(No inbox data yet — Mail may still be syncing after launch. Try again in "
            "a moment. If it keeps saying this, grant Wisp Automation access in System "
            "Settings ▸ Privacy & Security ▸ Automation ▸ Wisp ▸ Mail.)")


def email_sync_state() -> str:
    """Current-session header readiness: ``ready``, ``syncing``, or
    ``unavailable``.

    Parseable restored rows do not count as ready.  They may be a perfectly
    good fallback later, but they cannot answer "today" accurately until the
    app has told us its first live scan finished.
    """
    if _external_fresh() or any(item.get("headers") is not None for item in _external_accounts.values()):
        return "ready"
    if any(item.get("headers_error") for item in _external_accounts.values()):
        return "ready" if _parse_lines() else "unavailable"
    if _email_available is False:
        return "unavailable"
    if _email_sync_pending:
        return "syncing"
    if _headers_sync_generation > 0:
        return "ready"
    return "syncing"


def email_source_details(account: str | None = None, *, part: str = "headers") -> list[dict]:
    now = time.time()
    result = []
    native = {"raw": _swift_raw, "history": _swift_history}.get(part, _swift_headers)
    native_rows = []
    for record in native.split(_RAW_RS if part == "raw" else "\n"):
        if record.strip():
            label, native_id = _source_identity(record, raw=part == "raw")
            native_rows.append({"account": label, "account_id": native_id})
    if part == "history" and not native_rows:
        native_rows = native_email_accounts()
        native_rows = [{"account": r["name"], "account_id": r["id"]} for r in native_rows]
    selected = _resolve_account_groups(account, native_rows)[0] if account else None
    for address, item in _external_accounts.items():
        if selected is not None and _account_group(address, address) not in selected:
            continue
        stamp = _source_receipt(item.get(part + "_at"))
        error = str(item.get(part + "_error") or "")
        fresh = bool(item.get(part + "_verified") and stamp and not error and 0 <= now - stamp <= _EXTERNAL_MAX_AGE)
        result.append({"account": address, "source": "gmail", "fresh": fresh,
                       "last_success": stamp or None, "error": error,
                       "age_seconds": max(0, now - stamp) if stamp else None})
    observed = {}
    for row in native_rows:
        label, native_id = row["account"], row["account_id"]
        if not label or selected is not None and _account_group(label, native_id) not in selected:
            continue
        if any(_provider_selected(address, item, part) and _covered_source(label, native_id, address, item)
               for address, item in _external_accounts.items()):
            continue
        observed[native_id or label] = label
    native_stamp = _source_receipt({"raw": _native_raw_at, "history": _native_history_at}.get(part, _native_headers_at))
    for label in observed.values():
        # Mail.app read completion still cannot prove a server fetch completed.
        result.append({"account": label, "source": _email_read_source or _native_source or "saved_cache",
                       "fresh": bool(_email_read_source == "mail_app" and _email_available and
                                     0 <= now - native_stamp <= _EXTERNAL_MAX_AGE),
                       "last_success": native_stamp or None,
                       "age_seconds": max(0, now - native_stamp) if native_stamp else None,
                       "error": _email_reason if _email_available is False else ""})
    return result


def external_recent_complete(address: str) -> bool:
    item = _external_accounts.get(address, {})
    if (not item.get("headers_verified") or item.get("headers_error") or
            not _source_receipt(item.get("headers_at")) or
            not 0 <= time.time() - _source_receipt(item.get("headers_at")) <= _EXTERNAL_MAX_AGE):
        return False
    headers = item.get("headers")
    if headers == "":
        return True
    if headers is None:
        return False
    return any(line.split("\x01")[0] == "C2" and len(line.split("\x01")) == 6 and
               line.split("\x01")[4:] == ["0", "0"] for line in headers.splitlines())


def _range_history(start: float, end: float) -> list[dict]:
    return [row for row in _parse_header_records(_history, snapshot="history")
            if start <= row["ts"] < end and not any(external_recent_complete(address) and
                _account_group(row["account"], row.get("account_id", "")) == _account_group(address, address)
                for address in _external_accounts)]


def _history_warning(account: str | None = None) -> str:
    return email_freshness_warning(account, part="history")


def email_freshness_warning(account: str | None = None, *, part: str = "headers") -> str:
    details = email_source_details(account, part=part)
    warnings = []
    for item in details:
        if item["fresh"] or part == "history" and item["source"] == "gmail" and external_recent_complete(item["account"]):
            continue
        last = (datetime.fromtimestamp(item["last_success"]).strftime("%b %-d, %-I:%M %p")
                if item["last_success"] else "unknown")
        if item["source"] == "gmail":
            reason = item["error"] or "a current Google refresh has not completed"
            kind = "Gmail history cache" if part == "history" else "Gmail cache"
            warnings.append(f"{item['account']}: {kind} is stale or unverified; last successful refresh {last}. {reason}.")
        else:
            failure = f" The latest Mail refresh failed: {item['error']}." if item["error"] else ""
            warnings.append(f"{item['account']}: Email is from Mail’s local cache. Newer messages may be missing; "
                            f"server freshness is unverified (last local {'history ' if part == 'history' else ''}read {last}). "
                            f"To check for newer mail, open Mail and let it refresh.{failure}")
    # Preserve the native diagnostic for successful empty local-index scans, too.
    if not details and _email_read_source == "local_index":
        warnings.append("Email is from Mail’s local cache. Newer messages may be missing; "
                        "server freshness is unverified. To check for newer mail, open Mail and let it refresh."
                        + (f" The latest Mail refresh failed: {_email_reason}." if _email_available is False and _email_reason else ""))
    return "\n".join(warnings)


def with_email_freshness_note(text: str, warning: str) -> str:
    """Attach the limitation outside model output so it cannot be omitted."""
    return f"{warning}\n\n{text}" if warning and warning not in text else text


def _disclose_mail_freshness(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        # Digests are synchronous once readiness resolves. Inspect freshness
        # after on-demand refresh so a successful retry removes the old caveat.
        import inspect
        bound = inspect.signature(function).bind_partial(*args, **kwargs)
        account = bound.arguments.get("account")
        part = "raw" if function.__name__.startswith("view_email") else "headers"
        result = await function(*args, **kwargs)
        warning = email_freshness_warning(account, part=part) if account or part == "raw" else email_freshness_warning()
        if function.__name__ in {"summarize_inbox_for_day", "summarize_inbox_for_period"}:
            history_warning = _history_warning(account)
            if history_warning and history_warning != warning:
                warning = "\n".join(filter(None, (warning, history_warning)))
        return with_email_freshness_note(result, warning)
    return wrapped


def email_syncing_message() -> str:
    """Directly user-facing launch-race response (safe to short-circuit)."""
    return ("Wisp is still syncing your email after launch, so I’m holding off "
            "on the mail summary rather than showing you an incomplete one. "
            "Try again in a moment.")


# --- Machine-sender classification -------------------------------------------
#
# Splits automated/marketing traffic (job alerts, newsletters, receipts) out
# from mail actually from a person — see brief.py's _email_block, which uses
# this to keep the daily summary's "worth a look" section from being drowned
# out by notification trivia. Deliberately generous heuristic, not a spam
# filter: a false positive here just means one real email lands in the
# "automated" bucket instead of the human one, which costs little next to a
# false negative burying a person's email in a wall of marketing noise.
_MACHINE_SENDER_HINTS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "notification", "alert", "newsletter", "digest",
    "unsubscribe", "auto-confirm", "info@", "support@", "updates@", "news@",
)

# Known automated/marketing senders by NAME (case-insensitive, prefix match —
# "Glassdoor Jobs" and "Glassdoor" both hit "glassdoor"). Deliberately excludes
# schools/employers/orgs (UCSC, Cal Poly, etc.) — application and enrollment
# mail is genuinely personal signal, even though it's also high-volume and
# templated.
_MACHINE_SENDER_NAMES = (
    "indeed", "ziprecruiter", "glassdoor", "linkedin", "paypal", "dropbox",
    "google play", "college board", "amazon", "facebook", "instagram",
    "twitter", "docusign", "venmo", "cash app", "uber", "lyft", "doordash",
    "grubhub", "netflix", "spotify", "canva", "notion", "slack", "zoom",
    "calendly", "eventbrite", "meetup", "ticketmaster", "airbnb", "expedia",
    "kayak", "booking.com", "google ai", "google one", "apple",
)


def is_machine_sender(sender: str) -> bool:
    """True for an automated/marketing/notification sender, false for anyone
    who looks like they actually wrote the email themselves."""
    s = (sender or "").strip().lower()
    if not s:
        return False
    if any(h in s for h in _MACHINE_SENDER_HINTS):
        return True
    # Substring, not prefix: a real recruiter signs mail "Phil @ ZipRecruiter"
    # (name first), which a startswith("ziprecruiter") check misses entirely.
    return any(n in s for n in _MACHINE_SENDER_NAMES)


# Summary views should not spend space on codes, promotions, or duplicate
# arrivals.  This is deliberately separate from `is_machine_sender`: a payment
# receipt or security alert may be automatic yet still be useful in a summary.
_MARKETING_SUBJECT = re.compile(
    r"\b(?:sale|deal|offer|promo(?:tion)?|discount|coupon|save \d|"
    r"shop now|new arrivals|limited time|newsletter|digest|unsubscribe)\b",
    re.IGNORECASE)
_AUTOMATED_SIGNAL = re.compile(
    r"\b(?:security alert|password|sign[ -]?in|login|suspicious|fraud|"
    r"payment|receipt|invoice|order|delivery|shipment|reservation|itinerary|"
    r"meeting (?:invite|updated)|mentioned|assigned|direct message|invited|"
    r"action required|expir(?:es|ing))\b",
    re.IGNORECASE)


def is_summary_noise(sender: str, subject: str) -> bool:
    """Compatibility hint for callers; routine mail is ranked, never hidden."""
    return bool(_MARKETING_SUBJECT.search(subject or "") or
                (is_machine_sender(sender) and not _AUTOMATED_SIGNAL.search(subject or "")))


def filter_summary_rows(rows: list[tuple[float, str, str, str, bool | None]]) -> list[tuple[float, str, str, str, bool | None]]:
    """Legacy tuple view: collapse only exact repeats, retaining routine mail."""
    return sorted(dict.fromkeys(rows), key=lambda row: row[0], reverse=True)


def sender_stats() -> dict[str, dict]:
    """Per-sender volume across the header history: {sender: {count, first_ts,
    last_ts}} — deterministic, for memory/entities.py."""
    stats: dict[str, dict] = {}
    for ts, _account, sender, _subject, _unread in (_parse_history() or _parse_lines()):
        key = sender.strip()
        if not key:
            continue
        s = stats.setdefault(key, {"count": 0, "first_ts": ts, "last_ts": ts})
        s["count"] += 1
        s["first_ts"] = min(s["first_ts"], ts)
        s["last_ts"] = max(s["last_ts"], ts)
    return stats


_RAW_TTL_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class RawEmailSnapshot:
    """One live body/identity/coverage generation; all nested values immutable.

    This contains mailbox evidence, not authenticated RFC unsubscribe headers
    or historical open events. Use raw_email_snapshot(), which fails closed,
    rather than accessing the internal publication directly.
    """
    generation: int
    synced_at: float
    rows: tuple[Mapping[str, object], ...]
    accounts: tuple[str, ...]
    failed_accounts: tuple[str, ...]
    complete: bool
    decode_complete: bool
    identity_complete: bool
    failure_reason: str
    raw_digest: str


def _raw_snapshot_candidate(raw: str, coverage: dict | None, stamp: float) -> RawEmailSnapshot:
    rows = _parse_raw_text(raw)
    record_count = sum(bool(record.strip()) for record in raw.split(_RAW_RS))
    scan = coverage if isinstance(coverage, dict) else {}
    accounts_value, failures_value = scan.get("accounts"), scan.get("failed_accounts")
    valid_accounts = (isinstance(accounts_value, (list, tuple))
                      and all(isinstance(value, str) and value.strip() for value in accounts_value))
    valid_failures = (isinstance(failures_value, (list, tuple))
                      and all(isinstance(value, str) and value.strip() for value in failures_value))
    accounts = tuple(accounts_value) if valid_accounts else ()
    failures = tuple(failures_value) if valid_failures else ()
    decode_complete = record_count == len(rows) and scan.get("decode_complete", True) is True
    identities, labels = {}, {}
    identity_complete = valid_accounts and valid_failures
    for row in rows:
        account_id, message_id = row["account_id"], row["message_id"]
        if not account_id or not message_id or row["account"] not in accounts:
            identity_complete = False
        key = (account_id, message_id)
        if key in identities and identities[key] != row:
            identity_complete = False
        if account_id in labels and labels[account_id] != row["account"]:
            identity_complete = False
        identities[key], labels[account_id] = row, row["account"]
    # One display label naming several native accounts cannot demonstrate
    # per-account coverage with today's label-only coverage envelope.
    if len(set(labels.values())) != len(labels):
        identity_complete = False
    complete = (scan.get("complete") is True and valid_accounts and valid_failures
                and not failures and decode_complete and identity_complete)
    return RawEmailSnapshot(0, stamp, tuple(MappingProxyType(dict(row)) for row in rows),
                            accounts, failures, complete, decode_complete, identity_complete,
                            str(scan.get("failure_reason") or ""),
                            hashlib.sha256(raw.encode("utf-8")).hexdigest())


def provider_history_snapshot(provider, *, expected_receipt=None, required_refs=()):
    """Pure, explicit provider evidence API; separate from native completeness.

    No implicit source construction, cache warming, token refresh or network.
    Callers must recheck the same receipt and record refs before using results.
    Native-only raw_email_snapshot retains its existing admission contract.
    """
    from service.gmail_behavior_history import HistoryProvider
    from service.gmail_source import PublicHistoryBridge
    if not isinstance(provider, (HistoryProvider, PublicHistoryBridge)):
        return None
    return provider.snapshot(expected_receipt=expected_receipt, required_refs=required_refs)


def raw_email_snapshot(*, expected_generation: int | None = None) -> RawEmailSnapshot | None:
    """Read once for coherent provenance, or None for ineligible evidence.

    Coverage describes the native recent body scan, not all mailbox history.
    Restored/legacy globals alone never create a publication. Callers must use
    this snapshot's rows and generation together, not reread _parse_raw() or
    raw_reference_metadata(). Holding an older returned object cannot mutate it.
    """
    with _raw_snapshot_lock:
        snapshot = _raw_mail_snapshot
        if (snapshot is None or _email_available is not True or not snapshot.complete
                or not 0 <= time.time() - snapshot.synced_at <= 20 * 60
                or (expected_generation is not None and expected_generation != snapshot.generation)):
            return None
        return snapshot


@mutating
def cache_raw_emails(raw: str, coverage: dict | None = None) -> None:
    global _raw_emails, _raw_emails_at, _raw_reference_scan
    global _raw_mail_snapshot, _raw_snapshot_generation, _swift_raw, _native_raw_at
    global _raw_publication_clock, _raw_publication_monotonic
    raw = raw or ""
    stamp = time.time()
    scan = dict(coverage or {})
    for key in ("accounts", "failed_accounts"):
        if isinstance(scan.get(key), (list, tuple)):
            scan[key] = list(scan[key])  # Own the mutable caller envelope before parsing.
    # Prepare from arguments only. A concurrent writer cannot change which
    # bytes these account/message/coverage checks describe.
    candidate = _raw_snapshot_candidate(raw, scan, stamp)
    with _raw_snapshot_lock:
        _raw_publication.withdraw()
        _raw_publication_clock = Clock()
        _raw_publication_monotonic = time.monotonic()
        _raw_publication_clock.observe(stamp, _raw_publication_monotonic)
        _raw_snapshot_generation += 1
        snapshot = RawEmailSnapshot(_raw_snapshot_generation, candidate.synced_at,
            candidate.rows, candidate.accounts, candidate.failed_accounts, candidate.complete,
            candidate.decode_complete, candidate.identity_complete, candidate.failure_reason,
            candidate.raw_digest)
        if scan.get("failed_accounts") and not scan.get("accounts"):
            _raw_reference_scan = {**scan, "synced_at": stamp, "complete": False}
            _raw_mail_snapshot = snapshot
            return  # Preserve old display bytes, never their complete provenance.
        # Display bytes merge in Gmail rows; the evidence snapshot above stays
        # Mail-only (task evidence/references address Mail messages).
        _swift_raw = raw
        _native_raw_at = stamp
        _raw_emails = _compose_source(raw, "raw")
        _raw_emails_at = stamp
        _raw_reference_scan = {**scan, "synced_at": stamp,
                               "decode_complete": candidate.decode_complete}
        _raw_mail_snapshot = snapshot  # Atomic body + identity + coverage publication.
        _persist_composed_sources()
        if snapshot.complete:
            _raw_publication.publish()


@mutating
def prepare_publication_witness(snapshot):
    """Qualify a captured native body publication outside the publish lease.

    The returned witness never calls this getter, parses content, touches disk
    or refreshes the source. Restored caches and caller-made copies cannot
    acquire the live publication's stamp or extend its original receipt age.
    """
    with _raw_snapshot_lock:
        if (type(snapshot) is not RawEmailSnapshot or _raw_mail_snapshot is not snapshot
                or raw_email_snapshot(expected_generation=snapshot.generation) is not snapshot
                or _raw_publication_clock is None or _raw_publication_monotonic is None):
            return None
        return Witness.combine(_raw_availability.token(),
            _raw_publication.token(clock=_raw_publication_clock,
                expires_wall=snapshot.synced_at+1200,
                expires_monotonic=_raw_publication_monotonic+1200))


def raw_reference_metadata() -> dict:
    scan = _raw_reference_scan
    stamp = float(scan.get("synced_at") or 0)
    return {"synced_at": stamp,
            "accounts": list(scan.get("accounts") or []),
            "failed_accounts": list(scan.get("failed_accounts") or []),
            "failure_reason": str(scan.get("failure_reason") or ""),
            "available": bool(stamp and 0 <= time.time() - stamp <= 20 * 60
                              and scan.get("decode_complete", True)),
            "complete": scan.get("complete") is True}


async def ensure_reply_source(timeout_seconds: float = 2.5) -> None:
    meta = raw_reference_metadata()
    if meta["available"] and meta["complete"]:
        return
    from service.assistant.hub import hub
    generation = _raw_reference_scan.get("synced_at")
    await hub.publish({"type": "sync_emails_now"})
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _raw_reference_scan.get("synced_at") != generation:
            return
        await asyncio.sleep(0.1)


@mutating
def _purge_raw_if_expired(*, persist: bool = True) -> None:
    """Admit bodies only from source snapshots with a valid original receipt.

    The composed display store is never evidence of ownership or receipt age.
    Rebuild it on every read, including when no source needed expiration.
    """
    global _raw_emails, _raw_emails_at, _raw_mail_snapshot, _swift_raw, _native_raw_at
    changed = False
    with _raw_snapshot_lock:
        now = time.time()
        def receipt(value) -> float:
            stamp = _source_receipt(value)
            return stamp if stamp and 0 <= now - stamp <= _RAW_TTL_SECONDS else 0.0

        for item in _external_accounts.values():
            if item.get("raw") is not None and not receipt(item.get("raw_at")):
                item["raw"] = None
                item["raw_at"] = 0
                changed = True
        if _swift_raw and not receipt(_native_raw_at):
            _raw_publication.withdraw()
            _swift_raw = ""
            _native_raw_at = 0.0
            _raw_mail_snapshot = None
            changed = True
        admitted = _compose_source(_swift_raw, "raw")
        changed = changed or admitted != _raw_emails
        _raw_emails = admitted
        stamps = [receipt(_native_raw_at)] if _swift_raw else []
        stamps.extend(receipt(item.get("raw_at")) for item in _external_accounts.values()
                      if item.get("raw") is not None)
        _raw_emails_at = max(stamps, default=0.0)
        if changed and persist:
            _persist_composed_sources(purge=False)


def _parse_raw() -> list[dict]:
    return _parse_raw_text(_raw_emails)


def _parse_raw_text(raw: str) -> list[dict]:
    """Each raw record -> {ts, account, sender, to, subject, message_id, body}.
    Skips anything that doesn't parse (e.g. a message AppleScript couldn't
    read).

    Current records have 9 fields: timestamp, read flag, account name, native
    account ID, sender, recipients, subject, Message-ID, and body. Also accepts
    the older 8-field flagged and 6/7-field unflagged formats. The caches
    are restored from disk across restarts (see cache_store), so right after an
    upgrade this parses a file the OLD app wrote — rejecting those on a length
    mismatch would blank the user's inbox tools until the next full raw sync
    (up to 15 minutes) for no reason. Old records simply carry no message_id
    and can't be replied to/archived until they're re-synced.
    """
    out: list[dict] = []
    for record in raw.split(_RAW_RS):
        record = record.strip()
        if not record:
            continue
        parts = record.split(_RAW_FS)
        # Read status was added as field 1 (right after the timestamp, same
        # placement and same reason as the header format). `unread` is None for
        # records written before it existed — see _parse_pipe_lines.
        unread: bool | None = None
        account_id = ""
        has_native_account_id = len(parts) == 9
        if len(parts) in (8, 9):
            if parts[1] not in ("R", "U"):
                continue
            unread = parts[1] == "U"
            parts = [parts[0]] + parts[2:]
        if len(parts) == 8:
            ts_s, account, account_id, sender, to, subject, msg_id, body = parts
        elif len(parts) == 7:
            ts_s, account, sender, to, subject, msg_id, body = parts
        elif len(parts) == 6:
            ts_s, account, sender, to, subject, body = parts
            msg_id = ""
        else:
            continue
        if has_native_account_id and not account_id.strip():
            continue
        try:
            ts = float(ts_s)
            datetime.fromtimestamp(ts)  # reject non-finite/out-of-range dates
        except (ValueError, OverflowError, OSError):
            continue
        out.append({"ts": ts, "account": account, "account_id": account_id, "sender": sender.strip(),
                    "to": to.strip(), "subject": subject.strip(),
                    "message_id": msg_id.strip(), "body": body.strip(),
                    "unread": unread})
    return out


def _parse_pipe_lines(text: str) -> list[tuple[float, str, str, str, bool | None]]:
    """Each header line -> (ts, account, sender, subject, unread).

    TWO FORMATS, both accepted:
        "epochSecs | R/U | account | sender | subject"   (current)
        "epochSecs | account | sender | subject"         (older builds)

    `unread` is True/False from Mail's own read status, or **None** when the
    line predates the flag. None means "unknown", NOT "read" — the caches are
    restored from disk across restarts (see cache_store), so right after an
    upgrade this parses a file the old app wrote, and treating those as read
    would silently answer "no unread mail" from stale data. `_unread_rows`
    below is what turns that distinction into an honest answer.

    The flag is SECOND rather than last because the final field has to absorb
    any " | " inside a subject line — a trailing flag would be swallowed by it.
    Skips anything that doesn't parse (e.g. a stray line without a timestamp).
    """
    out: list[tuple[float, str, str, str, bool | None]] = []
    seen: set[str] = set()
    for line in text.strip().split("\n"):
        line = line.rstrip("\r")
        # Byte-identical duplicate lines are dropped. The live cache on
        # 2026-08-14 held every header EXACTLY three times — 1200 lines, 400
        # distinct, every one at multiplicity 3 — so the sync writing it is
        # scanning something three times over (MailReader.swift; the parser
        # can't tell which mailbox pass a line came from, so it can't be fixed
        # from here). Downstream that reads as real repetition: the daily brief
        # described one "I want to connect" email as three separate ones, and
        # every inbox count on this path was 3x the truth.
        #
        # Safe to collapse because these fields identify a message as closely as
        # this cache can: same account, same sender, same subject, same second.
        # Two genuinely distinct emails matching all four are a duplicate
        # delivery, and reporting that once is the right answer anyway. Done at
        # the parser rather than in one consumer so the brief, the summarizers,
        # sender_stats, and the profile builder all agree on how much mail there
        # actually is.
        if line in seen:
            continue
        seen.add(line)
        if line.startswith("H2\x01"):
            fields = line.split("\x01")
            if len(fields) not in (9, 10) or fields[2] not in ("R", "U"):
                continue
            try:
                ts = float(fields[1])
                datetime.fromtimestamp(ts)
            except (ValueError, OverflowError, OSError):
                continue
            sender = fields[5] or fields[6]
            out.append((ts, fields[3], sender, fields[8], fields[2] == "U"))
            continue
        parts = line.split(" | ", 4)
        unread: bool | None = None
        if len(parts) == 5 and parts[1] in ("R", "U"):
            unread = parts[1] == "U"
            parts = [parts[0], parts[2], parts[3], parts[4]]
        elif len(parts) == 5:
            # Five fields but no flag: an old-format line whose SUBJECT
            # contained " | ". Re-split so the subject keeps it.
            parts = line.split(" | ", 3)
        if len(parts) != 4:
            continue
        try:
            ts = float(parts[0])
            datetime.fromtimestamp(ts)
        except (ValueError, OverflowError, OSError):
            continue
        out.append((ts, parts[1], parts[2], parts[3], unread))
    return out


def _normalized_address(value: str) -> str:
    address = parseaddr(value or "")[1].strip().casefold()
    return address if "@" in address and not any(c.isspace() for c in address) else ""


def _parse_header_records(text: str, *, snapshot: str = "") -> list[dict]:
    """Header-only records, including identity metadata on current H2 rows.

    Legacy pipe rows remain readable. When a sender address is absent, no
    display-name-only grouping is inferred: each such row stands alone.
    """
    records: list[dict] = []
    seen: set[tuple] = set()
    no_id_occurrences: dict[str, int] = {}
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("H2\x01"):
            parts = line.split("\x01")
            if len(parts) not in (9, 10) or parts[2] not in ("R", "U"):
                continue
            try:
                ts = float(parts[1])
                datetime.fromtimestamp(ts)
            except (ValueError, OverflowError, OSError):
                continue
            _, _, flag, account, account_id, name, address, message_id, subject = parts[:9]
            native_id = parts[9].strip() if len(parts) == 10 else ""
            address = _normalized_address(address or name)
            record = {"ts": ts, "account": account, "account_id": account_id,
                      "sender": name or address or "Unknown sender", "sender_address": address,
                      "message_id": message_id.strip(), "native_id": native_id,
                      "subject": subject, "_snapshot": snapshot,
                      "unread": flag == "U"}
        else:
            parsed = _parse_pipe_lines(line)
            if not parsed:
                continue
            ts, account, sender, subject, unread = parsed[0]
            name, address = parseaddr(sender)
            record = {"ts": ts, "account": account, "account_id": "",
                      "sender": name or sender, "sender_address": _normalized_address(address),
                      "message_id": "", "native_id": "",
                      "subject": subject, "unread": unread, "_snapshot": snapshot}
        account_key = (record["account_id"] or record["account"]).casefold()
        keys = []
        if account_key and record["message_id"]:
            keys.append(("message", account_key, record["message_id"].casefold()))
        if account_key and record["native_id"]:
            keys.append(("native", account_key, record["native_id"].casefold()))
        if not keys and line.startswith("H2\x01"):
            # Identical no-ID headers can belong to distinct messages received
            # in the same second. Preserve their multiplicity within a scan;
            # matching occurrence numbers let recent/history overlap collapse.
            occurrence = no_id_occurrences.get(line, 0)
            no_id_occurrences[line] = occurrence + 1
            keys.append(("no-id-h2", line, occurrence))
            record["_fallback_key"] = keys[0]
        elif not keys:
            keys.append(("exact", line))
        duplicate = any(key in seen for key in keys)
        seen.update(keys)
        if not duplicate:
            records.append(record)
    return sorted(records, key=lambda row: row["ts"], reverse=True)


def _unique_records(rows: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result: list[dict] = []
    # Current read state wins over an older historical copy, even if that
    # historical header has an anomalous date. Restore date order afterwards.
    for row in sorted(rows, key=lambda r: (r.get("_snapshot") == "recent", r["ts"]), reverse=True):
        account = (row["account_id"] or row["account"]).casefold()
        identities = []
        if account and row.get("message_id"):
            identities.append(("message", _account_group(row["account"], row["account_id"]),
                               row["message_id"].casefold()))
        if account and row.get("native_id"):
            identities.append(("native", account, row["native_id"].casefold()))
        if not identities:
            identities.append(row.get("_fallback_key", (
                "exact", row["ts"], row["account"], row["account_id"],
                row["sender"], row["sender_address"], row["subject"], row["unread"])))
        duplicate = any(key in seen for key in identities)
        seen.update(identities)
        if not duplicate:
            result.append(row)
    result.sort(key=lambda r: r["ts"], reverse=True)
    # Mail.app and the SQLite fallback use different native ID namespaces. A
    # producer switch can leave the same message in recent/history with
    # unrelated account/native IDs. Pair only across snapshots and producers,
    # preferring a shared RFC Message-ID. Otherwise require the same visible
    # header and disclose the uncertain match in the digest.
    def fingerprint(row: dict) -> tuple:
        return (row["ts"], row["account"].casefold(),
                row["sender_address"] or row["sender"].casefold(),
                row["subject"])

    def producer(row: dict) -> str:
        return row.get("native_id", "").split(":", 1)[0]

    recent = [row for row in result if row.get("_snapshot") == "recent"]
    matched: set[int] = set()
    paired = []
    for row in result:
        if row.get("_snapshot") == "history":
            row_account = row.get("account_id", "")
            candidates = [(index, other) for index, other in enumerate(recent)
                          if index not in matched
                          and other["account"].casefold() == row["account"].casefold()
                          and row_account and other.get("account_id") == row_account
                          and (producer(other) != producer(row) or
                               not producer(other) or not producer(row))]
            partner = next((index for index, other in candidates
                            if other.get("message_id") and row.get("message_id")
                            and other["message_id"].casefold() == row["message_id"].casefold()), None)
            if partner is None:
                partner = next((index for index, other in candidates
                                if fingerprint(other) == fingerprint(row)
                                and not (other.get("message_id") and row.get("message_id")
                                         and other["message_id"].casefold() != row["message_id"].casefold())), None)
            if partner is not None:
                matched.add(partner)
                recent[partner]["_cross_source_matched"] = True
                continue
            # The same display label can name multiple linked accounts, and
            # Mail.app/SQLite account IDs need not share a namespace. Keep both
            # possible copies when account identity cannot be proved.
            for other in recent:
                if (other["account"].casefold() == row["account"].casefold()
                        and other.get("account_id") != row_account
                        and ((other.get("message_id") and row.get("message_id")
                              and other["message_id"].casefold() == row["message_id"].casefold())
                             or (fingerprint(other) == fingerprint(row)
                                 and not (other.get("message_id") and row.get("message_id")
                                          and other["message_id"].casefold() != row["message_id"].casefold())))):
                    other["_cross_source_uncertain"] = True
                    row["_cross_source_uncertain"] = True
        paired.append(row)
    result = paired
    return result


def header_scan_coverage(rows: list[dict], *, raw_headers: str | None = None,
                         account: str | None = None,
                         fallback_cap: bool = True) -> dict:
    """Native attempted/skipped/cap evidence, before identity deduplication."""
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    native: dict[str, tuple[int, int, bool]] = {}
    incomplete: dict[str, str] = {}
    scoped_keys = None
    if account:
        extra = rows + (_parse_header_records(raw_headers) + _scan_marker_accounts(raw_headers)
                        if raw_headers is not None else [])
        scoped_keys, _ = _resolve_account_groups(account, extra)
    def selected(key: str, label: str) -> bool:
        return (key == "*" or scoped_keys is None or _account_group(label, key) in scoped_keys or
                key == label and _account_group(label, "") in scoped_keys)
    if raw_headers is None:
        entries = [(row.get("account_id") or row.get("account") or "unknown",
                    row.get("account") or "Mail") for row in rows]
    else:
        entries = []
        for line in raw_headers.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("H2\x01"):
                parts = line.split("\x01")
                if len(parts) not in (9, 10) or parts[2] not in ("R", "U"):
                    continue
                try:
                    datetime.fromtimestamp(float(parts[1]))
                except (ValueError, OverflowError, OSError):
                    continue
                entries.append((parts[4] or parts[3] or "unknown", parts[3] or "Mail"))
            elif line.startswith("C2\x01"):
                parts = line.split("\x01")
                if len(parts) != 6 or parts[5] not in ("0", "1"):
                    continue
                try:
                    attempted, skipped = int(parts[3]), int(parts[4])
                except ValueError:
                    continue
                if attempted < 0 or skipped < 0 or skipped > attempted:
                    continue
                key, label = parts[2] or parts[1] or "unknown", parts[1] or "Mail"
                if selected(key, label):
                    normalized = key.casefold()
                    previous = native.get(normalized, (0, 0, False))
                    native[normalized] = (previous[0] + attempted,
                                          previous[1] + skipped,
                                          previous[2] or parts[5] == "1")
                    labels[normalized] = label
            elif line.startswith("C3\x01"):
                parts = line.split("\x01")
                if len(parts) != 4 or parts[3] not in ("failed", "interrupted"):
                    continue
                key, label = parts[2] or parts[1] or "unknown", parts[1] or "Mail"
                if selected(key, label) or (account and not parts[2]):
                    incomplete[key.casefold()] = label
            elif parsed := _parse_pipe_lines(line):
                entries.append((parsed[0][1] or "unknown", parsed[0][1] or "Mail"))
    for key, label in entries:
        normalized = key.casefold()
        if not selected(key, label):
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
        labels[normalized] = label
    keys = set(counts) | set(native)
    cap_accounts = sorted({labels[key] for key in keys
                           if (native[key][2] if key in native else
                               (fallback_cap and counts.get(key, 0) >= _RECENT_HEADER_CAP_PER_ACCOUNT))})
    return {"cap_accounts": cap_accounts,
            "incomplete_accounts": sorted(set(incomplete.values())),
            "skipped": sum(item[1] for item in native.values()),
            "attempted": sum(native[key][0] if key in native else counts.get(key, 0)
                             for key in keys)}


def header_scan_cap_accounts(rows: list[dict], *, raw_headers: str | None = None) -> list[str]:
    """Compatibility view of the native header-scan coverage metadata."""
    return header_scan_coverage(rows, raw_headers=raw_headers)["cap_accounts"]


def _parse_lines() -> list[tuple[float, str, str, str, bool | None]]:
    return _parse_pipe_lines(_headers)


def _parse_history() -> list[tuple[float, str, str, str, bool | None]]:
    return _parse_pipe_lines(_history)


def header_rows(*, since_ts: float | None = None, limit: int | None = None
                ) -> list[dict]:
    """Cached inbox headers as DICTS — the shape-stable view of `_parse_lines`.

    The tuple `_parse_lines` returns is an internal detail of this module and it
    has grown twice: three fields, then four when Mail sync went multi-account,
    then five when read status arrived (see _parse_pipe_lines). Each time, a
    consumer outside this file that unpacked it positionally broke — most
    recently the Daily Summary button, which raised ValueError on every single
    press because service/assistant/brief.py still unpacked four.

    Nothing about those callers actually needed a tuple; they needed a sender
    and a subject. So cross-module consumers take this instead, and the next
    field lands as one more key that old callers simply don't read. Callers
    INSIDE this module keep using the tuples directly — they live next to the
    parser and move with it.

    Newest first, matching `_parse_lines`.
    """
    rows = _parse_header_records(_headers, snapshot="recent")
    if since_ts is not None:
        rows = [r for r in rows if r["ts"] >= since_ts]
    return rows[:limit] if limit else rows


def _unread_rows(rows: list) -> tuple[list, str]:
    """Keep only unread rows. Returns (rows, note) where `note` is a warning to
    surface when read status isn't available yet.

    Wisp could always WRITE read status (mark_email_read) and never READ it —
    the header scan simply didn't collect it, so "what's unread?" was
    structurally unanswerable and the model had nothing to filter on. Now that
    it does, the one remaining hazard is a cache written before the flag
    existed: those rows are `None`, and silently dropping them would report
    "no unread mail" from data that never knew.
    """
    known = [r for r in rows if r[4] is not None]
    if not known:
        return [], ("(Read/unread status isn't in the mail cache yet — it "
                    "arrives on the next Mail sync, usually within 5 minutes. "
                    "Tell the user you can't check unread just yet rather than "
                    "saying they have none.)")
    stale = len(rows) - len(known)
    note = (f"(Note: {stale} older cached emails don't carry read status yet, "
            f"so they're excluded.)" if stale else "")
    return [r for r in known if r[4]], note


def _cache_ready() -> bool:
    """True if the header cache has a row or native scan provenance.

    Stronger than `_headers.strip()`: distinguishes a genuinely-empty/answered
    inbox from a not-yet-synced or partial cache. This is the exact gap behind
    the reported bug — right after launch the header push hadn't landed, so the
    'recent' scan produced zero rows and wrongly reported 'No emails found'
    instead of 'still syncing'. An attempted, skipped, or incomplete native
    scan can explain a rowless cache without asserting an empty inbox.
    """
    coverage = header_scan_coverage([], raw_headers=_headers)
    return (bool(_parse_lines()) or bool(_scan_marker_accounts(_headers)) or
            any(item.get("headers") is not None for item in _external_accounts.values()) or
            coverage["attempted"] > 0 or bool(coverage["incomplete_accounts"]))


def _raw_ready() -> bool:
    """Same idea for the verbatim/raw cache that backs view_emails."""
    _purge_raw_if_expired()
    return bool(_raw_emails.strip()) or any(item.get("raw") is not None for item in _external_accounts.values())


async def _ensure_email_cache(*, want_raw: bool = False,
                              timeout_seconds: float = 2.5) -> None:
    """If the cache isn't ready, ask the Wisp app to sync Mail NOW and wait
    briefly for the push to land — instead of passively waiting on its 5-min
    timer. Fixes the launch-time race where the first email query saw an empty
    cache and answered 'no emails'; a retry seconds later worked once the
    timer's sync landed. Best-effort: if the app isn't connected (no hub
    subscriber) or the push doesn't arrive in time, we fall through and the
    caller's readiness guard reports the sync-aware 'still syncing' message.
    """
    parts = ("headers", "raw") if want_raw else ("headers",)
    try:
        from service import gmail_source
        # The configured account list is authoritative even when it is empty.
        # A restored provider snapshot must not outlive a disconnected account.
        configured = gmail_source.connected_accounts()
        if configured or _external_accounts:
            configure_external_accounts(configured)
        await gmail_source.refresh_if_stale(parts=parts)
    except ImportError:
        if _external_accounts:
            configure_external_accounts({})
    except Exception:  # Optional provider availability must not disable native Mail.
        for address in list(_external_accounts):
            for part in parts:
                external_email_error(address, part, "Google refresh is unavailable; showing the last available cache")
    sources = email_source_details(part="raw" if want_raw else "headers")
    refresh_local = (
        any(not row["fresh"] for row in sources) or
        (not sources and (_email_read_source == "local_index" or bool(_swift_raw if want_raw else _swift_headers))))
    generation = _headers_sync_generation
    raw_generation = _raw_snapshot_generation
    if (_raw_ready() if want_raw else email_sync_state() == "ready") and not refresh_local:
        return
    try:
        from service.assistant.hub import hub
        # The legacy sync_emails_now event also starts MailReader.syncRaw(),
        # which fetches bodies. Digest and Daily readiness request headers only.
        event = ({"type": "sync_emails_now"} if want_raw else
                 {"type": "sync_assistant_sources_now", "sources": ["email"]})
        await hub.publish(event)
    except Exception:  # noqa: BLE001 — a signalling failure must not break the query
        return
    # Poll for the app's push (headers/raw land via /assistant/sync/emails).
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        await asyncio.sleep(0.2)
        if ((_raw_ready() and (not refresh_local or _raw_snapshot_generation > raw_generation)) if want_raw else (
                email_sync_state() == "unavailable" or
                (email_sync_state() == "ready" and
                 (not refresh_local or _headers_sync_generation > generation)))):
            return


async def ensure_current_email_headers(timeout_seconds: float = 8.0) -> str:
    """Request the first live Mail header sync and return its readiness state.

    Daily Summary uses the longer wait because an accurate snapshot is worth a
    few seconds; normal agent mail calls keep the shorter default above.  A
    timeout remains a normal ``syncing`` state, not an empty inbox.
    """
    await _ensure_email_cache(timeout_seconds=timeout_seconds)
    return email_sync_state()


def _day_bounds(day: str) -> tuple[float, float, str]:
    """Resolve "today" / "yesterday" / "YYYY-MM-DD" to a local-time
    [start, end) epoch window, plus a human label for the summary header."""
    today = datetime.now().date()
    if day == "today":
        d = today
    elif day == "yesterday":
        d = today - timedelta(days=1)
    else:
        d = datetime.fromisoformat(day).date()
    start = datetime(d.year, d.month, d.day)
    return start.timestamp(), (start + timedelta(days=1)).timestamp(), d.strftime("%A, %B %-d")


def _fmt_line(account: str, sender: str, subject: str) -> str:
    tag = f"[{account}] " if account else ""
    return f"{tag}{sender} | {subject}"


_ACCOUNT_FILLER_RE = re.compile(r"\b(?:e-?mail|account|inbox|mail)\b", re.I)


def _account_query(account: str) -> str:
    return _ACCOUNT_FILLER_RE.sub("", account).strip().casefold() or account.strip().casefold()


def _account_group(label: str, native_id: str) -> tuple[str, str]:
    for address, item in _external_accounts.items():
        if native_id.casefold() == address or not native_id and label.casefold() == address:
            return ("native", item.get("mail_account_id") or item.get("inferred_native_id") or address)
    return ("native", native_id) if native_id else ("legacy", label.casefold())


def _account_catalog(extra: list[dict]) -> dict:
    # Header queries never inspect history or bodies to discover identities.
    # Raw lookups pass retained native body identities explicitly below.
    groups = {}
    inventory = (_parse_header_records(_headers) + _parse_header_records(_swift_headers) +
                 _scan_marker_accounts(_swift_headers) + extra)
    def add(label, native_id):
        key = _account_group(label, native_id)
        group = groups.setdefault(key, {"ids": set(), "labels": set()})
        if native_id:
            group["ids"].add(native_id.casefold())
        if label:
            group["labels"].add(label.casefold())
    for row in inventory:
        add(str(row.get("account") or ""), str(row.get("account_id") or ""))
    for address, item in _external_accounts.items():
        add(address, address)
        bound = item.get("mail_account_id") or item.get("inferred_native_id")
        if bound:
            add(str(item.get("mail_account_name") or address), str(bound))
    return groups


def _resolve_account_groups(account: str, extra: list[dict] | None = None) -> tuple[set, dict]:
    groups = _account_catalog(extra or [])
    q = _account_query(account)
    exact_id = {key for key, group in groups.items() if account.strip().casefold() in group["ids"]}
    exact_label = {key for key, group in groups.items() if q in group["labels"]}
    partial = {key for key, group in groups.items() if any(q in name for name in group["labels"] | group["ids"])}
    return exact_id or exact_label or partial, groups


def _filter_account(rows: list, account: str | None) -> list:
    if not account:
        return rows
    keys, _ = _resolve_account_groups(account, [{"account": r[1], "account_id": ""} for r in rows])
    return [r for r in rows if _account_group(r[1], "") in keys]


def _filter_account_records(rows: list[dict], account: str | None) -> list[dict]:
    if not account:
        return rows
    keys, _ = _resolve_account_groups(account, rows)
    return [r for r in rows if _account_group(r["account"], r.get("account_id", "")) in keys]


def _raw_account_scope(rows: list[dict], account: str | None) -> tuple[list[dict], str]:
    """Resolve identities before labels; replacement must not erase ambiguity."""
    if not account or not account.strip():
        return rows, ""
    retained = []
    for record in _swift_raw.split(_RAW_RS):
        if record.strip():
            label, native_id = _source_identity(record, raw=True)
            retained.append({"account": label, "account_id": native_id})
    keys, groups = _resolve_account_groups(account, rows + retained)
    if not keys:
        return [], (f"(error: no cached email account matches {account!r}. "
                    "Choose an account from the synced Mail data; no other account was searched.)")
    if len(keys) != 1:
        choices = "; ".join(f"{', '.join(sorted(groups[key]['labels']))} (account ID: {key[1] or 'unavailable'})"
                            for key in sorted(keys))
        return [], (f"(error: several email accounts match {account!r}: {choices}. "
                    "Which one? Use an exact account ID, or rename the accounts in Mail and sync again.)")
    return [row for row in rows if _account_group(str(row.get("account") or ""),
                                                  str(row.get("account_id") or "")) in keys], ""


def _email_body_block(row: dict) -> str:
    """Render verbatim evidence with its account and message identities."""
    when = datetime.fromtimestamp(row["ts"]).strftime("%a %b %-d, %Y %-I:%M %p")
    account = f"Account: {row['account']}\n" if row.get("account") else ""
    native_id = f"Account-ID: {row['account_id']}\n" if row.get("account_id") else ""
    message_id = f"Message-ID: {row['message_id']}\n" if row.get("message_id") else ""
    return (f"{account}{native_id}From: {row['sender']}\nTo: {row['to']}\n"
            f"Subject: {row['subject']}\nDate: {when}\n{message_id}\n{row['body']}")


def _scan_marker_accounts(text: str) -> list[dict]:
    """Linked accounts proved by a native scan even when every header skipped."""
    accounts = []
    for line in text.split("\n"):
        parts = line.rstrip("\r").split("\x01")
        if len(parts) == 4 and parts[0] == "C3" and parts[3] in ("failed", "interrupted"):
            accounts.append({"account": parts[1] or "Mail", "account_id": parts[2]})
            continue
        if len(parts) != 6 or parts[0] != "C2" or parts[5] not in ("0", "1"):
            continue
        try:
            attempted, skipped = int(parts[3]), int(parts[4])
        except ValueError:
            continue
        if attempted < 0 or skipped < 0 or skipped > attempted:
            continue
        accounts.append({"account": parts[1] or "Mail", "account_id": parts[2]})
    return accounts


def _known_accounts() -> list[str]:
    """Every distinct account identifier currently in the cache."""
    seen: list[str] = []
    for r in _parse_lines() + _parse_history():
        a = r[1]
        if a and a not in seen:
            seen.append(a)
    for r in _scan_marker_accounts(_headers) + _scan_marker_accounts(_history):
        a = r["account"]
        if a and a not in seen:
            seen.append(a)
    return seen


def _unknown_account_message(account: str | None) -> str | None:
    """A corrective message when `account` matches NOTHING in the whole cache
    — otherwise None.

    MEASURED FAILURE (2026-08-18). Asked "and on my ucsc email?", the model
    passed account="UCSC email". The real cached identifiers are "Google" and
    "adnjain@ucsc.edu" — filler-word stripping above now resolves that specific
    phrase, but the underlying gap is structural: the model is guessing an
    account label rather than being told the real ones, the same shape as
    _wrong_account_path's invented `/Users/<name>` and
    action_tools._own_address_guard's own-email-as-recipient. Whatever a future
    phrasing doesn't resolve, THIS is what turns "No emails in this week" (which
    reads as a quiet inbox) into an answer that names the real problem.
    Critically, it also protects _empty_range_message's own honesty check: that
    function re-applies _filter_account and would see the same zero rows, so a
    bad account name defeats it too unless caught first, here.
    """
    if not account:
        return None
    if (header_scan_coverage([], raw_headers=_headers, account=account)["incomplete_accounts"] or
            header_scan_coverage([], raw_headers=_history, account=account,
                                 fallback_cap=False)["incomplete_accounts"]):
        return None
    # A global native limit or malformed row can hide an account entirely;
    # absence from the observed rows cannot prove that it is unlinked.
    if any(r["account_id"] == "*" for r in
           _scan_marker_accounts(_headers) + _scan_marker_accounts(_history)):
        return None
    if _filter_account_records(_parse_header_records(_headers) +
                               _parse_header_records(_history) +
                               _scan_marker_accounts(_headers) +
                               _scan_marker_accounts(_history), account):
        return None
    known = _known_accounts()
    if not known:
        return None  # cache genuinely empty — the ordinary "no mail yet" path is honest
    return (f"(error: no linked account matches {account!r}. The linked "
            f"account(s) are: {', '.join(known)}. Call this again with one of "
            f"those exactly, or omit `account` to see mail from all of them.)")


_URGENT_SUBJECT = re.compile(
    r"\b(?:urgent|action required|deadline|due|expires?|fraud|suspicious|"
    r"security alert|payment (?:failed|due)|past due|respond by|reply requested|rsvp)\b", re.I)
_ACTION_SUBJECT_SCORE = re.compile(
    r"\b(?:review|approve|confirm|submit|sign|interview|invitation|invoice|"
    r"payment|security|password|account locked)\b", re.I)


def header_importance(row: dict, *, newest_ts: float | None = None) -> int:
    """Deterministic ranking from headers only; subject text is never a command."""
    subject = row.get("subject", "") or ""
    score = 0
    if _URGENT_SUBJECT.search(subject):
        score += 8
    elif _ACTION_SUBJECT_SCORE.search(subject):
        score += 5
    if row.get("unread") is True:
        score += 2
    if re.search(r"\b(?:work|school|college|university)\b", row.get("account", ""), re.I):
        score += 1
    if newest_ts is not None and row.get("ts", 0) >= newest_ts - 2 * 86400:
        score += 1
    if is_machine_sender(row.get("sender", "")) or _MARKETING_SUBJECT.search(subject):
        score -= 3
    return score


# --- Sectioned inbox digest ----------------------------------------------------
# A flat list grouped by sender buries "office hours cancelled" between two
# promotions. The sectioned layout ranks mail by what the person must DO about it.
# Classification uses headers only (sender, address, subject, unread flag), never
# message bodies, and subject text is only ever a label, never an instruction.
_SOCIAL_RE = re.compile(
    r"facebook|instagram|twitter|linkedin|reddit|discord|tiktok|snapchat|pinterest|youtube|"
    r"whatsapp|nextdoor|tumblr|bluesky|mastodon|\bthreads\b|\bx\.com\b", re.I)
_COURSE_RE = re.compile(
    r"instructure|canvas|edstem|ed discussion|gradescope|piazza|blackboard|moodle|"
    r"brightspace|coursera|\bcourse", re.I)
_NEWSLETTER_RE = re.compile(
    r"substack|newsletter|digest|nytimes|new york times|washington ?post|bloomberg|"
    r"morning brew|medium\.com|\bnews@|updates@|marketing|promo|\boffers?@|\bdeals?@", re.I)
_TIME_CHANGE_RE = re.compile(
    r"\b(?:cancel(?:l?ed)?|resched\w*|postponed|moved to|overdue|final notice|"
    r"expires?|expiring|missing)\b", re.I)
_SCHOOL_ACTION_RE = re.compile(
    r"\b(?:due|deadline|grade[sd]?|exam|midterm|quiz|late|action required|required|reminder|"
    r"assignment|homework|hw ?\d+|pa ?\d+|lab ?\d+|problem set|pset)\b", re.I)
# Words that make a display name an ORGANISATION, not a person. "BYU Independent
# Study" and "Psychology Advising" are not someone waiting on a reply.
_ORG_NAME_RE = re.compile(
    r"\b(?:university|college|school|office|department|dept|team|support|services?|advising|"
    r"announcements?|news|community|security|study|studies|admissions?|center|centre|institute|"
    r"academy|staff|hr|billing|orders?|notifications?|alerts?|updates?|newsletter|digest|"
    r"bank|inc|llc|ltd|corp|company|group|club|association|society|program|programs)\b", re.I)


def _looks_like_person(name: str, address: str) -> bool:
    """A display name that reads like a human's: 1-4 plain words, no organisation
    words, no digits, not a shouted brand. Headers only; deliberately conservative,
    because mistaking a broadcast for a person floods the attention section."""
    name = re.sub(r"\s*<[^>]*>", "", name or "").strip()
    if not name or name.casefold() == (address or "").casefold() or "@" in name:
        return False
    words = name.split()
    if not 1 <= len(words) <= 4 or _ORG_NAME_RE.search(name) or re.search(r"\d", name):
        return False
    if any(len(w) > 3 and w.isupper() for w in words):
        return False                    # NVIDIA, ZIPRECRUITER
    return all(re.fullmatch(r"[A-Za-z\u00C0-\u024F.'\-]+", w) for w in words)
_SECURITY_RE = re.compile(
    r"\b(?:security alert|suspicious|fraud|unauthori[sz]ed|password (?:reset|changed)|"
    r"payment (?:failed|due|declined)|account (?:locked|suspended)|verify your)\b", re.I)
# Explicit payment problems use the same framed-title boundary as account alerts.
# Receipts and generic billing words are not attention signals.
_PAYMENT_PROBLEM_RE = re.compile(r"\bpayment (?:failed|due|declined)\b", re.I)
# Phrases that are about the reader's OWN account (a sign-in, a code, a password),
# narrow enough to trust from a social network too. A social subject often quotes
# other people's posts, so the broad words above ("fraud", "suspicious") are not
# trusted from those senders; these account-access phrases are.
_ACCOUNT_SECURITY_RE = re.compile(
    r"\b(?:(?:new|unusual|suspicious|unrecogni[sz]ed) (?:sign[- ]?in|log[- ]?in|device|activity)|"
    r"(?:sign[- ]?in|log[- ]?in) (?:attempt|alert)|"
    r"(?:signed|logged) in (?:to|from|on) (?:your|a new)|"
    r"someone (?:tried to |may have )?(?:sign(?:ed)?|log(?:ged)?)[- ]?in|"
    r"was this you|did you (?:just )?(?:sign|log)[- ]?in|confirm (?:it['’]?s|it is) you|"
    r"(?:reset|change) your password|"
    r"password (?:(?:was|has been) )?(?:reset|changed)|password change request|"
    r"(?:verification|security|login|log-in|sign[- ]?in|one[- ]time|authentication) (?:code|pin|passcode)|"
    r"pin to verify|verify your (?:account|email|identity|phone|login|sign[- ]?in)|"
    r"confirm your (?:identity|account)|"
    r"account (?:(?:has been|was|is) )?(?:locked|suspended|compromised|disabled|restricted)|"
    r"security (?:alert|warning|notice|check)|"
    r"unauthori[sz]ed (?:access|sign[- ]?in|log[- ]?in|activity|charge|transaction|purchase))\b", re.I)
_QUOTED_SOCIAL_TITLE_RE = re.compile(
    r'''"[^"]*"|“[^“”]*”|(?<!\w)'(?:[^']|(?<=\w)'(?=\w))*'(?!\w)|‘(?:[^‘’]|(?<=\w)’(?=\w))*’(?!\w)''')
_UNPAIRED_SOCIAL_QUOTE_RE = re.compile(r'''["“”‘]|(?<!\w)['’]|['’](?!\w)''')
_SOCIAL_TITLE_PREFIX_RE = re.compile(
    r"\b(?:posted|shared|liked|commented on|replied to|mentioned you in)"
    r"(?:\s+(?:a|an|the|your|this|new))?"
    r"(?:\s+(?:post|comment|thread|discussion|article|video|story|update))?\s*:?\s*$|"
    r"\b(?:new|recommended|trending)\s+(?:post|comment|thread|discussion|article|video|story|update)"
    r"(?:\s+(?:from|by|in|on)\s+[^:\n]{1,60})?\s*:\s*$", re.I)
_SOCIAL_TITLE_SUFFIX_RE = re.compile(
    r"^\s*(?:is\s+)?(?:trending|recommended)(?:\s+(?:in|on|by|from)\b|[.!]?\s*$)", re.I)


def _social_account_security_subject(subject: str) -> str:
    """Exclude explicitly notified, quoted content from an account-alert match.

    Quotes alone are not enough: genuine alerts quote device names too. Preserve
    all text outside a framed title, including any separate account warning.
    This only consumes header text; it neither reads nor interprets mail bodies.
    """
    # An unmatched delimiter can make a later device-name quote look like the
    # end of a post title. Ambiguous headers retain their security wording.
    unquoted = _QUOTED_SOCIAL_TITLE_RE.sub(lambda match: " " * len(match.group()), subject)
    if _UNPAIRED_SOCIAL_QUOTE_RE.search(unquoted):
        return subject
    def title(match: re.Match) -> str:
        # Only nearby framing matters; avoid rescanning an entire long subject
        # for every quoted span. Unknown/distant framing remains conservative.
        if (_SOCIAL_TITLE_PREFIX_RE.search(subject, max(0, match.start() - 160), match.start())
                or _SOCIAL_TITLE_SUFFIX_RE.search(subject[match.end():match.end() + 160])):
            return " " * len(match.group())
        return match.group()
    return _QUOTED_SOCIAL_TITLE_RE.sub(title, subject)


_ACCOUNT_RE = re.compile(
    r"\b(?:receipt|invoice|order|shipped|shipment|delivery|delivered|reservation|itinerary|"
    r"statement|bill|payment|verification|sign[- ]?in|login|two[- ]factor)\b", re.I)

# Section order is the reading order: what needs the person first, noise last.
DIGEST_SECTIONS = (
    ("attention", "\U0001F534 Needs your attention"),
    ("people", "\U0001F464 From people"),
    ("school", "\U0001F393 School & courses"),
    ("accounts", "\U0001F9FE Accounts & receipts"),
    ("social", "\U0001F4AC Social"),
    ("news", "\U0001F4F0 Newsletters & updates"),
)
_FULL_SECTIONS = {"attention": 10, "people": 10, "school": 10, "accounts": 6}


def digest_category(row: dict) -> str:
    """The section a message belongs in, from its headers alone."""
    subject = row.get("subject", "") or ""
    sender = row.get("sender", "") or ""
    address = (row.get("sender_address", "") or "").lower()
    domain = address.rpartition("@")[2]
    identity = f"{sender} {address}"
    machine = is_machine_sender(sender) or is_machine_sender(address)
    social = bool(_SOCIAL_RE.search(domain) or _SOCIAL_RE.search(sender))
    marketing = bool(_MARKETING_SUBJECT.search(subject) or _NEWSLETTER_RE.search(identity))
    course = bool(_COURSE_RE.search(domain) or _COURSE_RE.search(sender))
    school = domain.endswith(".edu") or course
    urgent = bool(_URGENT_SUBJECT.search(subject)) or bool(_TIME_CHANGE_RE.search(subject))
    human = (not machine and not social and not marketing
             and _looks_like_person(sender, address))

    # Security and payment problems matter whoever sent them, and even from a
    # sender that otherwise only sends promotions. A sign-in alert from a social
    # network is about the reader's account, not social activity, so it keeps its
    # subject in the attention section instead of collapsing into the roll-up.
    account_subject = _social_account_security_subject(subject) if social else subject
    if (_ACCOUNT_SECURITY_RE.search(account_subject)
            or _PAYMENT_PROBLEM_RE.search(account_subject)):
        return "attention"
    if _SECURITY_RE.search(subject) and not social:
        return "attention"
    if urgent and not marketing and not social:
        return "attention"
    if course and _SCHOOL_ACTION_RE.search(subject) and not marketing:
        return "attention"
    if human and row.get("unread") is True:
        return "attention"          # a real person is waiting on a read
    if social:
        return "social"
    if marketing:
        return "news"
    if school:
        return "school"
    if human:
        return "people"
    if _ACCOUNT_RE.search(subject):
        return "accounts"
    return "news"


def _short_account(account: str) -> str:
    """'adnjain@ucsc.edu' -> 'ucsc'; 'Google' -> 'Google'."""
    account = _digest_text(account, fallback="Mail")
    if "@" in account:
        host = account.rpartition("@")[2]
        return host.split(".")[0] or account
    return account


def _clip(text: str, limit: int = 110) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def _sectioned_lines(shown_groups: list[list[dict]], newest: float,
                     all_accounts: set[str]) -> tuple[list[str], dict[str, int]]:
    """The digest body: one block per section, plus how many messages each holds.

    Every message is classified and counted BEFORE any display clipping, so the
    headline and section counts cover all represented mail; only the lines shown
    are clipped (three per sender per section, with a "+N more" note)."""
    multi_account = len(all_accounts) > 1
    entries: dict[str, list[dict]] = {key: [] for key, _ in DIGEST_SECTIONS}
    for sender_index, group in enumerate(shown_groups):
        for row in group:
            entries[digest_category(row)].append(
                {"row": row, "sender": sender_index, "more": 0,
                 "rank": header_importance(row, newest_ts=newest)})
    counts = {key: len(value) for key, value in entries.items()}
    # Two different addresses behind one display name must stay distinguishable.
    addresses_by_name: dict[str, set[str]] = {}
    for items in entries.values():
        for item in items:
            row = item["row"]
            if row.get("sender_address"):
                addresses_by_name.setdefault(
                    _digest_text(row.get("sender", ""), fallback="").casefold(), set()
                ).add(row["sender_address"].lower())
    blocks: list[str] = []
    for key, title in DIGEST_SECTIONS:
        items = sorted(entries[key], key=lambda e: (-e["rank"], -e["row"]["ts"]))
        if not items:
            continue
        lines = [f"**{title}** ({len(items)})"]
        if key in _FULL_SECTIONS:
            limit = _FULL_SECTIONS[key]
            # Clip each sender to its three most important lines in this section;
            # the rest are folded into a note on that sender's first line.
            first_line: dict[int, dict] = {}
            lines_for: dict[int, int] = {}
            visible: list[dict] = []
            for item in items:
                sender = item["sender"]
                first = first_line.setdefault(sender, item)
                if lines_for.get(sender, 0) < 3:
                    lines_for[sender] = lines_for.get(sender, 0) + 1
                    visible.append(item)
                else:
                    first["more"] += 1
            shown_lines = visible[:limit]
            for item in shown_lines:
                row = item["row"]
                address = row.get("sender_address", "")
                name = _digest_text(row.get("sender", ""), fallback="")
                if not address:
                    who = f"**{name or 'Unknown sender'} (address unavailable)**"
                else:
                    who = f"**{name or address}**"
                    # The sender's domain stays visible where the person may act on
                    # the message: a look-alike display name is exposed by it. When
                    # one display name fronts several addresses, show them in full.
                    if "@" in address and name.casefold() != address:
                        ambiguous = len(addresses_by_name.get(name.casefold(), ())) > 1
                        who += f" · {address if ambiguous else address.rpartition('@')[2]}"
                mark = "\u25CF " if row.get("unread") is True else ""
                claim = "Subject says: " if (
                _URGENT_SUBJECT.search(row.get("subject", ""))
                or _PAYMENT_PROBLEM_RE.search(row.get("subject", ""))) else ""
                subject = _clip(_subject_text(row.get("subject", "")))
                where = (f" · {_short_account(row['account'])}"
                         if multi_account and row.get("account") else "")
                more = f" (+{item['more']} more from this sender)" if item["more"] else ""
                lines.append(f"- {mark}{who} \u2014 {claim}\u201c{subject}\u201d{where}{more}")
            # Messages behind lines past the limit, counted, not just lines.
            unshown = len(items) - sum(1 + item["more"] for item in shown_lines)
            if unshown:
                lines.append(f"- \u2026and {unshown} more in this group")
        else:
            # Noise sections name who wrote, not what: one line instead of one per email.
            names: dict[object, list] = {}
            for index, item in enumerate(items):
                row = item["row"]
                label = _digest_text(row.get("sender", ""), fallback=row.get("sender_address", "") or "Unknown sender")
                if not row.get("sender_address"):
                    # No address means no proof two of these are the same sender.
                    names[("unknown", index)] = [f"{label} (address unavailable)", 1]
                else:
                    entry = names.setdefault(row["sender_address"].lower(), [label, 0])
                    entry[1] += 1
            lines.append(", ".join(f"{label} ({n})" if n > 1 else label for label, n in names.values()))
        blocks.append("\n".join(lines))
    return blocks, counts


def sender_digest(rows: list[dict], label: str, *, scanned: int | None = None,
                  truncated: int = 0, requested: tuple[float, float] | None = None,
                  max_senders: int = 12,
                  scan_cap_accounts: list[str] | None = None,
                  scan_skipped: int = 0, scan_attempted: int | None = None,
                  scan_incomplete_accounts: list[str] | None = None,
                  history_cap_accounts: list[str] | None = None,
                  history_skipped: int = 0, history_attempted: int = 0,
                  history_incomplete_accounts: list[str] | None = None,
                  layout: str = "sections") -> str:
    """Header-only inbox digest with exact coverage disclosed.

    ``layout="sections"`` (the default) sorts messages by what needs attention.
    ``layout="flat"`` is the older one-bullet-per-sender list; the Daily Summary
    feeds that to a model and keeps it.
    """
    rows = _unique_records(rows)
    if not rows:
        return f"No emails found for {label}."
    newest = max(row["ts"] for row in rows)
    groups: dict[str, list[dict]] = {}
    for index, row in enumerate(rows):
        address = row.get("sender_address", "")
        # A legacy row without an address cannot prove sender identity.
        key = address if address else f"unknown:{index}"
        groups.setdefault(key, []).append(row)
    ordered = sorted(groups.values(), key=lambda group: (
        -max(header_importance(row, newest_ts=newest) for row in group),
        -max(row["ts"] for row in group),
        group[0].get("sender_address", "")))
    all_accounts = {row["account"] for row in rows if row.get("account")}
    date = lambda ts: datetime.fromtimestamp(ts).strftime("%b %-d, %Y %-I:%M %p")
    actual = f"{date(min(r['ts'] for r in rows))} to {date(newest)}"
    window = (f"; requested {date(requested[0])} to {date(requested[1])}"
              if requested else "")
    shown_groups = ordered[:max_senders]
    represented = sum(len(group) for group in shown_groups)
    hidden = len(rows) - represented
    scanned_count = scanned if scanned is not None else len(rows) + truncated
    uncertain = (scan_cap_accounts or scan_skipped or scan_incomplete_accounts or
                 history_cap_accounts or history_skipped or history_incomplete_accounts)
    known = " known" if uncertain else ""
    header_label = "cached header" if uncertain else "header"
    meta = (f"Scanned {scanned_count} {header_label}{'s' if scanned_count != 1 else ''}; "
            f"represented {represented} message{'s' if represented != 1 else ''} from "
            f"{len(shown_groups)} sender note{'s' if len(shown_groups) != 1 else ''}; "
            f"truncated {truncated + hidden}{known} messages "
            f"({truncated} by scan limit, {hidden} by sender note limit). "
            f"Actual dates: {actual}{window}.")
    if scan_skipped:
        meta += (f" Native scan attempted {scan_attempted or scanned_count} headers "
                 f"and skipped {scan_skipped} malformed header{'s' if scan_skipped != 1 else ''} "
                 "with unknown dates; requested-period mail may be missing.")
    if scan_cap_accounts:
        names = ", ".join(_digest_text(name, fallback="Mail") for name in scan_cap_accounts)
        meta += (f" Recent header scan reached its 200-message-per-account cap "
                 f"for {names}; additional messages outside the cache may be "
                 "missing, so total truncation is unknown.")
    if scan_incomplete_accounts:
        names = ", ".join(_digest_text(name, fallback="Mail") for name in scan_incomplete_accounts)
        meta += (f" Recent header scan did not complete for {names}; messages may be "
                 "missing, so total truncation is unknown.")
    if history_skipped:
        meta += (f" History scan attempted {history_attempted} headers and skipped "
                 f"{history_skipped} malformed header{'s' if history_skipped != 1 else ''} "
                 "with unknown dates; requested-period mail may be missing.")
    if history_cap_accounts:
        names = ", ".join(_digest_text(name, fallback="Mail") for name in history_cap_accounts)
        meta += (f" History scan reached its limit for {names}; older messages "
                 "within the requested period may be missing, so total truncation is unknown.")
    if history_incomplete_accounts:
        names = ", ".join(_digest_text(name, fallback="Mail") for name in history_incomplete_accounts)
        meta += (f" History scan did not complete for {names}; requested-period mail "
                 "may be missing, so total truncation is unknown.")
    identity_limited = sum("_fallback_key" in row for row in rows)
    if identity_limited:
        meta += (f" {identity_limited} cached header{'s' if identity_limited != 1 else ''} "
                 "lack stable message identity; indistinguishable messages "
                 "across overlapping scans may be undercounted.")
    if any(row.get("_cross_source_matched") for row in rows):
        meta += (" Headers from different Mail readers were matched by Message-ID "
                 "or visible fields; without Message-ID, indistinguishable "
                 "separate messages may be undercounted.")
    if any(row.get("_cross_source_uncertain") for row in rows):
        meta += (" Reader-switch account IDs differ for otherwise matching "
                 "headers; copies were kept separate, so the represented count "
                 "may include duplicates.")
    if layout == "sections":
        blocks, counts = _sectioned_lines(shown_groups, newest, all_accounts)
        unread = sum(row.get("unread") is True for group in shown_groups for row in group)
        facts = [f"{represented} email{'s' if represented != 1 else ''}"]
        if unread:
            facts.append(f"{unread} unread")
        if counts["attention"]:
            facts.append(f"{counts['attention']} need{'s' if counts['attention'] == 1 else ''} attention")
        footer = ["**Coverage:** " + meta]
        if len(ordered) > max_senders:
            # The headline and sections count only represented mail; say so.
            footer.append(f"{len(ordered) - max_senders} more sender addresses ({hidden} "
                          f"message{'s' if hidden != 1 else ''}) are not in the sections or "
                          "the counts at the top; Coverage counts them as truncated by "
                          "the sender note limit.")
        return (f"\U0001F4EC **Inbox digest \u2014 {label}**\n" + " \u00b7 ".join(facts)
                + "\n\n" + "\n\n".join(blocks) + "\n\n" + "\n".join(footer))
    bullets = []
    for group in shown_groups:
        best = sorted(group, key=lambda r: (-header_importance(r, newest_ts=newest), -r["ts"]))
        first = best[0]
        address = first.get("sender_address", "")
        name = _digest_text(first.get("sender", ""), fallback="Unknown sender")
        sender = f"{name} <{address}>" if address and name.casefold() != address else (address or name + " (address unavailable)")
        accounts = sorted({r["account"] for r in group if r.get("account")})
        unread_count = sum(r.get("unread") is True for r in group)
        unread_note = f", {unread_count} unread" if unread_count else ""
        account_note = (f"; accounts: {', '.join(_digest_text(a, fallback='Mail') for a in accounts)}"
                        if len(all_accounts) > 1 else "")
        subjects = []
        for row in best[:3]:
            subject = _subject_text(row.get("subject", ""))
            claim = "Subject says: " if (
                _URGENT_SUBJECT.search(row.get("subject", ""))
                or _PAYMENT_PROBLEM_RE.search(row.get("subject", ""))) else ""
            subjects.append(f"{claim}“{subject}”")
        more = f"; +{len(group) - 3} more" if len(group) > 3 else ""
        bullets.append(f"- **{sender}** ({len(group)} message{'s' if len(group) != 1 else ''}{unread_note}{account_note}) — "
                       + "; ".join(subjects) + more)
    if len(ordered) > max_senders:
        bullets.append(f"{len(ordered) - max_senders} more sender addresses are included in the counts above.")
    return f"📬 **Inbox digest — {label}**\n{meta}\n" + "\n".join(bullets)


async def _summarize(raw_lines: list[str] | list[dict], header_label: str) -> str:
    """Compatibility entry point for direct digest calls and header records."""
    if not raw_lines:
        return f"No emails found for {header_label}."
    if isinstance(raw_lines[0], dict):
        return sender_digest(raw_lines, header_label)
    rows = []
    for index, line in enumerate(raw_lines):
        entry = _digest_entry(line)
        if entry:
            sender_text = line.split("] ", 1)[1] if line.startswith("[") and "] " in line else line
            rows.append({**entry, "ts": float(index), "account_id": "",
                         "sender_address": _normalized_address(sender_text.partition(" | ")[0]),
                         "message_id": "", "unread": None})
    return sender_digest(rows, header_label)


def _digest_entry(line: str) -> dict | None:
    """Parse a display line without exposing account identifiers in the digest."""
    text = (line or "").strip()
    account = ""
    if text.startswith("[") and "] " in text:
        account, text = text[1:].split("] ", 1)
    sender, separator, subject = text.partition(" | ")
    if not separator:
        return None
    return {"account": account, "sender": sender.strip(), "subject": subject.strip()}


def _digest_text(text: str, *, fallback: str) -> str:
    """Keep display text compact and prevent header text from changing Markdown."""
    text = re.sub(r"\s*<[^>]+>", "", text or "")
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("*", r"\*") if text else fallback


def _subject_text(text: str) -> str:
    """Display the original subject, escaping markup but retaining its words."""
    text = re.sub(r"[\x00-\x1f]+", " ", text or "").strip()
    return text.replace("\\", r"\\").replace("*", r"\*") or "(no subject)"


@_disclose_mail_freshness
async def summarize_inbox_for_day(day: str, account: str | None = None,
                                  unread: bool = False) -> str:
    """day: 'today' | 'yesterday' | 'YYYY-MM-DD'."""
    # No parseable data at all -> the cache isn't ready (still syncing / no
    # permission), which is different from 'no mail on that specific day'. Only
    # the former gets the sync-aware message; an empty day-window with a ready
    # cache still legitimately answers "no emails found for <day>" below.
    if not _cache_ready():
        return _no_inbox_message()
    try:
        start, end, label = _day_bounds(day)
    except ValueError:
        return f"(couldn't understand the date {day!r} — use 'today', 'yesterday', or YYYY-MM-DD)"
    cached = _parse_header_records(_headers, snapshot="recent")
    history = _range_history(start, end)
    candidates = _filter_account_records(cached + history, account)
    scoped = [r for r in candidates if start <= r["ts"] < end]
    rows = [r for r in _unique_records(candidates) if start <= r["ts"] < end]
    if not rows and not unread:
        return _empty_range_message(label, start, end, account)
    coverage = header_scan_coverage(cached, raw_headers=_headers, account=account)
    history_coverage = header_scan_coverage([], raw_headers=_history, account=account,
                                            fallback_cap=False)
    note = ""
    if unread:
        rows, note, empty = _dated_unread_filter(rows, label, start, end, coverage, history_coverage)
        if empty:
            return empty
        label = f"UNREAD email for {label}"
    out = sender_digest(rows, label, scanned=len(scoped),
                         requested=(start, end),
                         scan_cap_accounts=coverage["cap_accounts"],
                         scan_incomplete_accounts=coverage["incomplete_accounts"],
                         scan_skipped=coverage["skipped"],
                         scan_attempted=coverage["attempted"],
                         history_cap_accounts=history_coverage["cap_accounts"],
                         history_incomplete_accounts=history_coverage["incomplete_accounts"],
                         history_skipped=history_coverage["skipped"],
                         history_attempted=history_coverage["attempted"])
    return f"{out}\n\n{note}" if note else out


def _dated_unread_filter(rows: list[dict], label: str, start: float, end: float,
                         coverage: dict, history_coverage: dict) -> tuple[list[dict], str, str]:
    """Keep the date/account intersection and disclose missing read evidence."""
    unknown = sum(row.get("unread") is None for row in rows)
    read = sum(row.get("unread") is False for row in rows)
    selected = [row for row in rows if row.get("unread") is True]
    note = (f"Read-status filter: {read} known read message(s) excluded. "
            f"{unknown} cached message(s) in the requested range lack read status "
            "and are excluded; their unread state is unknown.")
    if selected:
        return selected, note, ""
    window = (f"{datetime.fromtimestamp(start).strftime('%b %-d, %Y %-I:%M %p')} to "
              f"{datetime.fromtimestamp(end).strftime('%b %-d, %Y %-I:%M %p')}")
    result = (f"No known unread messages found in the available cache for {label} "
              f"({window}). Scanned {len(rows)} matching cached headers; represented "
              f"0 unread messages. {note}")
    incomplete = bool(unknown)
    for name, scan in (("Recent", coverage), ("History", history_coverage)):
        if scan["cap_accounts"]:
            incomplete = True
            result += f" {name} scan reached its limit for {', '.join(scan['cap_accounts'])}."
        if scan["incomplete_accounts"]:
            incomplete = True
            result += f" {name} scan did not complete for {', '.join(scan['incomplete_accounts'])}."
        if scan["skipped"]:
            incomplete = True
            result += (f" {name} scan attempted {scan['attempted']} headers and skipped "
                       f"{scan['skipped']} malformed headers with unknown dates/read state.")
    result += (" Total unread coverage for this range is unknown; this does not "
               "establish that no unread mail exists." if incomplete else
               " This result is limited to synced headers in the requested range.")
    return [], note, result


# A wide range can contain thousands of headers. Bound its display input while
# keeping the full matched count for honest coverage reporting.
_MAX_SUMMARY_ROWS = 150


def _sample_for_summary(rows: list) -> tuple[list, int]:
    """Keep strong header signals and sample the rest across the date range."""
    total = len(rows)
    if total <= _MAX_SUMMARY_ROWS:
        return rows, 0
    newest = max(row["ts"] for row in rows)
    ranked = sorted(range(total), key=lambda i: (
        -header_importance(rows[i], newest_ts=newest), -rows[i]["ts"]))
    selected = set(ranked[:_MAX_SUMMARY_ROWS // 2])
    remaining = [i for i in range(total) if i not in selected]
    slots = _MAX_SUMMARY_ROWS - len(selected)
    selected.update(remaining[round(i * (len(remaining) - 1) / (slots - 1))]
                    for i in range(slots))
    return sorted((rows[i] for i in selected), key=lambda row: row["ts"], reverse=True), total


def _empty_range_message(label: str, start: float, end: float,
                         account: str | None = None) -> str:
    """What to say when a date-scoped email query matched nothing.

    "No emails found for this week." is TRUE and it is the wrong answer. Verified
    live 2026-08-10 02:32, from the user's own debug export: asked "is there
    anything needed to be responded to on my emails?" — a question naming no time
    at all — the model reached for `period="this week"`. At 2:32am on a MONDAY
    "this week" is two and a half hours old, so it matched zero emails, and the
    model reported there was nothing to respond to. There were plenty; they had
    simply all arrived before midnight. The user pushed back ("like u mean I have
    no emails this week or nothing important") and the model re-ran the identical
    query and said the same thing.

    So a bare "no emails found" is a dead end that reads as "the inbox is
    empty". This says what the range actually covered, states plainly that the
    inbox itself is not empty, and offers the recent-inbox view. It is written
    entirely as user-facing prose because this pre-synthesized tool can be
    returned directly without another model narration pass.
    """
    if email_freshness_warning(account) or _history_warning(account):
        return (f"I couldn’t verify whether you have email for {label}. No matching messages "
                "were found in the available cache; this does not mean your inbox is empty. "
                "Other dates may have cached emails.")
    rows = _filter_account_records(_parse_header_records(_headers), account)
    coverage = header_scan_coverage(rows, raw_headers=_headers, account=account)
    history_coverage = header_scan_coverage([], raw_headers=_history, account=account,
                                            fallback_cap=False)
    cap_accounts = coverage["cap_accounts"]
    fmt = "%a %b %-d, %-I:%M %p"
    window = (f"{datetime.fromtimestamp(start).strftime(fmt)} to "
              f"{datetime.fromtimestamp(end).strftime(fmt)}")
    if (cap_accounts or coverage["skipped"] or coverage["incomplete_accounts"] or
            history_coverage["cap_accounts"] or history_coverage["skipped"] or
            history_coverage["incomplete_accounts"]):
        names = ", ".join(_digest_text(name, fallback="Mail") for name in cap_accounts)
        cap_note = (f"The recent scan reached its 200-message-per-account cap for {names}; "
                    "messages from the requested period may be outside the cache."
                    if cap_accounts else "")
        skipped_note = (f" Native scan attempted {coverage['attempted']} headers and "
                        f"skipped {coverage['skipped']} malformed headers; requested mail "
                        "may be among them." if coverage["skipped"] else "")
        incomplete_names = ", ".join(_digest_text(name, fallback="Mail")
                                     for name in coverage["incomplete_accounts"])
        incomplete_note = (f" Recent header scan did not complete for {incomplete_names}; "
                           "requested mail may be outside the cache."
                           if coverage["incomplete_accounts"] else "")
        history_names = ", ".join(_digest_text(name, fallback="Mail")
                                  for name in history_coverage["cap_accounts"])
        history_note = (f" History scan reached its limit for {history_names}; "
                        "requested mail may be outside the cache."
                        if history_coverage["cap_accounts"] else "")
        history_skip_note = (f" History scan attempted {history_coverage['attempted']} "
                             f"header{'s' if history_coverage['attempted'] != 1 else ''} "
                             f"and skipped {history_coverage['skipped']} malformed "
                             f"header{'s' if history_coverage['skipped'] != 1 else ''} "
                             "with unknown dates; requested mail may be among them."
                             if history_coverage["skipped"] else "")
        history_incomplete_names = ", ".join(_digest_text(name, fallback="Mail")
                                             for name in history_coverage["incomplete_accounts"])
        history_incomplete_note = (f" History scan did not complete for {history_incomplete_names}; "
                                   "requested mail may be outside the cache."
                                   if history_coverage["incomplete_accounts"] else "")
        total = ("total truncation is unknown" if (cap_accounts or coverage["incomplete_accounts"] or
                                                    history_coverage["cap_accounts"] or
                                                    history_coverage["incomplete_accounts"]) else
                 "truncated 0 known matching messages; skipped headers have unknown dates")
        return (f"No matching headers in the available cache for {label} ({window}). "
                "Scanned 0 matching cached headers; represented 0 messages; "
                f"{total}. {cap_note}{skipped_note}{incomplete_note}"
                f"{history_note}{history_skip_note}{history_incomplete_note}")
    if not rows:
        return (f"No emails in {label} ({window}). Scanned 0 matching headers; "
                "represented 0 messages; truncated 0 messages.")
    rows.sort(key=lambda r: r["ts"], reverse=True)
    newest = datetime.fromtimestamp(rows[0]["ts"]).strftime(fmt)
    # This string can be returned DIRECTLY to the user: summarize_emails is a
    # pre-synthesized tool and the agent deliberately skips a redundant model
    # narration pass when it is the only source.  The old text contained
    # model-facing commands ("THE INBOX IS NOT EMPTY", "Do NOT tell the user",
    # argument names in backticks), which therefore leaked into the response
    # exactly as written in the 2026-08-28 debug export.
    return (f"I don’t see any emails in {label} ({window}). Your inbox itself "
            f"isn’t empty: Wisp has {len(rows)} recent emails cached, with the "
            f"newest from {newest}. Scanned 0 matching headers; represented 0 "
            f"messages; truncated 0 messages. If you want, ask for the recent inbox "
            f"instead.")


@_disclose_mail_freshness
async def summarize_inbox_for_period(period: str, account: str | None = None,
                                     unread: bool = False) -> str:
    """Summarize every email in a spoken time RANGE — "this month", "last week".

    Distinct from summarize_inbox_for_day, which covers one calendar day. The
    range is resolved in Python (see tools/timeranges) rather than by the model.
    """
    if not _cache_ready():
        return _no_inbox_message()
    try:
        start, end, label = resolve_span(period)
    except BadPeriod as e:
        return str(e)
    cached = _parse_header_records(_headers, snapshot="recent")
    # The recent cache only reaches back ~5-7 weeks, so any range older than
    # that needs the deeper ~2-year history scan — merged, not substituted,
    # because a range can straddle the boundary between the two.
    history = _range_history(start, end)
    candidates = _filter_account_records(cached + history, account)
    scoped = [r for r in candidates if start <= r["ts"] < end]
    rows = [r for r in _unique_records(candidates) if start <= r["ts"] < end]
    if not rows and not unread:
        return _empty_range_message(label, start, end, account)
    coverage = header_scan_coverage(cached, raw_headers=_headers, account=account)
    history_coverage = header_scan_coverage([], raw_headers=_history, account=account,
                                            fallback_cap=False)
    note = ""
    if unread:
        rows, note, empty = _dated_unread_filter(rows, label, start, end, coverage, history_coverage)
        if empty:
            return empty
        label = f"UNREAD email for {label}"
    rows, sampled = _sample_for_summary(rows)
    out = sender_digest(rows, label, scanned=len(scoped),
                         truncated=(sampled - len(rows)) if sampled else 0,
                         requested=(start, end),
                         scan_cap_accounts=coverage["cap_accounts"],
                         scan_incomplete_accounts=coverage["incomplete_accounts"],
                         scan_skipped=coverage["skipped"],
                         scan_attempted=coverage["attempted"],
                         history_cap_accounts=history_coverage["cap_accounts"],
                         history_incomplete_accounts=history_coverage["incomplete_accounts"],
                         history_skipped=history_coverage["skipped"],
                         history_attempted=history_coverage["attempted"])
    return f"{out}\n\n{note}" if note else out


@_disclose_mail_freshness
async def summarize_inbox_recent(count: int = 20, account: str | None = None,
                                 unread: bool = False) -> str:
    """No specific day requested — just the N most-recently-scanned messages."""
    # Zero parseable rows here can't mean "you have no email you asked to see
    # recently" — it means the cache isn't populated yet. Report the sync-aware
    # message rather than a confident (and, per the reported bug, wrong) "No
    # emails found for your recent inbox."
    if not _cache_ready():
        return _no_inbox_message()
    # Sort before slicing rather than trusting scan order: with more than one
    # account linked, "the N most recent" has to mean N by date across all of
    # them. MailReader already merges its per-account scans newest-first, but
    # this is the line that actually defines "recent", so it shouldn't depend on
    # the pusher having got the ordering right.
    rows = _filter_account_records(_parse_header_records(_headers), account)
    rows = _unique_records(rows)
    coverage = header_scan_coverage(rows, raw_headers=_headers, account=account)
    label = "your recent inbox"
    note = ""
    if unread:
        known = [r for r in rows if r["unread"] is not None]
        stale = len(rows) - len(known)
        note = (f"{stale} older cached emails lack read status and are excluded."
                if stale else "")
        rows = [r for r in known if r["unread"]]
        label = "your UNREAD email"
        if note and not rows:
            return note
        if not rows:
            if coverage["cap_accounts"] or coverage["skipped"] or coverage["incomplete_accounts"]:
                names = ", ".join(_digest_text(name, fallback="Mail")
                                  for name in coverage["incomplete_accounts"])
                incomplete = (f" Recent header scan did not complete for {names}."
                              if coverage["incomplete_accounts"] else "")
                return ("No unread email found in the available recent cache; "
                        f"total coverage is unknown.{incomplete}")
            return "No unread email found in your recent inbox."
    if not rows:
        coverage = header_scan_coverage([], raw_headers=_headers, account=account)
        if coverage["cap_accounts"] or coverage["skipped"] or coverage["incomplete_accounts"]:
            names = ", ".join(_digest_text(name, fallback="Mail")
                              for name in coverage["incomplete_accounts"])
            incomplete = (f" Recent header scan did not complete for {names}."
                          if coverage["incomplete_accounts"] else "")
            return ("No parseable emails in the available recent header cache. "
                    f"Native scan attempted {coverage['attempted']} headers and skipped "
                    f"{coverage['skipped']} malformed headers; total coverage is unknown."
                    f"{incomplete}")
        return "No emails found in your recent inbox."
    # Disclose the cut instead of implying the slice IS the inbox.
    #
    # Reported 2026-08-16 ("missed some emails"): this returns the newest
    # `count` (default 20) and the summary opens "Here's a quick roundup of your
    # recent inbox" — wording that asserts completeness it doesn't have. The
    # user, who could see more mail than that in Mail.app, read the omission as
    # a sync failure. Same invisible-incompleteness shape the backlog already
    # flags for view_emails; fixed here at the point the rows are actually cut.
    total = len(rows)
    rows = rows[:count]
    out = sender_digest(rows, label, scanned=total, truncated=total - len(rows),
                        scan_cap_accounts=coverage["cap_accounts"],
                        scan_incomplete_accounts=coverage["incomplete_accounts"],
                        scan_skipped=coverage["skipped"],
                        scan_attempted=coverage["attempted"])
    return f"{out}\n\n{note}" if note else out


@register(
    "summarize_emails",
    "Read cached Mail.app headers and make one note per sender email address, "
    "showing counts and original subject lines. Subject urgency is a claim in "
    "the subject, not verified email content. Use for inbox summaries. "
    "ONLY scope it by date when the user actually named a time: `period` for a "
    "RANGE they named ('this month', 'last week'), `day` ('today', "
    "'yesterday', or an ISO date like '2026-07-14') for ONE day they named. "
    "For 'anything I need to reply to?', 'what's new?', 'anything important?' "
    "— which name no time — pass NEITHER and let it return the recent inbox. "
    "Both reach back roughly TWO YEARS. Pass `account` (e.g. the account's name) if the user "
    "asks about a SPECIFIC linked email account and more than one is linked — "
    "omit it otherwise. Header-only and deterministic; never reads bodies.",
    {"type": "object",
     "properties": {
         "period": PERIOD_ARG,
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD' — summarize that whole day's mail"},
         "count": {"type": "integer",
                   "description": "when `period`/`day` are omitted, how many recent messages to scan (default 20)"},
         "unread": {"type": "boolean",
                    "description": "true to cover ONLY known unread email, intersecting "
                                   "any requested day/period. Missing read status is disclosed. "
                                   "Use for 'what's unread' or 'anything I haven't read'"},
         "account": {"type": "string",
                     "description": "only include this linked account (only useful when more than one is linked)"},
     }},
    category="email_read",
)
async def summarize_emails(day: str | None = None, count: int = 20,
                           account: str | None = None,
                           period: str | None = None,
                           unread: bool = False) -> str:
    # Interactive path: if the cache is cold (e.g. just after launch), pull a
    # fresh sync now instead of answering "no emails" and making the user retry.
    await _ensure_email_cache()
    state = email_sync_state()
    if state == "syncing":
        return email_syncing_message()
    if state == "unavailable" and not (_email_available is False and _parse_header_records(_swift_headers)):
        return _no_inbox_message()
    if account and (msg := _unknown_account_message(account)):
        return msg
    # `period` first: it's the more specific request, and a model that supplies
    # both ("this month", day="today") means the range.
    # Unread is an additional filter; it must never discard a named range.
    # Legacy history without read flags is excluded with an explicit warning.
    if period:
        return await summarize_inbox_for_period(period, account, unread=unread)
    if day:
        return await summarize_inbox_for_day(day, account, unread=unread)
    if unread:
        return await summarize_inbox_recent(max(count, 50), account, unread=True)
    return await summarize_inbox_recent(count, account)


def _matches(query: str, sender: str, to: str, subject: str, body: str) -> bool:
    q = query.lower()
    return any(q in f.lower() for f in (sender, to, subject, body))


@_disclose_mail_freshness
async def view_emails_impl(query: str | None = None, day: str | None = None,
                           count: int = 5, account: str | None = None,
                           period: str | None = None,
                           unread: bool = False,
                           strict_match: bool = False) -> str:
    # Interactive path: warm the raw cache on demand if it's cold, rather than
    # dead-ending on "no raw content cached" and forcing a retry.
    await _ensure_email_cache(want_raw=True)
    _purge_raw_if_expired()
    if not _raw_emails.strip() and _external_accounts and all(
            item.get("raw") == "" for item in _external_accounts.values()):
        return "No emails found in the available body snapshot." if not email_freshness_warning(account, part="raw") else (
            "No messages are available in the saved body cache; I couldn’t verify whether you have new email.")
    if _email_available is False and not _raw_emails.strip():
        return _no_inbox_message()
    if not _raw_emails.strip():
        return ("(No raw email content cached right now — either Mail.app hasn't synced "
                "yet, or the cache expired and is waiting on the next body "
                "sync. Try again shortly, or use summarize_emails for a header digest in the "
                "meantime.)")
    rows = _parse_raw()
    rows, account_problem = _raw_account_scope(rows, account)
    if account_problem:
        return account_problem
    label = "your recent inbox"
    # `period` (a range) wins over `day` (one day) when both are supplied — see
    # summarize_emails for why.
    if period:
        try:
            start, end, label = resolve_span(period)
        except BadPeriod as e:
            return str(e)
        rows = [r for r in rows if start <= r["ts"] < end]
    elif day:
        try:
            start, end, label = _day_bounds(day)
        except ValueError:
            return f"(couldn't understand the date {day!r} — use 'today', 'yesterday', or YYYY-MM-DD)"
        rows = [r for r in rows if start <= r["ts"] < end]
    # Scope the query before assessing read-status coverage. Unknown flags in
    # unrelated mail must not inflate the exclusion count for this lookup.
    if query:
        rows = [r for r in rows
                if _matches(query, r["sender"], r["to"], r["subject"], r["body"])]
        if not rows:
            return (f"No emails matching “{query}” were found in the recent "
                    "inbox Wisp searched.")
    note = ""
    if unread:
        # Same rule as summarize_emails: read status only exists for what's
        # cached, and `None` means "this record predates the flag", never
        # "read". Reporting zero unread from records that never knew would be a
        # confident wrong answer.
        known = [r for r in rows if r.get("unread") is not None]
        unknown = len(rows) - len(known)
        if unknown:
            note = (
                f"Read-status coverage: {unknown} "
                f"{'email was' if unknown == 1 else 'emails were'} excluded from "
                "the requested raw-cache scope because read/unread "
                "status is unknown. Unread coverage is incomplete; excluded "
                "records may also be unread.\n\n"
            )
        # An empty date scope does not prove that read flags are unavailable.
        # Retain the bounded raw-cache absence message below in that case.
        had_scoped_rows = bool(rows)
        rows = [r for r in known if r["unread"]]
        label = f"UNREAD in {label}" if label != "your recent inbox" else "your UNREAD email"
        if not rows and unknown:
            return note + "No known unread emails were found in this raw-cache scope."
        if not rows and had_scoped_rows:
            if query:
                return (f"No known unread emails matching “{query}” were found "
                        "in this raw-cache scope.")
            return f"No unread emails in {label.replace('UNREAD in ', '')}."
    rows.sort(key=lambda r: r["ts"])
    if not rows:
        return (f"No emails found for {label} (note: raw content only covers "
                "roughly the 50 most recent emails — try summarize_emails for older mail).")
    # ``count`` is a display limit, not evidence that only that many matched.
    # Keep the newest rows as before, but disclose the cut in user-facing text
    # so neither the model nor the user can mistake a partial raw result for a
    # complete search/list response.
    try:
        limit = max(1, int(count))
    except (TypeError, ValueError):
        limit = 5
    available = len(rows)
    rows = rows[-limit:]
    omitted = available - len(rows)
    coverage = ""
    if omitted:
        kind = "matching emails" if query else "emails in this raw-cache scope"
        coverage = (
            f"Result coverage: returned {len(rows)} of {available} available {kind}; "
            f"{omitted} older {'email was' if omitted == 1 else 'emails were'} omitted "
            f"by count={limit}.\n\n"
        )
    blocks = [_email_body_block(r) for r in rows]
    return note + coverage + "\n\n---\n\n".join(blocks)


@register(
    "view_emails",
    "Get the RAW, verbatim content of the user's emails — full body text, "
    "sender/recipient email addresses, subject line, and exact time — not a "
    "summary. Use this instead of summarize_emails when the user needs a "
    "specific detail FROM an email: an order number, a confirmation code, an "
    "address, a pickup/delivery time, party details, exact wording to quote "
    "back, etc. Pass `query` to search sender/recipient/subject/body for a "
    "keyword (e.g. a person's name, 'order', 'birthday'); pass `period` for a "
    "date RANGE ('this month', 'last week') or `day` for one specific day; "
    "pass `account` if the user asks about a SPECIFIC linked email account and "
    "more than one is linked; omit any/all for the most recent emails. Covers "
    "roughly the 50 most-recently-received emails (shared across the linked "
    "accounts when more than one) — for anything older, fall back to "
    "summarize_emails (which reaches back about two years).",
    {"type": "object",
     "properties": {
         "query": {"type": "string",
                   "description": "keyword to search for in sender, recipient, subject, or body"},
         "period": PERIOD_ARG,
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD' — scope to that day"},
         "unread": {"type": "boolean",
                    "description": "true to return ONLY unread emails"},
         "count": {"type": "integer",
                   "description": "max emails to return in full (default 5 — full bodies are long)"},
         "account": {"type": "string",
                     "description": "only include this linked account (only useful when more than one is linked)"},
         "strict_match": {"type": "boolean",
                          "description": "true for a specific user-requested lookup; return no match instead of unrelated fallback mail"},
     }},
    category="email_read",
)
async def view_emails(query: str | None = None, day: str | None = None, count: int = 5,
                      account: str | None = None, period: str | None = None,
                      unread: bool = False, strict_match: bool = False) -> str:
    return await view_emails_impl(query, day, count, account, period, unread,
                                  strict_match)


async def run_daily_email_summary() -> None:
    """Scheduler entry point (~8am): summarize YESTERDAY — the full day that
    just finished, not "recent N" (which could span days or miss the night's
    mail) — and push it as a notification."""
    if email_sync_state() != "ready":
        return
    summary = await summarize_inbox_for_day("yesterday")
    if summary.startswith("(") or summary.startswith("No emails") or not summary:
        return
    from service.assistant.hub import hub
    await hub.publish({"type": "email_summary", "summary": summary})
