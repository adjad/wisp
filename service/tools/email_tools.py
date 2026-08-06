"""Email summary via gemma.

The inbox is READ by the Swift app (Mail.app AppleScript, clean Wisp.app identity
+ Automation grant) and pushed to /assistant/sync/emails — same split as the
calendar, because the Python backend can't get Automation access to Mail. Here we
just cache the pushed headers and summarize them with the FAST model (gemma), so
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
  • `_history` — up to a year back, header-only, every ~30 min (a full year's
    scan is too slow to run on the 5-min cadence). `summarize_inbox_for_day`
    falls back to this when a requested day isn't covered by `_headers`, and
    the profile builder (service/memory/profile.py) always prefers it.
  • `_raw_emails` — recent ~50, FULL body content, every 15 min. Stays small
    deliberately: `content of m` is the slow AppleScript call, so a year's
    worth of bodies isn't practical the way a year of headers is — verbatim
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
import time
from datetime import datetime, timedelta

from service.config import role_to_model
from service.inference.omlx_client import OMLXClient
from service.tools import cache_store
from service.tools.registry import register

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

# Latest inbox headers pushed by the Swift app — "epochSecs | account | sender |
# subject" per line (MailReader.swift), merged newest-first across every linked
# account. Recent-only (~200 per account) — see module docstring. Readers that
# care about ordering sort anyway; the merge is what makes that sort meaningful
# rather than something that has to be trusted.
_headers: str = ""
_headers_at: float = 0.0

# Same 4-field format, but the ~1-year-back scan (MailReader.swift's
# historyScript) — a separate cache so its slower cadence can't race/clobber
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
# by MailReader.swift). Ground truth for the profile builder — without it the
# extractor can't tell the user's own details from the many other people's
# details in the same mailbox, and it misattributed a stranger's address.
_identity_emails: list[str] = []

# RFC 2606 / RFC 6761 reserve these names precisely so they can never resolve to
# a real host, which makes them unambiguous placeholder data — nobody's Mail.app
# account lives at one. Filtered because scripts/install_cache_fixtures.sh
# writes a fixture identity_emails.txt into the LIVE cache, and unlike every
# other cache file it has no real counterpart to overwrite it: the real mail
# syncs replaced email_headers/raw/history and left this one asserting the
# user's address is testuser@example.com. Harmless while identity was only read
# by the profile builder; not harmless now that it's stated as ground truth in
# every mail summary. Wrong identity is worse than no identity, so drop it.
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


def have_emails() -> bool:
    return bool(_headers.strip())


# --- Machine-sender classification -------------------------------------------
#
# The profile builder extracts "durable facts" from every line it's given, so
# a mailbox that's 90% automated notification traffic produces a profile
# that's 90% notification trivia — a real build had "receives daily Google AI
# Pro plan reminders" as its single most-repeated bullet (195 of 868 lines),
# crowding out the person's actual identity and relationships from the fixed-
# size digest (see memory/profile.py's _dedup_lines / _DIGEST_CHARS). This is
# a cheap, deliberately generous heuristic, not a spam filter: false positives
# (dropping a genuine email) cost nothing here — the profile just doesn't
# learn one fact it could've — while false negatives let junk back into the
# exact section that's already prone to being drowned by it.
_MACHINE_SENDER_HINTS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "notification", "alert", "newsletter", "digest",
    "unsubscribe", "auto-confirm", "info@", "support@", "updates@", "news@",
)

# Known automated/marketing senders by NAME (case-insensitive, prefix match —
# "Glassdoor Jobs" and "Glassdoor" both hit "glassdoor"). Deliberately excludes
# schools/employers/orgs (UCSC, Cal Poly, etc.) — application and enrollment
# mail is exactly the durable, personal signal the profile wants, even though
# it's also high-volume and templated.
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
    for ts, _account, sender, _subject in (_parse_history() or _parse_lines()):
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
    """Each raw record -> {ts, account, sender, to, subject, body}. Skips
    anything that doesn't parse (e.g. a message AppleScript couldn't read)."""
    out: list[dict] = []
    for record in _raw_emails.split(_RAW_RS):
        record = record.strip()
        if not record:
            continue
        parts = record.split(_RAW_FS)
        if len(parts) != 6:
            continue
        ts_s, account, sender, to, subject, body = parts
        try:
            ts = float(ts_s)
        except ValueError:
            continue
        out.append({"ts": ts, "account": account.strip(), "sender": sender.strip(),
                     "to": to.strip(), "subject": subject.strip(), "body": body.strip()})
    return out


def _parse_pipe_lines(text: str) -> list[tuple[float, str, str, str]]:
    """Each "epochSecs | account | sender | subject" line -> (ts, account,
    sender, subject). Shared by both `_headers` and `_history` — same format,
    different scan depth. Skips anything that doesn't parse (e.g. a stray line
    without a timestamp)."""
    out: list[tuple[float, str, str, str]] = []
    for line in text.strip().splitlines():
        parts = line.split(" | ", 3)
        if len(parts) != 4:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        out.append((ts, parts[1], parts[2], parts[3]))
    return out


def _parse_lines() -> list[tuple[float, str, str, str]]:
    return _parse_pipe_lines(_headers)


def _parse_history() -> list[tuple[float, str, str, str]]:
    return _parse_pipe_lines(_history)


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


def _filter_account(rows: list[tuple[float, str, str, str]], account: str | None
                    ) -> list[tuple[float, str, str, str]]:
    if not account:
        return rows
    q = account.lower()
    return [r for r in rows if q in r[1].lower()]


async def _summarize(raw_lines: list[str], header_label: str) -> str:
    if not raw_lines:
        return f"No emails found for {header_label}."
    c = _c()
    model = role_to_model("fast")  # gemma — cheap, and the user asked for it
    await c.ensure_only(model)
    today_str = datetime.now().strftime("%A, %B %-d, %Y")
    # Profile context: lets the digest recognize senders and organizations
    # ("that's their school", "that's Mom") instead of re-deriving who everyone
    # is from scratch each time. Matters especially here because this output is
    # returned VERBATIM as the final answer (see main._PRESYNTHESIZED_TOOLS),
    # so the agent loop's own profile context never reaches the user.
    # Identity block on top of that: a digest written in the second person has
    # to know whose inbox this is before it decides what "your" refers to. No
    # message-attribution rules here — inbox lines are `sender | subject`, with
    # none of the group-chat ambiguity those rules exist to resolve.
    from service.memory.identity import identity_prompt_block
    from service.memory.profile import profile_context_block
    identity = identity_prompt_block(messages=False).strip()
    resp = await c.chat(
        model,
        [{"role": "system", "content": f"{identity}\n\n{_SYS}\nToday is {today_str}; resolve any "
                                        "relative dates (due 'Friday', 'tomorrow') against it. "
                                        "A '[account name]' prefix (when present) names which "
                                        "linked email account a message is from — mention the "
                                        "account only if the user is asking about a specific one "
                                        "or more than one appears, otherwise ignore it."
                                        + profile_context_block(1600)},
         {"role": "user", "content": f"Inbox for {header_label} ([account] sender | subject):\n"
                                     + "\n".join(raw_lines)}],
        # 400 was a hard ceiling that would truncate a more detailed summary
        # mid-sentence regardless of what _SYS asked for — raised alongside
        # loosening _SYS's terseness constraint so there's actually room for
        # the extra context to land. temperature 0.6 (up from a flat 0.3): the
        # low setting produced correct-but-robotic prose, and warmth/expressive
        # phrasing is exactly what higher sampling temperature buys — still low
        # enough to stay grounded in the actual inbox, not confabulate.
        temperature=0.6, max_tokens=800)
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
        # doesn't reach this far back — fall back to the ~1-year history scan
        # (headers only, same format, just a deeper/slower-refreshed scan) so
        # older days still work instead of dead-ending on "no emails found".
        rows = [r for r in _parse_history() if start <= r[0] < end]
    rows = _filter_account(rows, account)
    rows.sort(key=lambda r: r[0])
    lines = [_fmt_line(a, s, subj) for _, a, s, subj in rows]
    return await _summarize(lines, label)


async def summarize_inbox_recent(count: int = 20, account: str | None = None) -> str:
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
    rows = rows[:count]
    lines = [_fmt_line(a, s, subj) for _, a, s, subj in rows]
    return await _summarize(lines, "your recent inbox")


@register(
    "summarize_emails",
    "Read the user's Mail.app inbox and summarize it (grouped by theme, urgent "
    "items flagged). Use whenever the user asks about their email or inbox. "
    "Pass `day` ('today', 'yesterday', or an ISO date like '2026-07-14') to "
    "summarize ALL emails received on that specific day — this works for any "
    "day in roughly the last YEAR, not just recent ones; omit it to just get "
    "the most recent messages regardless of date. Pass `account` (e.g. the "
    "account's name) if the user asks about a SPECIFIC linked email account "
    "and more than one is linked — omit it otherwise. Summarized by the fast "
    "local model.",
    {"type": "object",
     "properties": {
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD' — summarize that whole day's mail"},
         "count": {"type": "integer",
                   "description": "when `day` is omitted, how many recent messages to scan (default 20)"},
         "account": {"type": "string",
                     "description": "only include this linked account (only useful when more than one is linked)"},
     }},
    category="email_read",
)
async def summarize_emails(day: str | None = None, count: int = 20,
                           account: str | None = None) -> str:
    # Interactive path: if the cache is cold (e.g. just after launch), pull a
    # fresh sync now instead of answering "no emails" and making the user retry.
    await _ensure_email_cache()
    if day:
        return await summarize_inbox_for_day(day, account)
    return await summarize_inbox_recent(count, account)


def _matches(query: str, sender: str, to: str, subject: str, body: str) -> bool:
    q = query.lower()
    return any(q in f.lower() for f in (sender, to, subject, body))


async def view_emails_impl(query: str | None = None, day: str | None = None,
                           count: int = 5, account: str | None = None) -> str:
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
    if day:
        try:
            start, end, label = _day_bounds(day)
        except ValueError:
            return f"(couldn't understand the date {day!r} — use 'today', 'yesterday', or YYYY-MM-DD)"
        rows = [r for r in rows if start <= r["ts"] < end]
    rows.sort(key=lambda r: r["ts"])
    scoped = rows
    note = ""
    if query:
        matched = [r for r in scoped if _matches(query, r["sender"], r["to"], r["subject"], r["body"])]
        if matched:
            rows = matched
        else:
            # No exact keyword hit — rather than dead-end, hand back the whole
            # scoped set so gpt-oss can actually read through it itself. A
            # substring match on the model's guessed keyword is a much weaker
            # signal than gpt-oss reading the real text (e.g. it searches
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
        blocks.append(f"{acct}From: {r['sender']}\nTo: {r['to']}\nSubject: {r['subject']}\n"
                      f"Date: {when}\n\n{r['body']}")
    return note + "\n\n---\n\n".join(blocks)


@register(
    "view_emails",
    "Get the RAW, verbatim content of the user's emails — full body text, "
    "sender/recipient email addresses, subject line, and exact time — not a "
    "summary. Use this instead of summarize_emails when the user needs a "
    "specific detail FROM an email: an order number, a confirmation code, an "
    "address, a pickup/delivery time, party details, exact wording to quote "
    "back, etc. Pass `query` to search sender/recipient/subject/body for a "
    "keyword (e.g. a person's name, 'order', 'birthday'); pass `day` to scope "
    "to a specific day; pass `account` if the user asks about a SPECIFIC "
    "linked email account and more than one is linked; omit any/all for the "
    "most recent emails. Covers roughly the 50 most-recently-received emails "
    "(shared across the linked accounts when more than one) — for anything "
    "older, fall back to summarize_emails (which reaches back about a year).",
    {"type": "object",
     "properties": {
         "query": {"type": "string",
                   "description": "keyword to search for in sender, recipient, subject, or body"},
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD' — scope to that day"},
         "count": {"type": "integer",
                   "description": "max emails to return in full (default 5 — full bodies are long)"},
         "account": {"type": "string",
                     "description": "only include this linked account (only useful when more than one is linked)"},
     }},
    category="email_read",
)
async def view_emails(query: str | None = None, day: str | None = None, count: int = 5,
                      account: str | None = None) -> str:
    return await view_emails_impl(query, day, count, account)


def all_raw_email_text(*, exclude_machine: bool = False) -> str:
    """Every cached raw email (date/account/sender/to/subject/body), verbatim,
    OLDEST-FIRST — for the profile builder (service/memory/profile.py). No
    query/day filtering and no summarization, just the raw cache; covers
    roughly the 50 most-recently-scanned emails, same limit as view_emails.

    The Date line is essential rather than cosmetic: without it the profile
    builder couldn't distinguish an old thread from a current one and wrote
    year-old plans as if they were upcoming.

    `exclude_machine` drops automated/marketing senders (see
    is_machine_sender) — the profile builder wants this; view_emails/
    summarize_emails don't, since a user reading their own inbox wants
    everything, promos included."""
    _purge_raw_if_expired()
    records = sorted(_parse_raw(), key=lambda r: r["ts"])
    if exclude_machine:
        records = [r for r in records if not is_machine_sender(r["sender"])]
    blocks = []
    for r in records:
        acct = f"[{r['account']}] " if r["account"] else ""
        when = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
        blocks.append(f"Date: {when}  {acct}From: {r['sender']}  To: {r['to']}  "
                      f"Subject: {r['subject']}\n{r['body']}")
    return "\n\n---\n\n".join(blocks)


def all_header_text(*, exclude_machine: bool = False) -> str:
    """Every cached header line (date/account/sender/subject), oldest-first —
    for the profile builder. Prefers the ~1-year `_history` scan (richer,
    deeper); falls back to the fast recent `_headers` cache if history hasn't
    synced yet. See all_raw_email_text on why the date matters and what
    `exclude_machine` does."""
    rows = sorted(_parse_history() or _parse_lines(), key=lambda r: r[0])
    if exclude_machine:
        rows = [r for r in rows if not is_machine_sender(r[2])]
    return "\n".join(
        f"{datetime.fromtimestamp(ts).strftime('%Y-%m-%d')} | {_fmt_line(a, s, subj)}"
        for ts, a, s, subj in rows)


async def run_daily_email_summary() -> None:
    """Scheduler entry point (~8am): summarize YESTERDAY — the full day that
    just finished, not "recent N" (which could span days or miss the night's
    mail) — and push it as a notification."""
    summary = await summarize_inbox_for_day("yesterday")
    if summary.startswith("(") or summary.startswith("No emails") or not summary:
        return
    from service.assistant.hub import hub
    await hub.publish({"type": "email_summary", "summary": summary})
