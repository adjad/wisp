"""Is a mail sender someone the person already knows? Proven from the person's own side only.

Adding an event from an email to the Calendar without a tap needs, among other proofs, a
sender who is a KNOWN CORRESPONDENT. `known` answers that for one sender address, from the
person's saved Contacts and the person's own Sent mail; the email being judged is never
evidence about itself.

IMPORTANT rules:
- This proves the person has dealt with this ADDRESS before: it is saved in their Contacts,
  or their own Sent mail went to it before the message arrived. It does NOT prove the email
  really came from that address. A From line can be forged; whether this message is
  authentic is the authentication check's job, done elsewhere, and both are needed.
- `address` is the sender's real address as service/mail_sender.py parsed it. Only exact,
  casefolded, whole addresses are compared. A display name, a domain, a local part or "the
  same organisation" never matches. An empty or malformed address is never known.
- The person's own addresses (`mine`) never count as a correspondent, from either side.
- A Sent row counts only when it was sent strictly BEFORE `before_ts`, the arrival time of the
  message being judged: a reply sent after it arrived says nothing about who wrote to them.
- Sent mail counts only when its read is fresh. Stale or unreadable Sent mail, with no
  Contacts match, is "could not be checked", never "not known" and never "known".
- Readers fail CLOSED: an error is "could not be read" (None), which can only refuse.
- Recipients are untrusted header text, only ever compared here: never put in a prompt, a
  card or a log.

How far back Sent mail counts: any time before the message. No cutoff is added here, since a
cutoff can only weaken the proof's reach, never its strength (a person who wrote to an address
long ago did write to it). The real reach is what the readers supply: Google-linked accounts'
Sent sync covers gmail_source.SENT_DAYS (8 days) and Apple Mail's Sent index is read for
bridge.SENT_DAYS_MAX (31 days). An older correspondent is simply not proven by Sent mail.

What the rows expose: Gmail Sent rows carry the raw To header only (gmail_source._sync_sent);
Apple Mail's carry up to five To recipients, or Cc when there is no To (MailMemoryReader's
`sent`). Cc is matched when a row exposes it; Bcc never is.
"""
from __future__ import annotations

import math
import time

NO_ADDRESS = "no address"
SENT_UNCHECKED = "sent mail could not be checked"
UNKNOWN = "not someone you have written to or saved"

# A linked Google account's Sent list is current when synced this recently (the same bar
# the follow-ups feature uses), and Apple Mail's Sent index is re-read at most this often.
GMAIL_SENT_FRESH_S = 600.0
NATIVE_SENT_EVERY_S = 300.0
NATIVE_SENT_TIMEOUT_S = 10.0


def _exact(value) -> str:
    """The whole value as one clean casefolded address, or "" for anything else."""
    from service import mail_sender
    text = str(value or "").strip()
    if not text:
        return ""
    real = mail_sender.real_address(text)
    return real if real and real == text.casefold() else ""


def _contact(address: str, contact_addresses) -> bool | None:
    """True/False from the Contacts source, or None when Contacts could not be read."""
    if contact_addresses is None:
        return None
    try:
        if callable(contact_addresses):
            answer = contact_addresses(address)
            return None if answer is None else answer is True
        return address in {_exact(a) for a in contact_addresses} - {""}
    except Exception:  # noqa: BLE001 — unreadable Contacts can only refuse
        return None


def _sent_to(address: str, before_ts: float, sent_rows, mine: set[str]) -> bool:
    from service.proactive.sent_check import recipients
    for row in sent_rows:
        try:
            ts = float(row.get("ts"))
        except (AttributeError, TypeError, ValueError):
            continue
        if not math.isfinite(ts) or ts >= before_ts:
            continue
        try:
            people = recipients(row.get("to"))
        except Exception:  # noqa: BLE001 — an unreadable row proves nothing
            continue
        for _name, addr in people:
            if addr not in mine and _exact(addr) == address:
                return True
    return False


def known(address, *, before_ts, sent_rows, sent_fresh, contact_addresses, mine) -> tuple[bool, str]:
    """(True, "contact") / (True, "sent") when the person has dealt with this exact address,
    else (False, reason). `contact_addresses` is a set/iterable of saved addresses, a
    callable address -> True/False/None (None: unreadable), or None (Contacts unreadable)."""
    address = _exact(address)
    if not address:
        return False, NO_ADDRESS
    own = {_exact(a) for a in (mine or ())} - {""}
    if address in own:
        return False, UNKNOWN
    if _contact(address, contact_addresses):
        return True, "contact"
    try:
        before = float(before_ts)
    except (TypeError, ValueError):
        before = math.nan
    if sent_rows is None or sent_fresh is not True or not math.isfinite(before):
        return False, SENT_UNCHECKED
    if _sent_to(address, before, sent_rows, own):
        return True, "sent"
    return False, UNKNOWN


# --- production readers -----------------------------------------------------------------

_native: dict = {"at": None, "rows": None}


def _gmail_rows(now: float) -> tuple[list[dict], bool]:
    """Sent rows of the linked Google accounts whose Sent mail is current: signed in and
    synced within GMAIL_SENT_FRESH_S, and whether any account is current. A stale account's
    rows are left out."""
    from service import gmail_source
    accounts = gmail_source.connected_accounts() or {}
    last = getattr(gmail_source, "_last", {}) or {}
    current = set()
    for account, config in accounts.items():
        synced = (last.get(account) or {}).get("sent") if isinstance(last.get(account), dict) else None
        expired = isinstance(config, dict) and config.get("auth_expired")
        if not expired and isinstance(synced, (int, float)) and 0 <= now - synced <= GMAIL_SENT_FRESH_S:
            current.add(str(account).casefold())
    if not current:
        return [], False
    return [row for row in gmail_source.sent_rows() if str(row.get("account") or "").casefold() in current], True


async def _native_rows(now: float) -> list[dict] | None:
    """Apple Mail's Sent rows, read through the app at most every NATIVE_SENT_EVERY_S, or
    None when the last read failed (the app is away, Mail can't be read, it timed out)."""
    import asyncio
    at = _native["at"]
    if at is None or not 0 <= now - at < NATIVE_SENT_EVERY_S:
        _native.update(at=now, rows=None)
        try:
            from service.mail_memory import bridge
            rows, _capped = await asyncio.wait_for(
                bridge.read_sent(bridge.SENT_DAYS_MAX, timeout=NATIVE_SENT_TIMEOUT_S - 1), NATIVE_SENT_TIMEOUT_S)
            _native.update(rows=list(rows))
        except Exception:  # noqa: BLE001 — not read: not fresh
            pass
    return None if _native["rows"] is None else list(_native["rows"])


async def read_sent(now: float | None = None) -> tuple[list[dict] | None, bool]:
    """(the person's Sent rows, whether the read is fresh). Fresh means at least one source
    was read currently: a current Google-linked account or this cycle's Apple Mail read. Rows
    from a stale source are left out, so a source that failed only loses matches. Nothing
    current, or any unexpected error: (None, False)."""
    try:
        now = time.time() if now is None else float(now)
        try:
            gmail, google_current = _gmail_rows(now)
        except Exception:  # noqa: BLE001 — Google state unreadable: no Google account is current
            gmail, google_current = [], False
        native = await _native_rows(now)
        if native is None and not google_current:
            return None, False
        return gmail + (native or []), True
    except Exception:  # noqa: BLE001
        return None, False


def read_contact_addresses() -> set[str] | None:
    """Every email address saved in the person's Contacts, casefolded, or None when Contacts
    can't be read: no Contacts sync has landed this launch (nothing is restored from disk),
    or the app's last read said it may not read them. The app's ContactsReader pushes the
    whole handle -> name map (imessage_tools.apply_contacts_sync); its email handles are
    the address set. Contacts turned off in Wisp leaves an empty set, which proves nothing."""
    try:
        from service.tools import imessage_tools as im
        if getattr(im, "_contacts_privacy_current", False) is not True or getattr(im, "_contacts_not_allowed", True):
            return None
        return {a for a in (_exact(h) for h in dict(im._contacts) if "@" in str(h)) if a}
    except Exception:  # noqa: BLE001
        return None
