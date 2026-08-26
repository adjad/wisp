"""Email summary via the resident model.

The inbox is READ by the Swift app (Mail.app AppleScript, clean Wisp.app identity
+ Automation grant) and pushed to /assistant/sync/emails — same split as the
calendar, because the Python backend can't get Automation access to Mail. Here we
just cache the pushed headers and summarize them with the fast summarizer model, so
the digest stays cheap and never ties up the big model.
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

from service.config import no_thinking_kwargs, role_to_model
from service.tools.timeranges import PERIOD_ARG, BadPeriod, resolve_span
from service.inference.omlx_client import OMLXClient
from service.tools import cache_store
from service.tools.registry import register


# Output ceiling for both summarizers (imessage_tools imports this).
# Measured, not guessed — see the block at the chat() call below.
_SUMMARY_MAX_TOKENS = 2500


_SYS = (
    "You are Wisp, the user's warm, caring personal assistant giving them a "
    "genuinely helpful rundown of their inbox — the way a thoughtful friend who "
    "actually read everything would, not a terse machine report.\n"
    "\n"
    "FORMAT it to be pleasant and easy to scan — NOT a wall of text:\n"
    "- Open with a short, warm one-line lead-in (a fitting emoji is welcome, e.g. "
    "📬).\n"
    "- Group related emails into a few themed sections, each with a bold header "
    "that STARTS with a relevant emoji — e.g. '**🚨 Needs your attention**', "
    "'**💼 Job leads**', '**🎓 Campus**', '**🛍️ Promos**'.\n"
    "- When a section has several distinct emails, use a SHORT BULLET LIST ('- '), "
    "one line each, with the sender or the key thing **bolded**, then a phrase of "
    "real context — don't cram them into one dense paragraph. A single item can be "
    "a sentence or two instead.\n"
    "- Clearly flag anything that needs a reply, a decision, or has a deadline.\n"
    "- Close with a brief, caring offer to help (e.g. draft a reply, pull the full "
    "text).\n"
    "\n"
    "VOICE: warm, human, and caring, with varied sentence rhythm (mix short lines "
    "with a longer one) and a few tasteful emojis where they add warmth — not on "
    "every line. Lead with whatever is urgent or time-sensitive; skip pure "
    "newsletters and promotions unless genuinely notable. Do NOT just restate "
    "senders and subject lines verbatim — synthesize what's actually going on. Be "
    "specific with the real detail that's there (names, amounts, dates, asks), but "
    "stay completely grounded in what the emails actually say — never invent "
    "details, names, dates, or numbers, and don't pad with filler."
)

# Latest inbox headers pushed by the Swift app — "epochSecs | R/U | account |
# sender | subject" per line (MailReader.swift), where R/U is Mail's read
# status; lines from older builds omit that field and still parse (see
# _parse_pipe_lines). Merged newest-first across every linked
# account. Recent-only (~200 per account) — see module docstring. Readers that
# care about ordering sort anyway; the merge is what makes that sort meaningful
# rather than something that has to be trusted.
_headers: str = ""
_headers_at: float = 0.0

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
# Raw bodies carry real PII (addresses, order numbers, party plans) — purge
# after a week rather than let it sit in process memory indefinitely.
# MailReader.swift re-syncs raw content every 15 minutes, so in practice this
# is always fresh; the TTL is a backstop for the case where syncing itself has
# stalled (app not running, FDA revoked, etc.) — it now allows a week of
# staleness before dropping the cache outright, up from the previous 1 day.

_client: OMLXClient | None = None

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


def _c() -> OMLXClient:
    global _client
    if _client is None:
        _client = OMLXClient()
    return _client


def cache_emails(headers: str) -> None:
    global _headers, _headers_at
    _headers = headers or ""
    _headers_at = time.time()
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


def set_email_availability(available: bool, reason: str = "") -> None:
    """Record whether the Swift MailReader's last sync could read the inbox."""
    global _email_available, _email_reason
    _email_available = available
    _email_reason = reason


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


def cache_raw_emails(raw: str) -> None:
    global _raw_emails, _raw_emails_at
    _raw_emails = raw or ""
    _raw_emails_at = time.time()
    cache_store.save("email_raw", _raw_emails)


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

    Accepts BOTH the 7-field records the current MailReader pushes and the
    6-field ones without message_id that a previous version wrote. The caches
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
        if len(parts) == 8 and parts[1] in ("R", "U"):
            unread = parts[1] == "U"
            parts = [parts[0]] + parts[2:]
        if len(parts) == 7:
            ts_s, account, sender, to, subject, msg_id, body = parts
        elif len(parts) == 6:
            ts_s, account, sender, to, subject, body = parts
            msg_id = ""
        else:
            continue
        try:
            ts = float(ts_s)
        except ValueError:
            continue
        out.append({"ts": ts, "account": account.strip(), "sender": sender.strip(),
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
    for line in text.strip().splitlines():
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
        except ValueError:
            continue
        out.append((ts, parts[1], parts[2], parts[3], unread))
    return out


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
    rows = [{"ts": r[0], "account": r[1], "sender": r[2], "subject": r[3],
             "unread": r[4] if len(r) > 4 else None}
            for r in _parse_lines()]
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


async def _ensure_email_cache(*, want_raw: bool = False) -> None:
    """If the cache isn't ready, ask the Wisp app to sync Mail NOW and wait
    briefly for the push to land — instead of passively waiting on its 5-min
    timer. Fixes the launch-time race where the first email query saw an empty
    cache and answered 'no emails'; a retry seconds later worked once the
    timer's sync landed. Best-effort: if the app isn't connected (no hub
    subscriber) or the push doesn't arrive in time, we fall through and the
    caller's readiness guard reports the sync-aware 'still syncing' message.
    """
    if _raw_ready() if want_raw else _cache_ready():
        return
    try:
        from service.assistant.hub import hub
        await hub.publish({"type": "sync_emails_now"})
    except Exception:  # noqa: BLE001 — a signalling failure must not break the query
        return
    # Poll for the app's push (headers/raw land via /assistant/sync/emails).
    deadline = time.time() + 2.5
    while time.time() < deadline:
        await asyncio.sleep(0.2)
        if _raw_ready() if want_raw else _cache_ready():
            return


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
    if _filter_account(_parse_lines() + _parse_history(), account):
        return None
    known = _known_accounts()
    if not known:
        return None  # cache genuinely empty — the ordinary "no mail yet" path is honest
    return (f"(error: no linked account matches {account!r}. The linked "
            f"account(s) are: {', '.join(known)}. Call this again with one of "
            f"those exactly, or omit `account` to see mail from all of them.)")


async def _summarize(raw_lines: list[str], header_label: str) -> str:
    if not raw_lines:
        return f"No emails found for {header_label}."
    c = _c()
    model = role_to_model("fast")  # the summarizer — cheap, and the user asked for it
    await c.ensure_only(model)
    today_str = datetime.now().strftime("%A, %B %-d, %Y")
    # Identity block: a digest written in the second person has to know whose
    # inbox this is before it decides what "your" refers to. No
    # message-attribution rules here — inbox lines are `sender | subject`, with
    # none of the group-chat ambiguity those rules exist to resolve.
    from service.memory.identity import identity_prompt_block
    identity = identity_prompt_block(messages=False).strip()
    # The exact raw lines going into the synthesis call below — separate from
    # the synthesized text this function returns, so a debug-mode export can
    # show what the model actually read (the real emails) rather than just
    # what it said about them. See service/debug_capture.py.
    from service import debug_capture
    debug_capture.record("source", label=f"emails — {header_label}",
                         text="\n".join(raw_lines))
    messages = [
        {"role": "system", "content": f"{identity}\n\n{_SYS}\nToday is {today_str}; resolve any "
                                       "relative dates (due 'Friday', 'tomorrow') against it. "
                                       "A '[account name]' prefix (when present) names which "
                                       "linked email account a message is from — mention the "
                                       "account only if the user is asking about a specific one "
                                       "or more than one appears, otherwise ignore it."},
        {"role": "user", "content": f"Inbox for {header_label} ([account] sender | subject):\n"
                                    + "\n".join(raw_lines)},
    ]
    resp = await c.chat(
        model,
        messages,
        # 400 was a hard ceiling that would truncate a more detailed summary
        # mid-sentence regardless of what _SYS asked for — raised alongside
        # loosening _SYS's terseness constraint so there's actually room for
        # the extra context to land. Sampling is deliberately NOT specified:
        # a hardcoded temperature here overrode the model's own oMLX profile,
        # which is where it should be tuned.
        #
        # 4000 -> 2500 (2026-08-09), after MEASURING instead of reasoning about
        # it. The 800 -> 4000 raise above was correct for its time and its
        # premise is now dead: it sized the budget to share with an always-on
        # <think> block on LFM2.5, and every text role now runs
        # Agents-A1-4B-oQe6, which IS in no_thinking_capable — so the
        # `**no_thinking_kwargs(model)` on this very line means zero reasoning
        # tokens are generated and the whole budget is summary text.
        #
        # Measured on this machine's live caches, all finish_reason="stop",
        # none truncated:
        #     summarize_emails   recent        458 completion tokens
        #     summarize_emails   this month    629
        #     summarize_messages recent        175
        #     summarize_messages this month    515
        #     summarize_messages last month    767
        # Re-measured after the change, same prompts at 2500: the widest run
        # came back at 1,003 tokens (still finish_reason="stop"). Same input,
        # 767 -> 1,003 purely from sampling variance — which is exactly why
        # this is NOT lowered to the ~1,500 a first pass suggested. 1,500 would
        # have been 1.5x the observed worst case; 2,500 is ~2.5x. A
        # finish_reason="length" here is a QUALITY bug, not a slow one (the
        # user loses the summary and gets bare `sender | subject` lines), so
        # the headroom is deliberately generous.
        #
        # Why it is worth changing at all: oMLX's prefill guard admits a request
        # against prompt + max_tokens, so this is reserved KV whether or not it
        # is used — and it is reserved CONCURRENTLY with the agent
        # conversation's own cache, which is exactly the overlap that pushes the
        # process at the 8.5GB ceiling. Zero latency effect: nothing generates
        # near the ceiling, so the ceiling is never reached.
        max_tokens=_SUMMARY_MAX_TOKENS, **no_thinking_kwargs(model))
    debug_capture.record("model_call", model=model,
                         request={"messages": messages,
                                  "max_tokens": _SUMMARY_MAX_TOKENS,
                                  **no_thinking_kwargs(model)},
                         response=resp)
    text = (resp["choices"][0]["message"].get("content") or "").strip()
    return text or "\n".join(raw_lines)


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
    rows = [r for r in _parse_lines() if start <= r[0] < end]
    if not rows:
        # The fast recent cache (~200 msgs, ~5-7 weeks of typical traffic)
        # doesn't reach this far back — fall back to the ~2-year history scan
        # (headers only, same format, just a deeper/slower-refreshed scan) so
        # older days still work instead of dead-ending on "no emails found".
        rows = [r for r in _parse_history() if start <= r[0] < end]
    rows = _filter_account(rows, account)
    rows.sort(key=lambda r: r[0])
    if not rows:
        # Same reasoning as the range path — see _empty_range_message. This used
        # to hand an EMPTY line list to the summarizer, which then wrote prose
        # about nothing.
        return _empty_range_message(label, start, end, account)
    lines = [_fmt_line(a, s, subj) for _, a, s, subj, _u in rows]
    return await _summarize(lines, label)


# Most rows one summary call may be handed — see imessage_tools for the full
# reasoning. The per-day and per-count paths are implicitly bounded (one day, or
# `count`); a RANGE is the first that isn't, and an unbounded month of mail
# overflows the context window and returns a bare 400.
_MAX_SUMMARY_ROWS = 150


def _sample_for_summary(rows: list) -> tuple[list, int]:
    """Bound `rows` for a summary call, sampling EVENLY across the range rather
    than keeping the newest — a range answers "what happened over this period",
    so dropping the start of it invites "nothing happened in July". Returns
    (rows, original_count), with original_count 0 when nothing was dropped."""
    total = len(rows)
    if total <= _MAX_SUMMARY_ROWS:
        return rows, 0
    step = total / _MAX_SUMMARY_ROWS
    return [rows[int(i * step)] for i in range(_MAX_SUMMARY_ROWS)], total


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

    So a bare "no emails found" is a dead end that reads to the model as "the
    inbox is empty". This says what the range actually covered, states plainly
    that the inbox is NOT empty, and names the way out — which turns a wrong
    final answer into a recoverable step.
    """
    rows = _filter_account(_parse_lines(), account)
    fmt = "%a %b %-d, %-I:%M %p"
    window = (f"{datetime.fromtimestamp(start).strftime(fmt)} to "
              f"{datetime.fromtimestamp(end).strftime(fmt)}")
    if not rows:
        return f"No emails in {label} ({window})."
    rows.sort(key=lambda r: r[0], reverse=True)
    newest = datetime.fromtimestamp(rows[0][0]).strftime(fmt)
    return (f"No emails in {label} — that range covers only {window}, and "
            f"nothing arrived inside it.\n\n"
            f"THE INBOX IS NOT EMPTY: it holds {len(rows)} recent emails, the "
            f"newest from {newest}. Do NOT tell the user they have no email or "
            f"nothing to respond to — that would be wrong. If they did not "
            f"actually ask about {label}, call this tool again with NO `period` "
            f"and NO `day` to see the recent inbox; if they did, say that "
            f"specific range was quiet and offer the recent inbox instead.")


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
    rows = [r for r in _parse_lines() if start <= r[0] < end]
    # The recent cache only reaches back ~5-7 weeks, so any range older than
    # that needs the deeper ~2-year history scan — merged, not substituted,
    # because a range can straddle the boundary between the two.
    seen = {(r[0], r[3]) for r in rows}
    rows += [r for r in _parse_history()
             if start <= r[0] < end and (r[0], r[3]) not in seen]
    rows = _filter_account(rows, account)
    rows.sort(key=lambda r: r[0])
    if not rows:
        return _empty_range_message(label, start, end, account)
    rows, sampled = _sample_for_summary(rows)
    extra = (f" (sampled {len(rows)} of {sampled} emails, spread evenly across "
             f"the period)" if sampled else "")
    lines = [_fmt_line(a, s, subj) for _, a, s, subj, _u in rows]
    return await _summarize(lines, label + extra)


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
    rows = _filter_account(_parse_lines(), account)
    rows.sort(key=lambda r: r[0], reverse=True)
    label = "your recent inbox"
    note = ""
    if unread:
        rows, note = _unread_rows(rows)
        label = "your UNREAD email"
        if note and not rows:
            return note
        if not rows:
            return ("You have no unread email — everything in the recent inbox "
                    "has been read.")
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
    if total > len(rows):
        label += (f" — NOTE: this is only the {len(rows)} most recent of {total} "
                  f"cached emails. Say so at the end, and do NOT imply this is "
                  f"the whole inbox. Tell the user they can ask for more with a "
                  f"count, or narrow by day or period")
    lines = [_fmt_line(a, s, subj) for _, a, s, subj, _u in rows]
    out = await _summarize(lines, label)
    return f"{out}\n\n{note}" if note else out


@register(
    "summarize_emails",
    "Read the user's Mail.app inbox and summarize it (grouped by theme, urgent "
    "items flagged). Use whenever the user asks about their email or inbox. "
    "ONLY scope it by date when the user actually named a time: `period` for a "
    "RANGE they named ('this month', 'last week'), `day` ('today', "
    "'yesterday', or an ISO date like '2026-07-14') for ONE day they named. "
    "For 'anything I need to reply to?', 'what's new?', 'anything important?' "
    "— which name no time — pass NEITHER and let it return the recent inbox. "
    "Both reach back roughly TWO YEARS. Pass `account` (e.g. the account's name) if the user "
    "asks about a SPECIFIC linked email account and more than one is linked — "
    "omit it otherwise. Summarized by the fast local model.",
    {"type": "object",
     "properties": {
         "period": PERIOD_ARG,
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD' — summarize that whole day's mail"},
         "count": {"type": "integer",
                   "description": "when `period`/`day` are omitted, how many recent messages to scan (default 20)"},
         "unread": {"type": "boolean",
                    "description": "true to cover ONLY unread email — use for 'what's unread', "
                                   "'anything I haven't read', 'what needs a reply'"},
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
                           unread: bool = False) -> str:
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
            # No exact keyword hit — rather than dead-end, hand back the whole
            # scoped set so the agent model can actually read through it itself. A
            # substring match on the model's guessed keyword is a much weaker
            # signal than the agent model reading the real text (e.g. it searches
            # "pickup" but the email says "collect").
            rows = scoped
            count = max(count, 10)
            note = (f"(no exact match for {query!r} — showing the raw set below so you "
                    "can look through it yourself)\n\n")
    if not rows:
        return (f"No emails found for {label} (note: raw content only covers "
                "roughly the 50 most recent emails — try summarize_emails for older mail).")
    rows = rows[-count:]
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
    return note + "\n\n---\n\n".join(blocks)


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
     }},
    category="email_read",
)
async def view_emails(query: str | None = None, day: str | None = None, count: int = 5,
                      account: str | None = None, period: str | None = None,
                      unread: bool = False) -> str:
    if account and (msg := _unknown_account_message(account)):
        return msg
    return await view_emails_impl(query, day, count, account, period, unread)


async def run_daily_email_summary() -> None:
    """Scheduler entry point (~8am): summarize YESTERDAY — the full day that
    just finished, not "recent N" (which could span days or miss the night's
    mail) — and push it as a notification."""
    summary = await summarize_inbox_for_day("yesterday")
    if summary.startswith("(") or summary.startswith("No emails") or not summary:
        return
    from service.assistant.hub import hub
    await hub.publish({"type": "email_summary", "summary": summary})
