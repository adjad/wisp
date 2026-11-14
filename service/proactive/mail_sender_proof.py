"""Who sent an email read through Apple Mail, as far as Apple Mail lets Wisp tell.

Auto-add (autoadd.py) acts on a dated email without a tap only with proof of who sent it. For
mail fetched by Wisp's own Google sign-in that proof is Gmail's own authentication result
(autoadd.default_mail_proof). This module is the rule for every OTHER email: one Apple Mail
read, from any account (iCloud, Exchange, a work IMAP server, or a Google account added to
Apple Mail but not linked in Wisp).

Without server-side sender proof, a forged From line that imitates a known address could make
Wisp add something. The owner chose this on 2026-11-13 ("do act on email when not connected to
gmail").

IMPORTANT rules:
- Every check the Gmail proof makes about the sender and the date still holds here, in the same
  order and with the same reasons: a sender read strictly (not odd, one real address), a real
  person (not an organisation, a roll-up or an account alert), the dated sentence from this very
  message, and a KNOWN correspondent proven from the user's side (mail_known.known: a saved
  contact's exact address, or one the user's own Sent mail went to before this arrived). The
  user's own address is never a correspondent. Only the last Gmail check, Gmail's verdict, is
  replaced by `header_verdict` below.
- The receiving server's word, when the reader supplies it. `item["auth_results"]` is the list of
  the message's Authentication-Results header values, top-most first (see READER CONTRACT).
  - Present and PASSING (dmarc=pass aligned with the From domain, or an aligned dkim=pass /
    spf=pass, by mail_auth's rules): acted on, recorded as "verified".
  - Present and FAILING (any dmarc=fail or unknown dmarc result, a dmarc pass for another domain,
    no aligned pass, unparseable, or more than one result claiming the same server): NOT acted
    on. A tap card, as before.
  - Absent (the key missing or None, an empty list, or the trusted result names no dmarc, dkim or
    spf check at all): acted on by the known-sender rule alone, recorded as "unverified".
- Which Authentication-Results header is trusted. A header can be written by anyone on the path,
  the sender included; the one that counts is the one the user's own receiving server added, and
  a server prepends its headers, so it is above everything the sender wrote. So:
  1. The account's mail domains are known (the reader's `account_addresses`, else the Mail
     account label when it is an address, else the user's own addresses from identity): the
     trusted set is the TOP-MOST run of consecutive headers whose authserv-id is that domain's
     receiving operator (RECEIVERS, e.g. icloud.com for an iCloud, me.com or mac.com address;
     iCloud writes one header per check) or a subdomain of it. Any authserv-id of that run seen
     again anywhere in the message refuses (someone else wrote one), as the Gmail rule does.
  2. Nothing matches, or no domain is known: the top-most header alone.
  Its limits: (a) where the receiving server adds no Authentication-Results at all, the top-most
  header is the sender's own and is read as if it were the server's. (b) An internal hop above
  the border server (a filter, a forwarder) may be what is read. (c) A server whose authserv-id
  is not under the account's domain and not in RECEIVERS (a custom domain hosted elsewhere) falls
  to rule 2. None of these lets a header ENABLE an add that the known-sender rule would not
  allow: a pass and an absent result act alike; only a failure changes the outcome, and it only
  refuses. A forger can therefore at most make a forged mail look "verified" internally, or make
  a real one a tap card. The one thing the server's genuine top-most failure always does is stop
  the add, and a sender cannot remove a header the server writes above theirs.
- Mail of a Google account LINKED in Wisp keeps Gmail's proof, whichever reader saw it. An Apple
  Mail copy is treated as that account's mail when its account addresses, its Mail account label
  or its To header name a linked address; then only gmail_source's verdict for the same
  Message-ID and sender counts, exactly as for the Google-fetched copy. And any linked account
  whose fetched evidence for this Message-ID and sender FAILS refuses the Apple Mail copy too.
  Mail of every other account follows this module's rule while a Google account is linked:
  linking Google does not change what an iCloud or Exchange account's mail proves.
- APPLE_MAIL_ACTS is the fail-closed switch: False, and no email from Apple Mail is added
  without a tap, whatever else holds. The action is its own key (autoadd.ACTION_APPLE_MAIL),
  so a yes given before this was named does not cover it (acting.ACTIONS).

READER CONTRACT (what the Apple Mail reader must add for header proof; until it does, every
Apple Mail email is "absent" and acts on the known-sender rule alone):
- On each inbox cache row, and forwarded by sources.mail_items onto the item from the newest
  incoming message (`last_in`):
  - `auth_results`: list[str]. The value (after the colon) of every `Authentication-Results`
    header of that message, in the order they appear in its raw header block, top-most first.
    Folded lines may be left folded (they are unfolded here). At most mail_auth.MAX_HEADERS (64)
    values, each at most mail_auth.MAX_HEADER_CHARS (8192) characters; more is refused. Missing
    key or None = not read; [] = read, none present. From AppleScript: `all headers of m`, keep
    the header lines named Authentication-Results (case-insensitive) with their continuations.
  - `account_addresses`: list[str]. The Mail account's own `email addresses`, so the receiving
    server can be told apart. Optional.
"""
from __future__ import annotations

import re

from service import mail_auth

APPLE_MAIL_ACTS = True

VERIFIED, UNVERIFIED, GMAIL = "verified", "unverified", "gmail"
PASS, FAIL, ABSENT = "pass", "fail", "absent"

# A mail domain -> the domain whose hosts write its Authentication-Results. A domain not listed
# is its own operator. Matching is "equal to, or a subdomain of".
RECEIVERS: dict[str, str] = {
    "icloud.com": "icloud.com", "me.com": "icloud.com", "mac.com": "icloud.com",
    "gmail.com": "google.com", "googlemail.com": "google.com",
    "yahoo.com": "yahoo.com", "ymail.com": "yahoo.com", "rocketmail.com": "yahoo.com",
    "aol.com": "aol.com",
    "fastmail.com": "messagingengine.com", "fastmail.fm": "messagingengine.com",
}

_NAME = re.compile(r"^\s*authentication-results\s*:", re.I)
_FOLD = re.compile(r"\r?\n[ \t]+")
_AUTH_METHODS = ("dmarc", "dkim", "spf")


# --- the header rule ------------------------------------------------------------------------------

def _unfold(value: str) -> str:
    text = _FOLD.sub(" ", value)
    return _NAME.sub("", text, count=1).strip()


def receiving_domains(addresses) -> set[str]:
    """The operators whose authserv-ids count for these account addresses."""
    out: set[str] = set()
    for address in addresses or ():
        domain = mail_auth._address_domain(str(address or "").strip())
        if domain:
            out.add(RECEIVERS.get(domain, domain))
    return out


def _matches(authserv: str | None, domains: set[str]) -> bool:
    return bool(authserv) and any(authserv == d or authserv.endswith("." + d) for d in domains)


def trusted(headers, domains: set[str]) -> tuple[list[str] | None, str]:
    """(the headers to read, "") or (None, why refused). See the module docstring."""
    if not isinstance(headers, (list, tuple)) or len(headers) > mail_auth.MAX_HEADERS:
        return None, "unreadable"
    values = []
    for header in headers:
        if not isinstance(header, str) or len(header) > mail_auth.MAX_HEADER_CHARS:
            return None, "unreadable"
        values.append(_unfold(header))
    ids = [mail_auth._authserv_id(v) for v in values]
    first = next((i for i, a in enumerate(ids) if _matches(a, domains)), None) if domains else None
    if first is None:
        if ids[0] is None:
            return None, "unparseable"
        return [values[0]], ""
    end = first
    while end < len(ids) and _matches(ids[end], domains):
        end += 1
    run = ids[first:end]
    if any(ids.count(a) > 1 for a in run):
        return None, "more than one result claims to be your mail server's"
    return values[first:end], ""


def _parse(header: str):
    """mail_auth.parse, also reading an id-less header (Microsoft writes `spf=pass ...;`)."""
    if mail_auth._authserv_id(header) == "":
        return mail_auth.parse("unnamed; " + header)
    return mail_auth.parse(header)


def header_verdict(headers, *, from_address: str, domains: set[str]) -> tuple[str, str]:
    """(PASS | FAIL | ABSENT, reason). Absent covers None and []. Strictness is mail_auth's
    `authenticated`, applied to every result in the trusted set together."""
    if headers is None or (isinstance(headers, (list, tuple)) and not headers):
        return ABSENT, "no result from your mail server"
    from_domain = mail_auth._address_domain(from_address) if isinstance(from_address, str) else ""
    if not from_domain or from_address.strip().startswith("@"):
        return FAIL, "no from address"
    chosen, why = trusted(headers, domains)
    if chosen is None:
        return FAIL, why
    results = []
    try:
        for header in chosen:
            results += _parse(header)[1]
    except mail_auth._Unparseable:
        return FAIL, "unparseable"
    if not any(method in _AUTH_METHODS for method, _r, _p in results):
        return ABSENT, "no sender check in your mail server's result"
    dmarc_pass = False
    for method, result, props in results:
        if method != "dmarc":
            continue
        if result == "fail":
            return FAIL, "dmarc fail"
        if result == "pass":
            found = [mail_auth._domain(v) for v in props.get("header.from", [])]
            if not found:
                continue
            if not all(mail_auth.aligned(d, from_domain) for d in found):
                return FAIL, "dmarc for another domain"
            dmarc_pass = True
        elif result not in ("none", "temperror", "permerror"):
            return FAIL, "dmarc unknown result"
    if dmarc_pass:
        return PASS, "dmarc pass"
    for method, result, props in results:
        if method == "dkim" and result == "pass":
            signer = props.get("header.d")
            domain = mail_auth._domain(signer[0]) if signer and len(signer) == 1 else ""
            if not signer:
                ident = props.get("header.i")
                domain = mail_auth._address_domain(ident[0]) if ident and len(ident) == 1 else ""
            if domain and mail_auth.aligned(domain, from_domain):
                return PASS, "dkim pass aligned"
    for method, result, props in results:
        if method == "spf" and result == "pass":
            mailfrom = props.get("smtp.mailfrom")
            if mailfrom and len(mailfrom) == 1:
                value = mailfrom[0]
                domain = mail_auth._address_domain(value) if "@" in value else mail_auth._domain(value)
                if domain and mail_auth.aligned(domain, from_domain):
                    return PASS, "spf pass aligned"
    return FAIL, "no aligned pass"


# --- the proof for one Apple Mail item ------------------------------------------------------------

def _addresses(values) -> set[str]:
    from service import mail_sender
    out = set()
    for value in values or ():
        real = mail_sender.real_address(str(value or "").strip())
        if real:
            out.add(real)
    return out


def _to_addresses(header: str) -> set[str]:
    try:
        from service.proactive.sent_check import recipients
        return {str(a).casefold() for _n, a in recipients(header) if a}
    except Exception:  # noqa: BLE001 — unreadable: names no linked account
        return set()


def _linked() -> set[str]:
    from service.proactive import sources
    return {a.casefold() for a in sources.gmail_accounts()}


def _gmail_verdict(account: str, message_id: str, address: str):
    from service import gmail_source
    verdict = gmail_source.authenticated_for(account, message_id, address)
    return verdict if isinstance(verdict, tuple) and len(verdict) == 2 else None


async def apple_mail_proof(item: dict, plan: dict, evidence: dict) -> tuple[bool, str]:
    """(True, "") when this Apple Mail item may be acted on, with plan["sender_proof"] set to
    "verified", "unverified" or "gmail"; else (False, why). `evidence` is the per-cycle cache
    autoadd.default_mail_proof shares (Sent rows, Contacts addresses, the user's addresses)."""
    from service.proactive import mail_known
    if not APPLE_MAIL_ACTS:
        return False, "Apple Mail does not act"
    ref = item.get("ref") or {}
    address = str(ref.get("to") or "").strip().casefold()
    message_id = str(ref.get("message_id") or "").strip()
    if item.get("odd_sender") or not address:
        return False, "sender not read cleanly"
    if not item.get("person") or item.get("digest") or item.get("critical"):
        return False, "not a person"
    arrived = item.get("received_ts")
    if type(arrived) not in (int, float) or plan.get("line_ts") != arrived:
        return False, "the date is not from this message"
    raw_account = str(item.get("raw_account") or "").strip().casefold()
    own_account = _addresses(item.get("account_addresses") or ()) | ({raw_account} if "@" in raw_account else set())
    if "sent" not in evidence:
        evidence["sent"] = await mail_known.read_sent()
        evidence["contacts"] = mail_known.read_contact_addresses()
        try:
            from service.memory.identity import user_emails
            mine = {str(e).casefold() for e in user_emails() or []}
        except Exception:  # noqa: BLE001
            mine = set()
        evidence["mine"] = mine
    mine = set(evidence["mine"]) | own_account
    if address in mine:
        return False, "your own message"
    # Mail of a Google account linked in Wisp keeps Gmail's proof, whoever read it.
    proof = ""
    linked = _linked()
    if linked:
        claimed = linked & (own_account | _to_addresses(str(item.get("to_header") or "")))
        verdicts = {acct: _gmail_verdict(acct, message_id, address) for acct in sorted(linked)} if message_id \
            else {}
        # Gmail fetched this very message for some linked account and it did not pass: never.
        if any(v is not None and v[0] is not True for v in verdicts.values()):
            return False, "your mail provider's check failed"
        if claimed:
            if not any(verdicts.get(acct) and verdicts[acct][0] is True for acct in claimed):
                return False, "mail of a Google account you linked needs Gmail's proof"
            proof = GMAIL
    sent_rows, sent_fresh = evidence["sent"]
    known, why = mail_known.known(address, before_ts=float(arrived), sent_rows=sent_rows, sent_fresh=sent_fresh,
                                  contact_addresses=evidence["contacts"], mine=mine)
    if known is not True:
        return False, why
    if not proof:
        domains = receiving_domains(own_account or mine)
        verdict, reason = header_verdict(item.get("auth_results"), from_address=address, domains=domains)
        if verdict == FAIL:
            return False, "your mail server's check failed: " + reason
        proof = VERIFIED if verdict == PASS else UNVERIFIED
    plan["sender_proof"] = proof
    return True, ""
