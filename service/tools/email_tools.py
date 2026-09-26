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
    every ~30 min (a scan this deep is too slow to run on the 5-min cadence).
    `summarize_inbox_for_day` falls back to this when a requested day isn't
    covered by `_headers`.
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
import re
import time
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

# Same header format, but the ~2-year-back scan (MailReader.swift's
# historyBatchScript) — a separate cache so its slower cadence can't race/clobber
# the fast recent one above.
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
# Raw bodies carry real PII (addresses, order numbers, party plans) — purge
# after a week rather than let it sit in process memory indefinitely.
# MailReader.swift re-syncs raw content every 15 minutes, so in practice this
# is always fresh; the TTL is a backstop for the case where syncing itself has
# stalled (app not running, FDA revoked, etc.) — it now allows a week of
# staleness before dropping the cache outright, up from the previous 1 day.

# Restore whatever the last run had synced, so a backend restart doesn't leave
# these empty until each source's next (potentially very slow) sync lands —
# see cache_store's module docstring for the profile-build bug this fixes.
# `_at` timestamps are deliberately set to now rather than persisted: they only
# drive the raw cache's staleness TTL, and treating restored content as fresh
# for one more TTL window is far better than treating it as instantly expired.
_headers = cache_store.load("email_headers")
_history = cache_store.load("email_history")
_raw_emails = cache_store.load("email_raw")
if _headers:
    _headers_at = time.time()
if _history:
    _history_at = time.time()
if _raw_emails:
    _raw_emails_at = time.time()

# Managed QA's staged-import contract checks that summaries never construct a
# model client at import time. Sender digests are deterministic, so this stays
# empty; retain the sentinel for that compatibility check.
_client = None


def cache_emails(headers: str) -> None:
    global _headers, _headers_at, _headers_sync_generation
    _headers = headers or ""
    _headers_at = time.time()
    _headers_sync_generation += 1
    cache_store.save("email_headers", _headers)


def cache_history_headers(history: str) -> None:
    global _history, _history_at
    _history = history or ""
    _history_at = time.time()
    cache_store.save("email_history", _history)


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


def cache_identity(emails: list[str]) -> None:
    global _identity_emails
    _identity_emails = [e.strip() for e in (emails or [])
                        if e and e.strip() and not _is_placeholder(e)]
    cache_store.save("identity_emails", "\n".join(_identity_emails))


def get_identity_emails() -> list[str]:
    return list(_identity_emails)


_identity_emails = [e for e in cache_store.load("identity_emails").splitlines()
                    if e and not _is_placeholder(e)]


def set_email_availability(available: bool, reason: str = "",
                           syncing: bool = False, read_source: str = "") -> None:
    """Record whether the Swift MailReader's last sync could read the inbox."""
    global _email_available, _email_reason, _email_sync_pending, _email_read_source
    _email_available = available
    _email_reason = reason
    _email_sync_pending = syncing and available
    _email_read_source = read_source if read_source in {"local_index", "mail_app"} else ""


def _no_inbox_message() -> str:
    """The right 'no data' message for the CURRENT sync state — only blames
    permission when the sync actually failed."""
    if _email_available is False:
        reason = _email_reason or "Mail Automation access isn't granted"
        return (f"(Can't read Mail: {reason}. Grant Wisp access in System Settings ▸ "
                "Privacy & Security ▸ Automation ▸ Wisp ▸ Mail, then try again.)")
    if _email_available is True:
        # Sync worked, there's just nothing in the requested window.
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
    if _email_available is False:
        return "unavailable"
    if _email_sync_pending:
        return "syncing"
    if _headers_sync_generation > 0:
        return "ready"
    return "syncing"


def email_freshness_warning() -> str:
    """Read completion is not proof that Mail fetched everything from a server."""
    if email_sync_state() == "ready" and _email_read_source == "local_index":
        return ("Email is from Mail’s local cache. Newer messages may be missing; "
                "open Mail and let it refresh, then try again.")
    return ""


def with_email_freshness_note(text: str, warning: str) -> str:
    """Attach the limitation outside model output so it cannot be omitted."""
    return f"{warning}\n\n{text}" if warning and warning not in text else text


def _disclose_mail_freshness(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        # Capture BEFORE generation: a newer sync arriving during a model call
        # must not silently remove the caveat for the older input snapshot.
        warning = email_freshness_warning()
        return with_email_freshness_note(await function(*args, **kwargs), warning)
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


def cache_raw_emails(raw: str, coverage: dict | None = None) -> None:
    global _raw_emails, _raw_emails_at, _raw_reference_scan
    if coverage and coverage.get("failed_accounts") and not coverage.get("accounts"):
        _raw_reference_scan = {**coverage, "synced_at": time.time(), "complete": False}
        return  # failed scan is not evidence that old display data disappeared
    _raw_emails = raw or ""
    _raw_emails_at = time.time()
    # A skipped record may be another match. A successful native scan cannot
    # establish uniqueness when its wire representation was not fully decoded.
    record_count = sum(bool(record.strip()) for record in _raw_emails.split(_RAW_RS))
    _raw_reference_scan = {**(coverage or {}), "synced_at": _raw_emails_at,
                           "decode_complete": record_count == len(_parse_raw())}
    cache_store.save("email_raw", _raw_emails)


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


def _purge_raw_if_expired() -> None:
    """Drop the raw cache once it's more than a week old (see _RAW_TTL_SECONDS).
    Checked lazily on read rather than via a background task: same effect, no
    extra scheduler needed."""
    global _raw_emails, _raw_emails_at
    if _raw_emails_at and (time.time() - _raw_emails_at) > _RAW_TTL_SECONDS:
        _raw_emails = ""
        _raw_emails_at = 0.0


def _parse_raw() -> list[dict]:
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
    for record in _raw_emails.split(_RAW_RS):
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


def _parse_header_records(text: str) -> list[dict]:
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
                      "subject": subject,
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
                      "subject": subject, "unread": unread}
        identity = ((record["account_id"] or record["account"]).casefold(),
                    record["message_id"].casefold() or record["native_id"].casefold())
        if all(identity):
            key = ("id", *identity)
        elif line.startswith("H2\x01"):
            # Identical no-ID headers can belong to distinct messages received
            # in the same second. Preserve their multiplicity within a scan;
            # matching occurrence numbers let recent/history overlap collapse.
            occurrence = no_id_occurrences.get(line, 0)
            no_id_occurrences[line] = occurrence + 1
            key = ("no-id-h2", line, occurrence)
            record["_fallback_key"] = key
        else:
            key = ("exact", line)
        if key not in seen:
            seen.add(key)
            records.append(record)
    return sorted(records, key=lambda row: row["ts"], reverse=True)


def _unique_records(rows: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result: list[dict] = []
    for row in sorted(rows, key=lambda r: r["ts"], reverse=True):
        identity = ((row["account_id"] or row["account"]).casefold(),
                    (row.get("message_id", "") or row.get("native_id", "")).casefold())
        key = ("id", *identity) if all(identity) else row.get("_fallback_key", (
            "exact", row["ts"], row["account"], row["account_id"],
            row["sender"], row["sender_address"], row["subject"], row["unread"]))
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def header_scan_cap_accounts(rows: list[dict]) -> list[str]:
    """Accounts whose cached recent scan may omit messages beyond its cap."""
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for row in rows:
        key = row.get("account_id") or row.get("account") or "unknown"
        counts[key] = counts.get(key, 0) + 1
        labels[key] = row.get("account") or "Mail"
    return sorted({labels[key] for key, count in counts.items()
                   if count >= _RECENT_HEADER_CAP_PER_ACCOUNT})


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
    rows = _parse_header_records(_headers)
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
    """True if the header cache holds at least one PARSEABLE email row.

    Stronger than `_headers.strip()`: distinguishes a genuinely-empty/answered
    inbox from a not-yet-synced or partial cache. This is the exact gap behind
    the reported bug — right after launch the header push hadn't landed, so the
    'recent' scan produced zero rows and wrongly reported 'No emails found'
    instead of 'still syncing'. A populated, cleanly-synced inbox always yields
    at least one parseable row, so zero rows means 'not ready', not 'empty'.
    """
    return bool(_parse_lines())


def _raw_ready() -> bool:
    """Same idea for the verbatim/raw cache that backs view_emails."""
    _purge_raw_if_expired()
    return bool(_raw_emails.strip())


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
    refresh_local = not want_raw and bool(email_freshness_warning())
    generation = _headers_sync_generation
    if _raw_ready() if want_raw else email_sync_state() == "ready" and not refresh_local:
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
        if (_raw_ready() if want_raw else (
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


def _filter_account(rows: list, account: str | None) -> list:
    if not account:
        return rows
    # Strip generic words ("email", "account"...) before matching, so "UCSC
    # email" reaches the "ucsc" that actually appears in the cached identifier
    # rather than failing to match the whole phrase. See _unknown_account_message
    # for why this alone isn't sufficient.
    q = _ACCOUNT_FILLER_RE.sub("", account).strip().lower() or account.strip().lower()
    return [r for r in rows if q in r[1].lower()]


def _filter_account_records(rows: list[dict], account: str | None) -> list[dict]:
    if not account:
        return rows
    q = _ACCOUNT_FILLER_RE.sub("", account).strip().casefold() or account.strip().casefold()
    return [r for r in rows if q in r["account"].casefold() or
            q in r.get("account_id", "").casefold()]


def _known_accounts() -> list[str]:
    """Every distinct account identifier currently in the cache."""
    seen: list[str] = []
    for r in _parse_lines() + _parse_history():
        a = r[1]
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
    if _filter_account_records(_parse_header_records(_headers) +
                               _parse_header_records(_history), account):
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


def sender_digest(rows: list[dict], label: str, *, scanned: int | None = None,
                  truncated: int = 0, requested: tuple[float, float] | None = None,
                  max_senders: int = 12,
                  scan_cap_accounts: list[str] | None = None) -> str:
    """One note per normalized address, with exact header coverage disclosed."""
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
    known = " known" if scan_cap_accounts else ""
    header_label = "cached header" if scan_cap_accounts else "header"
    meta = (f"Scanned {scanned_count} {header_label}{'s' if scanned_count != 1 else ''}; "
            f"represented {represented} message{'s' if represented != 1 else ''} from "
            f"{len(shown_groups)} sender note{'s' if len(shown_groups) != 1 else ''}; "
            f"truncated {truncated + hidden}{known} messages ({truncated} by scan limit, "
            f"{hidden} by sender note limit). Actual dates: {actual}{window}.")
    if scan_cap_accounts:
        names = ", ".join(_digest_text(name, fallback="Mail") for name in scan_cap_accounts)
        meta += (f" Recent header scan reached its 200-message-per-account cap "
                 f"for {names}; additional messages outside the cache may be "
                 "missing, so total truncation is unknown.")
    identity_limited = sum("_fallback_key" in row for row in rows)
    if identity_limited:
        meta += (f" {identity_limited} cached header{'s' if identity_limited != 1 else ''} "
                 "lack stable message identity; indistinguishable messages "
                 "across overlapping scans may be undercounted.")
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
            claim = "Subject says: " if _URGENT_SUBJECT.search(row.get("subject", "")) else ""
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
async def summarize_inbox_for_day(day: str, account: str | None = None) -> str:
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
    cached = _parse_header_records(_headers)
    recent = [r for r in cached if start <= r["ts"] < end]
    history = [r for r in _parse_header_records(_history) if start <= r["ts"] < end]
    scoped = _filter_account_records(recent + history, account)
    rows = _unique_records(scoped)
    if not rows:
        return _empty_range_message(label, start, end, account)
    return sender_digest(rows, label, scanned=len(scoped),
                         requested=(start, end),
                         scan_cap_accounts=header_scan_cap_accounts(
                             _filter_account_records(cached, account)))


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
    rows = _filter_account_records(_parse_header_records(_headers), account)
    fmt = "%a %b %-d, %-I:%M %p"
    window = (f"{datetime.fromtimestamp(start).strftime(fmt)} to "
              f"{datetime.fromtimestamp(end).strftime(fmt)}")
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
async def summarize_inbox_for_period(period: str, account: str | None = None) -> str:
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
    cached = _parse_header_records(_headers)
    recent = [r for r in cached if start <= r["ts"] < end]
    # The recent cache only reaches back ~5-7 weeks, so any range older than
    # that needs the deeper ~2-year history scan — merged, not substituted,
    # because a range can straddle the boundary between the two.
    history = [r for r in _parse_header_records(_history) if start <= r["ts"] < end]
    scoped = _filter_account_records(recent + history, account)
    rows = _unique_records(scoped)
    if not rows:
        return _empty_range_message(label, start, end, account)
    rows, sampled = _sample_for_summary(rows)
    return sender_digest(rows, label, scanned=len(scoped),
                         truncated=(sampled - len(rows)) if sampled else 0,
                         requested=(start, end),
                         scan_cap_accounts=header_scan_cap_accounts(
                             _filter_account_records(cached, account)))


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
    scan_cap_accounts = header_scan_cap_accounts(rows)
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
            return "No unread email found in your recent inbox."
    if not rows:
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
                        scan_cap_accounts=scan_cap_accounts)
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
                    "description": "true to cover ONLY unread email — use for 'what's unread' "
                                   "or 'anything I haven't read'"},
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
    if state == "unavailable":
        return _no_inbox_message()
    if account and (msg := _unknown_account_message(account)):
        return msg
    # `period` first: it's the more specific request, and a model that supplies
    # both ("this month", day="today") means the range.
    # `unread` is a filter on the RECENT inbox, not a date scope — read status
    # only exists for what's currently cached, and "unread from last March" is
    # not a question the data can answer. A model that passes both gets the
    # unread view, which is what it actually asked for.
    if unread:
        return await summarize_inbox_recent(max(count, 50), account, unread=True)
    if period:
        return await summarize_inbox_for_period(period, account)
    if day:
        return await summarize_inbox_for_day(day, account)
    return await summarize_inbox_recent(count, account)


def _matches(query: str, sender: str, to: str, subject: str, body: str) -> bool:
    q = query.lower()
    return any(q in f.lower() for f in (sender, to, subject, body))


async def view_emails_impl(query: str | None = None, day: str | None = None,
                           count: int = 5, account: str | None = None,
                           period: str | None = None,
                           unread: bool = False,
                           strict_match: bool = False) -> str:
    # Interactive path: warm the raw cache on demand if it's cold, rather than
    # dead-ending on "no raw content cached" and forcing a retry.
    await _ensure_email_cache(want_raw=True)
    _purge_raw_if_expired()
    if not _raw_emails.strip():
        return ("(No raw email content cached right now — either Mail.app hasn't synced "
                "yet, or the cache expired after a day and is waiting on the next daily "
                "sync. Try again shortly, or use summarize_emails for a digest in the "
                "meantime.)")
    rows = _parse_raw()
    if account:
        q = account.lower()
        rows = [r for r in rows if q in r["account"].lower()]
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
    note = ""
    if unread:
        # Same rule as summarize_emails: read status only exists for what's
        # cached, and `None` means "this record predates the flag", never
        # "read". Reporting zero unread from records that never knew would be a
        # confident wrong answer.
        known = [r for r in rows if r.get("unread") is not None]
        if not known:
            return ("(Read/unread status isn't in the raw email cache yet — it "
                    "arrives on the next Mail sync, usually within 15 minutes. "
                    "Tell the user you can't check unread just yet rather than "
                    "saying they have none.)")
        rows = [r for r in known if r["unread"]]
        label = f"UNREAD in {label}" if label != "your recent inbox" else "your UNREAD email"
        if not rows:
            return f"No unread emails in {label.replace('UNREAD in ', '')}."
    rows.sort(key=lambda r: r["ts"])
    scoped = rows
    if query:
        matched = [r for r in scoped if _matches(query, r["sender"], r["to"], r["subject"], r["body"])]
        if matched:
            rows = matched
        else:
            return (f"No emails matching “{query}” were found in the recent "
                    "inbox Wisp searched.")
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
    blocks = []
    for r in rows:
        when = datetime.fromtimestamp(r["ts"]).strftime("%a %b %-d, %Y %-I:%M %p")
        acct = f"Account: {r['account']}\n" if r["account"] else ""
        # message_id is emitted so reply_to_email/mark_email_read/archive_email
        # have something to act on — it's the only stable handle back to this
        # exact message (see MailReader.rawScript). Omitted rather than shown
        # empty for pre-upgrade cached records, so the model doesn't try to
        # pass a blank id.
        mid = f"Message-ID: {r['message_id']}\n" if r.get("message_id") else ""
        blocks.append(f"{acct}From: {r['sender']}\nTo: {r['to']}\nSubject: {r['subject']}\n"
                      f"Date: {when}\n{mid}\n{r['body']}")
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
    if account and (msg := _unknown_account_message(account)):
        return msg
    return await view_emails_impl(query, day, count, account, period, unread,
                                  strict_match)


async def run_daily_email_summary() -> None:
    """Scheduler entry point (~8am): summarize YESTERDAY — the full day that
    just finished, not "recent N" (which could span days or miss the night's
    mail) — and push it as a notification."""
    summary = await summarize_inbox_for_day("yesterday")
    if summary.startswith("(") or summary.startswith("No emails") or not summary:
        return
    from service.assistant.hub import hub
    await hub.publish({"type": "email_summary", "summary": summary})
