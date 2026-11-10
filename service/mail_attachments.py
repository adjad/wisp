"""Find email attachments across Apple Mail and Gmail, and read them (Ask: read_email_attachments).

One request looks in two places and treats them as one mailbox: Apple Mail's index
(read by the app, which holds Full Disk Access) and every connected Gmail account.
An email in both appears once, its files merged by name, keeping Apple Mail's saved
copy AND the Gmail reference. A file is read from the saved copy first (instant,
offline, no 7-day Gmail sign-in) and from Gmail when it isn't on disk.

Rules:
- IMPORTANT: a file is never run. Programs, scripts, installers and archives are only
  inspected as bytes (service/attachment_child.py); archives are never unpacked.
- IMPORTANT: everything that comes from a file (its name, every inspect field, its
  contents) is attacker-controlled. Names are cleaned like the child's `_pretty`, contents
  go inside `untrusted.fence(..., EMAIL_ATTACHMENT)`, and nothing from a file is ever
  logged or printed.
- Only when asked: this runs for the tool call, never in the background.
- Text that was read is kept in service/attachment_store.py (this Mac only, dropped after
  30 days unused and when its account is disconnected), so a follow-up question about the
  same file needs no fetch and no second read. Nothing is kept for a file that wasn't asked for.
- Text in pictures and scanned pages is recognised by the app (Apple's on-device text
  recognition) from JPEGs the child process re-encoded; the app never decodes the sender's
  bytes. Without the app, or when a picture isn't text, the vision model reads it as before.
- Local only: `documents.local_model()` is called before any byte is fetched, and a model
  that isn't this Mac's engine means nothing is read.
- A failure belongs to one file (or one source) and has a plain reason. `run` never raises.
- The whole answer fits email_digest.RESULT_BUDGET_CHARS, shared as it goes: what one file
  leaves unused flows to the next. It says what it left out and why.
- Emails dated more than 36 hours ahead are real scheduled mail: left out of "recent"
  results unless the person gave a date range. Tiny images (logos, signature icons) are
  skipped unless named.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from typing import NamedTuple
from datetime import datetime
from pathlib import PurePath

import httpx

from service.attachment_child import ENCRYPTED_PDF_REASON, _pretty
from service.document_extract import SCANNED as _SCANNED_PDF
from service.mail_memory import bridge
from service.tools import untrusted

ATTACHMENT_TOOL = "read_email_attachments"
MAX_EMAILS = 10
MAX_READS = 8
DEADLINE_S = 150.0
RESULT_BUDGET_CHARS = 6000
TINY_IMAGE_BYTES = 20_000
FUTURE_HOURS = 36
DEFAULT_COUNT = 5
_RESERVE = 450                      # room kept for the "left out" notes
_APPLE_LIMIT = 300
_GMAIL_SEARCH_S = 25.0
_FILE_TIMEOUT_S = 60.0
_MAX_NOTE = 200
_OCR_MIN_CHARS = 40                 # fewer recognised characters than this: not a picture of text
_OCR_MIN_CONFIDENCE = 0.3
_OCR_PAGE_S = 20.0
_OCR_SURE_CONFIDENCE = 0.8             # at or above this on every page, the words are called exact
_OCR_REST_S = 300.0                 # after the app fails to answer, pictures go straight to the model this long
_ocr_resting_until = 0.0
_STRUCTURED_NAMES = frozenset({"ics", "vcf"})
_SAVED = "Wisp's saved reading"
_UNPRINTABLE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\ufeff]")
_KIND = getattr(untrusted, "EMAIL_ATTACHMENT", "email attachment")

_STOP_WORDS = frozenset("""
a an and any are about all attached attachment attachments by can could do email emails file files find for from
get give got has have i in is it its last latest look me message messages mine most my newest of on or please
read recent recently send sent show some that the their them this to up us was we what which with within you your
pdf picture pictures photo photos image images spreadsheet document documents doc attach attaching open
""".split())

_PICTURE_NAMES = frozenset({"png", "jpg", "jpeg", "gif", "bmp", "heic", "heif", "tif", "tiff", "webp", "avif"})
_PROGRAM_NAMES = frozenset({
    "app", "bin", "cpl", "com", "dll", "dmg", "dylib", "exe", "hta", "iso", "jar", "lnk", "mpkg", "msi", "msp",
    "pkg", "reg", "scpt", "scr", "so", "tool", "command", "vbe", "jse", "pif"})
_ARCHIVE_NAMES = frozenset({"zip", "rar", "7z", "gz", "tgz", "tar", "bz2", "xz", "z", "cab"})
_SCRIPT_NAMES = frozenset({"sh", "bash", "zsh", "py", "js", "mjs", "rb", "pl", "php", "ps1", "bat", "cmd", "vbs",
                           "lua", "applescript"})
_TEXT_NAMES = _SCRIPT_NAMES | frozenset({
    "ics", "vcf", "vtt", "srt", "svg", "ini", "cfg", "conf", "toml", "sql", "css", "ts", "tsx", "jsx", "java", "c",
    "h", "cpp", "swift", "go", "rs", "kt", "tex", "eml", "plist", "env", "rtx"})
_OFFICE_ZIPPED = frozenset({"docx", "xlsx", "pptx", "odt", "docm", "xlsm", "pptm"})
_MACRO_MAP = {"docm": "docx", "xlsm": "xlsx", "pptm": "pptx"}
_DOCUMENT_NAMES = frozenset({"pdf", "docx", "xlsx", "pptx", "odt", "doc", "rtf", "html", "htm", "txt", "text", "md",
                             "markdown", "csv", "tsv", "json", "log", "xml", "yaml", "yml"}) | frozenset(_MACRO_MAP)
_OLD_OFFICE = {"xls": "xlsx", "ppt": "pptx"}
_TABLE_KINDS = frozenset({"xlsx", "csv", "tsv"})

_LOCAL_WHY = {
    "not_on_disk": "Mail hasn't downloaded it to this Mac",
    "too_large": "it is larger than Wisp can read (32 MB)",
    "unreadable": "Mail's saved copy couldn't be read",
    "not_found": "Mail no longer has it",
    "no_mail_index": "Mail's index isn't available",
    "app_not_connected": "the Wisp app isn't connected",
}
_APPLE_SEARCH_WHY = {
    "app_not_connected": "the Wisp app isn't connected",
    "no_mail_index": "Mail's index isn't available",
    "unsupported_schema": "this version of Mail isn't supported",
    "attachments_timeout": "Mail didn't answer in time",
}
_LOCAL_ONLY = ("Wisp reads attachments only with a model running on this Mac, and Ask isn't using one right now, "
               "so nothing was opened.")


# --- small helpers ----------------------------------------------------------------

def terms_of(query: str | None) -> list[str]:
    """The words of `query` that name something to look for, at most five."""
    words: list[str] = []
    for raw in re.findall(r"[\w.@+'-]+", str(query or "").casefold()):
        word = re.sub(r"'s$", "", raw).strip(".'-")
        if len(word) >= 2 and word not in _STOP_WORDS and word not in words:
            words.append(word)
    return words[:bridge.ATTACHMENT_TERMS_MAX]


def _suffix(name: str) -> str:
    return PurePath(name or "").suffix.lower().lstrip(".")


def _safe_name(name: str) -> str:
    """A file name safe to display and to pass to a child process."""
    return _pretty(PurePath(str(name or "")).name.replace("\x00", ""), 100) or "file"


def _strs(value, count: int, limit: int = 160) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_pretty(item, limit) for item in value[:count] if isinstance(item, str) and item.strip()]


def _size_text(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{round(size / 1024)} KB"
    return f"{size} B"


def _is_picture(name: str, mime: str = "") -> bool:
    return _suffix(name) in _PICTURE_NAMES or str(mime).startswith("image/")


# --- merged model -----------------------------------------------------------------

@dataclass
class _File:
    name: str
    size: int = 0
    mime: str = ""
    rowid: int = 0
    attachment_id: str = ""
    on_disk: bool = False
    gmail_account: str = ""
    gmail_message: str = ""
    gmail_attachment: str = ""
    inline: bool = False
    data: bytes | None = None
    nth: int = 1           # which file of this name in its email, so repeats keep their own bytes
    account: str = ""      # Apple Mail's own label for the account the saved copy is in
    read_account: str = ""  # the account the bytes were actually read from (set by _fetch)

    @property
    def has_gmail(self) -> bool:
        return bool(self.gmail_account and self.gmail_message and (self.gmail_attachment or self.data is not None))

    @property
    def has_local(self) -> bool:
        return bool(self.rowid and self.attachment_id)


@dataclass
class _Email:
    key: str
    ts: float
    sender: str = ""
    subject: str = ""
    rfc: str = ""
    apple: bool = False
    gmail: list[str] = field(default_factory=list)
    files: list[_File] = field(default_factory=list)
    _index: dict = field(default_factory=dict)


def _email_key(rfc: str, subject: str, ts: float) -> str:
    rfc = bridge.norm_rfc(rfc)
    if rfc and not rfc.lstrip("-").isdigit():
        return "id:" + rfc
    return f"s:{str(subject or '').strip().casefold()}|{int(float(ts or 0) // 120)}"


def _add_file(email: _Email, source: str, counts: dict, f: _File) -> None:
    """Merge `f` into the email by (casefolded name, nth file of that name in this source)."""
    base = f.name.casefold()
    nth = counts[(source, base)] = counts.get((source, base), 0) + 1
    f.nth = nth
    have = email._index.get((base, nth))
    if have is None:
        email._index[(base, nth)] = f
        email.files.append(f)
        return
    if f.has_local and not have.has_local:
        have.rowid, have.attachment_id, have.on_disk = f.rowid, f.attachment_id, f.on_disk
        have.account = f.account
    if f.has_gmail and not have.has_gmail:
        have.gmail_account, have.gmail_message = f.gmail_account, f.gmail_message
        have.gmail_attachment, have.data, have.inline = f.gmail_attachment, f.data, f.inline
    have.size = have.size or f.size
    have.mime = have.mime or f.mime


def _merge(apple_rows: list[dict], gmail_messages: list[dict]) -> list[_Email]:
    emails: dict[str, _Email] = {}
    counts: dict[str, dict] = {}

    def get(key: str, ts: float, sender: str, subject: str, rfc: str) -> _Email:
        email = emails.get(key)
        if email is None:
            email = emails[key] = _Email(key, ts, sender, subject, bridge.norm_rfc(rfc))
        return email

    for r in apple_rows:
        who = (f"{r['sender_name']} <{r['sender']}>" if r.get("sender_name") and r.get("sender")
               else r.get("sender_name") or r.get("sender") or "")
        email = get(_email_key(r.get("rfc_message_id", ""), r.get("subject", ""), r["ts"]), r["ts"], who,
                    r.get("subject", ""), r.get("rfc_message_id", ""))
        email.apple = True
        _add_file(email, "apple", counts.setdefault(email.key, {}),
                  _File(r["name"], int(r.get("size") or 0), rowid=r["rowid"], attachment_id=r["attachment_id"],
                        on_disk=bool(r.get("on_disk")), account=str(r.get("account") or "")))
    for m in gmail_messages:
        email = get(_email_key(m.get("rfc_message_id", ""), m.get("subject", ""), m["ts"]), m["ts"],
                    m.get("sender", ""), m.get("subject", ""), m.get("rfc_message_id", ""))
        email.sender = email.sender or m.get("sender", "")
        if m["account"] not in email.gmail:
            email.gmail.append(m["account"])
        source = "gmail:" + m["account"]
        for a in m.get("attachments") or []:
            if not a.get("name"):
                continue
            _add_file(email, source, counts.setdefault(email.key, {}),
                      _File(a["name"], int(a.get("size") or 0), mime=str(a.get("mime") or ""),
                            gmail_account=m["account"], gmail_message=m["message_id"],
                            gmail_attachment=str(a.get("attachment_id") or ""), inline=bool(a.get("inline")),
                            data=a.get("data")))
    return list(emails.values())


# --- finding ----------------------------------------------------------------------

def _problem_of(error: BaseException) -> str:
    if isinstance(error, bridge.BridgeError):
        return _APPLE_SEARCH_WHY.get(str(error), "it couldn't be read just now")
    return "it couldn't be reached just now"


async def _search_apple(terms, since, until) -> tuple[list[dict], bool]:
    return await bridge.read_attachment_index(terms, since, until, [], limit=_APPLE_LIMIT)


async def _search_gmail(gmail_source, account: str, terms, start, end, limit: int) -> list[dict]:
    return await asyncio.wait_for(gmail_source.search_attachments(account, terms, start, end, limit=limit),
                                  _GMAIL_SEARCH_S)


def _pick_accounts(connected: dict, account: str | None) -> tuple[list[str], str]:
    names = sorted(connected)
    wanted = str(account or "").strip().casefold()
    if not wanted:
        return names, ""
    chosen = [n for n in names if wanted in n.casefold()]
    if chosen:
        return chosen, ""
    return names, f"No connected Gmail account matches {_pretty(account, 40)!r}, so all of them were searched."


# --- reading one file -------------------------------------------------------------

@dataclass
class _Reading:
    type_text: str = ""
    where: str = ""
    note: str = ""
    text: str = ""
    problem: str = ""
    warnings: list[str] = field(default_factory=list)
    risk_level: str = ""        # "high", "caution" or ""
    risk: list[str] = field(default_factory=list)
    partial: bool = False       # some of the file was not read, so "Read the whole file." must not be said


@dataclass
class _Ctx:
    deadline: float
    gmail_source: object
    gmail_accounts: list[str]
    passes: object
    client: object = None
    model: object = None
    question: str | None = None
    pages: str | None = None
    sheet: str | None = None
    lookups: dict = field(default_factory=dict)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


class _Unreadable(Exception):
    """A plain reason a file couldn't be read, for the person."""


async def _gmail_message(ctx: _Ctx, email: _Email) -> dict | None:
    """The email as Gmail has it, found by its Message-ID on each connected account."""
    if not email.rfc or email.rfc.lstrip("-").isdigit():
        return None
    if email.key in ctx.lookups:
        return ctx.lookups[email.key]
    found = None
    failed = False
    for account in ctx.gmail_accounts:
        try:
            found = await asyncio.wait_for(ctx.gmail_source.find_by_rfc_id(account, email.rfc),
                                           max(1.0, min(20.0, ctx.remaining())))
        except Exception:  # noqa: BLE001 — one account failing must not stop the next
            failed = True
            continue
        if found:
            break
    if found is None and failed:
        raise _Unreadable("Gmail couldn't be reached")
    ctx.lookups[email.key] = found
    return found


def _same_file(file: _File, others: list[dict]) -> dict | None:
    """The same file in Gmail's list: the nth of that name, so two `scan.pdf`s stay apart."""
    base = file.name.casefold()
    same = [item for item in others if str(item.get("name") or "").casefold() == base]
    return same[file.nth - 1] if 0 < file.nth <= len(same) else None


async def _fetch(ctx: _Ctx, email: _Email, f: _File) -> tuple[bytes, str]:
    """(bytes, where they came from). Raises _Unreadable with a plain reason."""
    reasons: list[str] = []
    if f.has_local and f.on_disk:
        try:
            data, _name, why = await bridge.read_attachment(f.rowid, f.attachment_id,
                                                            timeout=max(1.0, min(45.0, ctx.remaining())))
        except bridge.BridgeError as error:
            data, why = None, str(error)
        if data is not None:
            f.read_account = f.account
            return data, "Mail's saved copy"
        if why == "too_large":
            raise _Unreadable(_LOCAL_WHY["too_large"][0].upper() + _LOCAL_WHY["too_large"][1:] + ".")
        reasons.append(_LOCAL_WHY.get(why, "Mail's saved copy couldn't be fetched"))
    elif f.has_local:
        reasons.append(_LOCAL_WHY["not_on_disk"])
    if f.has_gmail:
        gmail_file = f
    else:
        gmail_file = None
        if ctx.gmail_accounts:
            try:
                message = await _gmail_message(ctx, email)
            except _Unreadable:
                message = None
                reasons.append("Gmail couldn't be reached. Sign in to Gmail again in Settings")
            if message:
                item = _same_file(f, message.get("attachments") or [])
                if item:
                    gmail_file = _File(f.name, int(item.get("size") or 0), gmail_account=message["account"],
                                       gmail_message=message["message_id"],
                                       gmail_attachment=str(item.get("attachment_id") or ""), data=item.get("data"))
    if gmail_file is not None:
        f.read_account = gmail_file.gmail_account
        if gmail_file.data is not None:
            return gmail_file.data, "Gmail"
        if gmail_file.size > ctx.gmail_source.MAX_ATTACHMENT_BYTES:
            raise _Unreadable("It is larger than Wisp can read (32 MB).")
        try:
            data = await asyncio.wait_for(
                ctx.gmail_source.attachment_bytes(gmail_file.gmail_account, gmail_file.gmail_message,
                                                  gmail_file.gmail_attachment),
                max(1.0, min(_FILE_TIMEOUT_S, ctx.remaining())))
            return data, "Gmail"
        except (TimeoutError, asyncio.TimeoutError, httpx.HTTPError) as error:  # noqa: PERF203
            reasons.append("Gmail didn't answer in time" if isinstance(error, (TimeoutError, asyncio.TimeoutError))
                           else "Gmail couldn't be reached")
        except Exception as error:  # noqa: BLE001 — GmailError and anything odd: a reason, never a crash
            from service.gmail_source import GmailError
            reasons.append("Gmail couldn't give it up. Sign in to Gmail again in Settings"
                           if isinstance(error, GmailError) else "Gmail couldn't give it up")
    elif not ctx.gmail_accounts:
        reasons.append("Gmail isn't connected")
    else:
        reasons.append("Gmail doesn't have it")
    sentence = ", and ".join(r[0].lower() + r[1:] if i else r[0].upper() + r[1:]
                             for i, r in enumerate(reasons or ["It couldn't be fetched"])).rstrip(".") + "."
    if any("downloaded" in r for r in reasons):
        sentence += " Open the email in Mail to download it, or sign in to Gmail."
    raise _Unreadable(sentence)


def _inspect_text(result: dict) -> tuple[str, list[str]]:
    """(plain description of what an inspected file is, warnings). Every field is untrusted."""
    lines = []
    real = _pretty(result.get("real_type"), 80)
    lines.append(f"Its bytes say it is {real}." if real else "Its bytes don't match a type Wisp recognises.")
    urls = _strs(result.get("urls"), 8, 200)
    if urls:
        lines.append("Web addresses in it: " + ", ".join(urls))
    entries = _strs(result.get("entries"), 40, 100)
    if entries:
        count = result.get("entry_count")
        total = count if type(count) is int and count >= 0 else len(entries)
        lines.append(f"Archive listing ({total} entries): " + ", ".join(entries[:20])
                     + (" ..." if total > 20 else ""))
    risky = _strs(result.get("risky_entries"), 12, 100)
    if risky:
        lines.append("Programs or scripts inside it: " + ", ".join(risky))
    if result.get("encrypted") is True:
        lines.append("It is password-protected.")
    return "\n".join(lines), _strs(result.get("warnings"), 6, _MAX_NOTE)


def _file_problem(error: BaseException) -> str:
    """Only Wisp's own sentences: a parser's message could quote text from the file."""
    text = _pretty(str(error), 200)
    return text if isinstance(error, ValueError) and text.startswith(("That ", "Wisp ")) else ""


# --- remembered readings ---------------------------------------------------------------

def _gmail_locator(f: _File) -> str:
    """A stable name for a Gmail file: Gmail message ids and their attachments never change, and
    the name and size are part of it. '' for anything else."""
    if f.has_gmail:
        return f"gmail:{f.gmail_account}:{f.gmail_message}:{f.nth}:{f.size}:{f.name}"[:300]
    return ""


def _locator(f: _File) -> str:
    """Where to look a saved reading up before fetching. Only a Gmail-only file has one: Apple
    Mail's row ids can be renumbered by a rebuild, so a file saved on this Mac is always fetched
    (instantly) and recognised by its bytes."""
    if f.has_local and f.on_disk:
        return ""
    return _gmail_locator(f)


def _meta_of(reading: _Reading) -> str:
    """What the inspection said, kept beside the text. Every field is Wisp's own wording."""
    return json.dumps({"type": reading.type_text, "warnings": reading.warnings, "risk_level": reading.risk_level,
                       "risk": reading.risk, "cover": reading.note,
                       "partial": reading.partial})


def _apply_cover(reading: _Reading, note: str) -> None:
    """The saved cover note only: how the text was got. The type and the risk always come from
    inspecting the bytes in hand."""
    try:
        meta = json.loads(note) if note else {}
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    cover = meta.get("cover")
    cover = _pretty(cover, 300) if isinstance(cover, str) else ""
    reading.note = cover + " " if cover else ""
    reading.partial = meta.get("partial") is True


def _apply_meta(reading: _Reading, note: str) -> None:
    """A Gmail-locator recall has no bytes to inspect: the saved type and risk are used. The locator
    holds the name, so it is the same file under the same name."""
    try:
        meta = json.loads(note) if note else {}
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    kind = meta.get("type")
    reading.type_text = _pretty(kind, 80) if isinstance(kind, str) else ""
    reading.warnings = _strs(meta.get("warnings"), 6, _MAX_NOTE)
    reading.risk = _strs(meta.get("risk"), 5, _MAX_NOTE)
    reading.risk_level = meta.get("risk_level") if meta.get("risk_level") in ("high", "caution") else ""
    _apply_cover(reading, note)


async def _recall(locator: str = "", digest: str = ""):
    """The saved reading for a file, by where it is or by its bytes. None when there isn't one."""
    from service import attachment_store

    def look():
        found = digest or (attachment_store.locate(locator) if locator else None)
        return attachment_store.get(found) if found else None
    try:
        return await asyncio.to_thread(look)
    except Exception:  # noqa: BLE001 — the store is a convenience; without it the file is simply read
        return None


async def _keep(f: _File, digest: str, *, kind: str, text: str, cut: bool, how: str, reading: _Reading) -> None:
    from service import attachment_store
    account, meta = f.read_account, _meta_of(reading)
    # Only bytes that really came from this Gmail file may be filed under its Gmail locator.
    locator = _gmail_locator(f) if account and account == f.gmail_account else ""

    def save():
        if attachment_store.put(digest, kind=kind, text=text, cut=cut, note=meta, how=how, account=account) \
                and locator:
            attachment_store.remember(locator, digest, account=account)
    try:
        await asyncio.to_thread(save)
    except Exception:  # noqa: BLE001
        pass


async def _shape(ctx: _Ctx, reading: _Reading, text: str, cut: bool, name: str, kind: str, budget: int,
                 description: str = "") -> _Reading:
    """Text to the budget. `kind` "ocr" is a picture's recognised text, shown with what the picture is."""
    from service import large_document
    picture = kind == "ocr"
    room = max(120, budget - (len(description) + 60 if picture else 0))
    reading = await _finish_text(ctx, reading, text, cut, name, "txt" if picture else kind, room, large_document)
    if picture and not reading.problem and reading.text.strip():
        reading.text = ("Text in the picture:\n" + reading.text
                        + (f"\n\nWhat the picture shows: {description}" if description else ""))
    return reading


async def _settle(ctx: _Ctx, reading: _Reading, f: _File, digest: str, text: str, cut: bool, name: str, kind: str,
                  budget: int, *, how: str = "text", description: str = "", keep: bool = True) -> _Reading:
    """Keep freshly read text for follow-ups (unless it is a partial reading that a retry could
    improve), then shape it for this answer."""
    if keep and str(text or "").strip() and digest:
        await _keep(f, digest, kind=kind, text=text, cut=cut, how=how, reading=reading)
    return await _shape(ctx, reading, text, cut, name, kind, budget, description)


# --- on-device text recognition ----------------------------------------------------------

class _Recognised(NamedTuple):
    text: str
    read: int               # pages with recognised text
    total: int              # pages in the file
    image: str              # the first prepared picture
    confidence: float       # the lowest page confidence that was accepted
    complete: bool          # every prepared page was tried (none skipped for a failure or for time)


def _exactness(confidence: float) -> str:
    """Wisp's own words for how sure the recognition was."""
    return "exactly on this Mac" if confidence >= _OCR_SURE_CONFIDENCE else "on this Mac; some words may be wrong"


async def _recognised(ctx: _Ctx, name: str, data: bytes, *, paged: bool) -> _Recognised | None:
    """The app's text recognition of a picture or the pages of a scan.

    None means "read it the old way": the app isn't there, the picture isn't text, or anything
    went wrong. Only JPEGs the child re-encoded from decoded pixels go to the app. A scanned PDF's
    text is `[Pages: total]` then a `[Page n]` block per page that had words."""
    from service import documents
    from service.assistant.hub import hub
    global _ocr_resting_until
    if not hub.has_subscribers or ctx.remaining() < 15.0 or time.monotonic() < _ocr_resting_until:
        return None
    try:
        result = await documents.run_attachment_child("ocr_images", name, data,
                                                      timeout=max(1.0, min(60.0, ctx.remaining())))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — the vision path gives the file its real reason
        return None
    result = result if isinstance(result, dict) else {}
    images = [u for u in result.get("images") or [] if isinstance(u, str) and u.startswith("data:image/jpeg;base64,")]
    if not images:
        return None
    numbers = [n for n in result.get("read_pages") or [] if type(n) is int and n > 0]
    if len(numbers) != len(images):
        numbers = list(range(1, len(images) + 1))
    total = result.get("pages")
    total = total if type(total) is int and total >= len(images) else len(images)
    parts: list[tuple[int, str, float]] = []
    attempted = 0
    for number, url in zip(numbers, images):
        if ctx.remaining() < 10.0:
            break
        page_s = max(1.0, min(_OCR_PAGE_S, ctx.remaining() - 5.0))
        try:
            jpeg = base64.b64decode(url.split(",", 1)[1], validate=True)
            text, confidence = await bridge.recognize_text(jpeg, timeout=page_s)
        except (bridge.BridgeError, ValueError) as error:
            # An older app drops the request, and a stalled one never answers: don't make every
            # picture in the next few minutes wait for it. A wait that the overall deadline cut
            # short says nothing about the app.
            if str(error) == "app_not_connected" or (str(error) == "recognize_timeout" and page_s >= _OCR_PAGE_S):
                _ocr_resting_until = time.monotonic() + _OCR_REST_S
            if not parts:
                return None
            break
        attempted += 1
        text = _UNPRINTABLE.sub(" ", str(text or "")).strip()
        if text and confidence >= _OCR_MIN_CONFIDENCE:
            parts.append((number, text, float(confidence)))
    if sum(len(re.sub(r"\s", "", text)) for _n, text, _c in parts) < _OCR_MIN_CHARS:
        return None
    joined = (f"[Pages: {total}]\n" + "\n\n".join(f"[Page {n}]\n{text}" for n, text, _c in parts)
              if paged else parts[0][1])
    return _Recognised(joined, len(parts), total, images[0], min(c for _n, _t, c in parts),
                       attempted == len(images) and result.get("stopped_early") is not True)


async def _describe(ctx: _Ctx, image: str) -> str:
    """What a picture of text is and how it is laid out, from the vision model. '' when that can't be had."""
    from service import vision
    if ctx.remaining() < 20.0:
        return ""
    try:
        text = await vision.read_images(ctx.question or "Describe this picture.", [image],
                                        what="a picture attached to an email", client=ctx.client, model=ctx.model,
                                        deadline=ctx.deadline, describe_only=True)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — the words are already in hand; the description is extra
        return ""
    return re.sub(r"^\s*DESCRIPTION:\s*", "", str(text or ""), flags=re.IGNORECASE).strip()[:400]


# --- invites and contact cards -----------------------------------------------------------

def _structured_text(result) -> tuple[str, str]:
    """(plain lines, "calendar" | "contacts" | ""). Every value is untrusted and goes in the fence."""
    if not isinstance(result, dict):
        return "", ""
    one = lambda value, limit=200: _pretty(value, limit) if isinstance(value, str) else ""  # noqa: E731
    lines: list[str] = []
    if result.get("kind") == "calendar":
        events = [e for e in result.get("events") or [] if isinstance(e, dict)]
        if not events:
            return "", ""
        count = result.get("event_count")
        count = count if type(count) is int and count >= len(events) else len(events)
        method = one(result.get("method"), 20).upper()
        said = {"REQUEST": "an invitation", "CANCEL": "a cancellation", "REPLY": "a reply to an invitation",
                "PUBLISH": "a published calendar", "COUNTER": "a proposed new time"}.get(method, "a calendar file")
        lines.append(f"This is {said} with {count} event{'' if count == 1 else 's'}"
                     + (f" (the first {len(events)} shown)." if count > len(events) else "."))
        for index, event in enumerate(events, 1):
            lines.append(f"Event {index}: {one(event.get('summary')) or '(no title)'}")
            start, end, zone = one(event.get("start"), 40), one(event.get("end"), 40), one(event.get("timezone"), 60)
            if start:
                when = start + (f" to {end}" if end and end != start else "")
                when += " (all day)" if event.get("all_day") is True else (f" ({zone})" if zone else "")
                lines.append(f"  When: {when}")
            for label, key, limit in (("Where", "location", 200), ("Organizer", "organizer", 120),
                                      ("Repeats", "repeats", 120), ("Status", "status", 30)):
                value = one(event.get(key), limit)
                if value:
                    lines.append(f"  {label}: {value}")
            people = _strs(event.get("attendees"), 12, 100)
            if people:
                total = event.get("attendee_count")
                total = total if type(total) is int and total >= len(people) else len(people)
                lines.append(f"  Invited ({total}): " + ", ".join(people))
            details = one(event.get("description"), 600)
            if details:
                lines.append(f"  Details: {details}")
        return "\n".join(lines), "calendar"
    if result.get("kind") == "contacts":
        contacts = [c for c in result.get("contacts") or [] if isinstance(c, dict)]
        if not contacts:
            return "", ""
        count = result.get("contact_count")
        count = count if type(count) is int and count >= len(contacts) else len(contacts)
        lines.append(f"A contact card with {count} contact{'' if count == 1 else 's'}"
                     + (f" (the first {len(contacts)} shown)." if count > len(contacts) else "."))
        for index, contact in enumerate(contacts, 1):
            lines.append(f"Contact {index}: {one(contact.get('name')) or '(no name)'}")
            for label, key in (("Organization", "organization"), ("Title", "title")):
                value = one(contact.get(key), 120)
                if value:
                    lines.append(f"  {label}: {value}")
            for label, key in (("Phone", "phones"), ("Email", "emails"), ("Address", "addresses")):
                values = _strs(contact.get(key), 6, 160)
                if values:
                    lines.append(f"  {label}: " + "; ".join(values))
            note = one(contact.get("note"), 300)
            if note:
                lines.append(f"  Note: {note}")
        return "\n".join(lines), "contacts"
    return "", ""


async def _read_file(ctx: _Ctx, email: _Email, f: _File, budget: int) -> _Reading:
    """Fetch, inspect and read one file. Never raises."""
    from service import attachment_store, documents, vision
    from service import large_document
    reading = _Reading()
    name = _safe_name(f.name)
    suffix = _suffix(name)
    try:
        if ctx.remaining() < 2.0:
            raise _Unreadable("There wasn't time left to read it.")
        locator = _locator(f)
        stored = await _recall(locator=locator) if locator else None
        if stored is not None:
            # A Gmail file seen before, under the same name: nothing to fetch. (Apple Mail's files
            # are never found by a row id, which a rebuild of Mail's index can hand to another file.)
            _apply_meta(reading, stored.note)
            reading.where = _SAVED
            return await _shape(ctx, reading, stored.text, stored.cut, name, stored.kind, budget)
        data, where = await _fetch(ctx, email, f)
        reading.where = where
        if not data:
            raise _Unreadable("The file is empty.")
        digest = attachment_store.digest(data)
        info = await documents.run_attachment_child("inspect", name, data,
                                                    timeout=max(1.0, min(30.0, ctx.remaining())))
        info = info if isinstance(info, dict) else {}
        reading.type_text = _pretty(info.get("real_type"), 80)
        reading.warnings = _strs(info.get("warnings"), 6, _MAX_NOTE)
        risk = info.get("risk") if isinstance(info.get("risk"), dict) else {}
        reading.risk = _strs(risk.get("reasons"), 5, _MAX_NOTE)
        reading.risk_level = risk.get("level") if risk.get("level") in ("high", "caution") and reading.risk else ""
        family = str(info.get("family") or "")
        inspect_only = (family in ("program", "archive") or suffix in _PROGRAM_NAMES or suffix in _ARCHIVE_NAMES
                        or (family == "zip" and suffix not in _OFFICE_ZIPPED))
        if inspect_only:
            reading.text, _ = _inspect_text(info)
            reading.note = "Inspected only. Wisp never opens, unpacks or runs programs, scripts or archives."
            return reading
        stored = await _recall(digest=digest)
        if stored is not None:
            # The same bytes read before (this file again, or another email's copy): no second read.
            # The type and the risk are this file's own, from the inspection above; only the text,
            # how it was got and how much of it there is come from the saved reading.
            _apply_cover(reading, stored.note)
            if stored.text.strip() and ENCRYPTED_PDF_REASON in reading.risk:
                reading.risk = [reason for reason in reading.risk if reason != ENCRYPTED_PDF_REASON]
                reading.risk_level = reading.risk_level if reading.risk else ""
            if _gmail_locator(f) and f.read_account == f.gmail_account:
                await _keep(f, digest, kind=stored.kind, text=stored.text, cut=stored.cut, how=stored.how,
                            reading=reading)
            return await _shape(ctx, reading, stored.text, stored.cut, name, stored.kind, budget)
        if suffix in _STRUCTURED_NAMES and family not in ("program", "archive", "zip", "image", "video"):
            try:
                result = await documents.run_attachment_child("structured", name, data,
                                                              timeout=max(1.0, min(30.0, ctx.remaining())))
            except ValueError:
                result = None           # not an invite or card after all: read as plain text below
            text, what = _structured_text(result)
            if text:
                reading.text = text
                reading.note = ("Read as a calendar invite. Nothing was added to the calendar." if what == "calendar"
                                else "Read as a contact card. Nothing was added to Contacts.")
                return reading
        if family == "script" or suffix in _TEXT_NAMES:
            stem = PurePath(name).stem or "file"
            text, cut = await documents.extract_async(f"{stem}.txt", data, large=True,
                                                      timeout=max(1.0, min(45.0, ctx.remaining())))
            script = family == "script" or suffix in _SCRIPT_NAMES
            reading.note = ("Read as plain text; the script was not run. " if script else "Read as plain text. ")
            return await _settle(ctx, reading, f, digest, text, cut, name, "txt", budget)
        if family == "video":
            raise _Unreadable("It is a video, and Wisp doesn't read videos in email.")
        if _is_picture(name, f.mime) or (family == "image" and suffix not in _DOCUMENT_NAMES):
            recognised = await _recognised(ctx, name, data, paged=False)
            if recognised:
                description = await _describe(ctx, recognised.image)
                reading.note = f"Its text was recognised {_exactness(recognised.confidence)}. "
                return await _settle(ctx, reading, f, digest, recognised.text, False, name, "ocr", budget,
                                     how="recognized", description=description)
            result = await documents.run_attachment_child("tiles", name, data,
                                                          timeout=max(1.0, min(45.0, ctx.remaining())))
            images = [u for u in (result or {}).get("images") or [] if isinstance(u, str)]
            if not images:
                raise _Unreadable("That picture couldn't be read.")
            tiled = (result or {}).get("tiled") is True
            text = await vision.read_images(ctx.question or "Describe this picture and read any text in it.",
                                            images, what="a picture attached to an email", sections=tiled,
                                            client=ctx.client, model=ctx.model, deadline=ctx.deadline)
            reading.text = str(text or "")
            reading.note = ("Read the picture in an overview and zoomed sections." if tiled
                            else "Read the whole picture.")
            return reading
        if suffix in _OLD_OFFICE:
            raise _Unreadable(f"It is an old .{suffix} file, which Wisp can't open. "
                              f"Saving it as .{_OLD_OFFICE[suffix]} first would work.")
        if suffix in _DOCUMENT_NAMES or family == "document":
            extract_name = f"{PurePath(name).stem or 'file'}.{_MACRO_MAP.get(suffix, suffix)}"
            try:
                text, cut = await documents.extract_async(extract_name, data, large=True,
                                                          timeout=max(1.0, min(45.0, ctx.remaining())))
            except ValueError as error:
                if suffix != "pdf" or str(error) != _SCANNED_PDF:
                    raise
                text, cut = "", False       # a scan: its pages are pictures, read below
            if text.strip() and ENCRYPTED_PDF_REASON in reading.risk:
                # It opened without a password and was read, so there is nothing left unchecked.
                reading.risk = [reason for reason in reading.risk if reason != ENCRYPTED_PDF_REASON]
                reading.risk_level = reading.risk_level if reading.risk else ""
            if not text.strip() and suffix == "pdf":
                recognised = await _recognised(ctx, name, data, paged=True)
                if recognised:
                    read, total = recognised.read, recognised.total
                    exact = _exactness(recognised.confidence)
                    reading.note = (f"A scanned PDF: its text was recognised {exact.split(';')[0]}, from {read} of its "
                                    f"{total} page{'' if total == 1 else 's'}"
                                    + ("; some words may be wrong" if ";" in exact else "") + ". ")
                    reading.partial = read < total
                    if not recognised.complete:
                        reading.note += "Recognition stopped early, so Wisp will try the rest next time. "
                    return await _settle(ctx, reading, f, digest, recognised.text, read < total, name, "pdf",
                                         budget, how="recognized", keep=recognised.complete)
                result = await documents.run_attachment_child("pdf_pages", name, data,
                                                              timeout=max(1.0, min(45.0, ctx.remaining())))
                images = [u for u in (result or {}).get("images") or [] if isinstance(u, str)]
                if not images:
                    raise _Unreadable("That PDF has no text and no pages Wisp can read.")
                text = await vision.read_images(
                    ctx.question or "Read the text on these pages and say what the document is.", images,
                    what="the scanned pages of a PDF attached to an email", client=ctx.client, model=ctx.model,
                    deadline=ctx.deadline)
                reading.text = str(text or "")
                reading.note = (f"A scanned PDF with no text: read the pictures of its first {len(images)} "
                                f"page{'' if len(images) == 1 else 's'} of {(result or {}).get('pages', '?')}.")
                return reading
            kind = _MACRO_MAP.get(suffix, suffix)
            if suffix == "pdf" and text.strip():
                declared, with_text = _page_facts(large_document, text)
                if declared and 0 < with_text < declared:
                    reading.partial = True
                    reading.note = (f"{with_text} of its {declared} pages have text Wisp could read; "
                                    "the rest look like pictures. ")
            return await _settle(ctx, reading, f, digest, text, cut, name, kind, budget)
        raise _Unreadable("Wisp can't read this kind of file."
                          + (f" Its bytes say it is {reading.type_text}." if reading.type_text else ""))
    except _Unreadable as error:
        reading.problem = str(error)
    except (bridge.BridgeError, vision.VisionUnavailable, TimeoutError, asyncio.TimeoutError, ValueError,
            httpx.HTTPError) as error:
        if isinstance(error, vision.VisionUnavailable):
            reading.problem = _LOCAL_ONLY
        elif isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            reading.problem = "Reading it took too long."
        elif isinstance(error, bridge.BridgeError):
            reading.problem = "Mail couldn't be asked just now."
        else:
            reading.problem = _file_problem(error) or "It couldn't be read."
    except Exception as error:  # noqa: BLE001 — Gmail errors and anything unexpected: one file, one reason
        from service.gmail_source import GmailError
        reading.problem = ("Gmail couldn't give it up. Sign in to Gmail again in Settings."
                           if isinstance(error, GmailError) else "It couldn't be read.")
    return reading


def _select(large_document, text: str, kind: str, pages, sheet, cut):
    """`large_document.select`, told whether the extraction was cut short when it can be told."""
    select = large_document.select
    try:
        takes_cut = "cut" in inspect.signature(select).parameters
    except (TypeError, ValueError):
        takes_cut = False
    if takes_cut:
        return select(text, kind, pages=pages, sheet=sheet, cut=bool(cut))
    return select(text, kind, pages=pages, sheet=sheet)


def _page_facts(large_document, text: str) -> tuple[int | None, int]:
    """(pages the PDF declares, pages that have text), (None, 0) when that can't be told."""
    facts = getattr(large_document, "page_facts", None)
    if facts is None:
        return None, 0
    try:
        declared, with_text = facts(text)
    except Exception:  # noqa: BLE001 — a nicety; without it the reading is simply not annotated
        return None, 0
    return (declared if type(declared) is int and declared > 0 else None,
            with_text if type(with_text) is int and with_text >= 0 else 0)


async def _finish_text(ctx: _Ctx, reading: _Reading, text: str, cut: bool, name: str, kind: str, budget: int,
                       large_document) -> _Reading:
    from service import documents
    text = str(text or "")
    if not text.strip():
        reading.problem = "There was no text in it to read."
        return reading
    part = False
    if ctx.pages or ctx.sheet:
        chosen = _select(large_document, text, kind, ctx.pages, ctx.sheet, cut)
        names = _strs(getattr(chosen, "available", None), 20, 60)
        if chosen.problem and not str(chosen.text or "").strip():
            if not names:
                reading.problem = _pretty(chosen.problem, _MAX_NOTE)
                return reading
            # No such sheet: say what there is. The names come from the file, so they go in the fence.
            reading.note += _pretty(chosen.problem, _MAX_NOTE) + " Its sheets are listed instead."
            reading.text = "Sheets in this file: " + ", ".join(names)
            return reading
        part = not chosen.problem and bool(chosen.note)
        text = str(chosen.text or "")
        said = " ".join(_pretty(x, _MAX_NOTE) for x in (chosen.problem, chosen.note) if x)
        reading.note += said + " " if said else ""
    client, model = ctx.client, ctx.model

    async def chat(messages: list[dict], max_tokens: int) -> str:
        return await documents.local_chat(client, model, messages, max_tokens, ctx.deadline)

    condensed = await large_document.condense(text, name=name, kind=kind, question=ctx.question,
                                              budget=budget, chat=chat, passes=ctx.passes,
                                              extraction_cut=bool(cut))
    reading.text = condensed.text
    whole = "" if reading.partial and not part else ("All of that part is shown." if part else "Read the whole file.")
    reading.note += condensed.note or (whole if condensed.complete else "")
    return reading


# --- output -----------------------------------------------------------------------

def _when(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "unknown date"


def _source_label(email: _Email) -> str:
    parts = (["Apple Mail"] if email.apple else []) + [f"Gmail ({_pretty(a, 60)})" for a in email.gmail]
    return " + ".join(parts) or "unknown"


def _email_header(index: int, email: _Email) -> str:
    return (f"Email {index}: from {_pretty(email.sender, 80) or 'unknown sender'} | "
            f"subject: {_pretty(email.subject, 100) or '(no subject)'} | {_when(email.ts)} | "
            f"source: {_source_label(email)}")


def _availability(f: _File) -> str:
    if f.has_local and f.on_disk:
        return "saved on this Mac"
    if f.has_gmail:
        return "in Gmail"
    return "not downloaded"


def _file_head(f: _File, reading: _Reading | None = None) -> str:
    name = _safe_name(f.name)
    kind = (reading.type_text if reading and reading.type_text else "") or (_suffix(name).upper() + " file"
                                                                             if _suffix(name) else "file")
    size = f", {_size_text(f.size)}" if f.size else ""
    head = f"  File: {name} ({kind}{size})"
    if reading and reading.where:
        head += f", read from {reading.where}"
    return head


def _block(f: _File, reading: _Reading, room: int) -> str:
    """The text for one read file, at most `room` characters (0 = doesn't fit)."""
    lines = [_file_head(f, reading)]
    if reading.problem:
        lines.append(f"  Not read: {reading.problem}")
    else:
        lines.append(f"  Coverage: {reading.note or 'Read the whole file.'}")
    if reading.risk:
        lines.append(("  Looks risky: " if reading.risk_level == "high" else "  Take care: ") + " ".join(reading.risk))
    else:
        lines.extend(f"  Warning: {w}" for w in reading.warnings)
    base = "\n".join(lines)
    if reading.problem or not reading.text.strip():
        return base if len(base) <= room else ""
    free = room - len(base) - 1 - untrusted.overhead(_KIND)
    if free < 40:
        short = base + "\n  (No room was left to show its contents.)"
        return short if len(short) <= room else (base if len(base) <= room else "")
    text = reading.text.strip()
    if len(text) > free:
        cut = text[:free].rstrip()
        text = cut
        lines[1] = lines[1].rstrip() + " Shortened to fit."
        base = "\n".join(lines)
        free = room - len(base) - 1 - untrusted.overhead(_KIND)
        text = text[:max(0, free)]
    return base + "\n" + untrusted.fence(text, _KIND)


def _trailer(notes: list[str], room: int) -> str:
    """What was left out, whole notes only: files not read come first, since they matter most."""
    if not notes or room <= 0:
        return ""
    ordered = sorted(notes, key=lambda note: 0 if "not read" in note or "without room" in note else 1)
    kept: list[str] = []
    for note in ordered:
        candidate = "Left out: " + " ".join([*kept, note])
        if len(candidate) > room - 24:
            break
        kept.append(note)
    if not kept:
        return ""
    more = len(ordered) - len(kept)
    return "Left out: " + " ".join(kept) + (f" (+{more} more)" if more else "")


def _listing(index: int, email: _Email) -> list[str]:
    lines = [_email_header(index, email)]
    for f in email.files:
        size = f", {_size_text(f.size)}" if f.size else ""
        lines.append(f"  - {_safe_name(f.name)} ({_suffix(f.name).upper() or 'file'}{size}, {_availability(f)})")
    return lines


# --- run --------------------------------------------------------------------------

async def run(query: str | None = None, name: str | None = None, start: float | None = None,
              end: float | None = None, label: str = "", count: int = DEFAULT_COUNT,
              account: str | None = None, question: str | None = None, list_only: bool = False,
              pages: str | None = None, sheet: str | None = None) -> str:
    """Find attachments across Apple Mail and Gmail, then list or read them. Never raises.

    `pages` ("12", "3-5") and `sheet` (a tab's name or number) narrow the reading to that part."""
    shown: list[str] = []
    try:
        return await asyncio.wait_for(
            _run(query, name, start, end, label, count, account, question, list_only, shown, pages, sheet),
            DEADLINE_S + 20.0)
    except (TimeoutError, asyncio.TimeoutError):
        if len(shown) > 1:
            # Files already read are whole blocks; running out of time doesn't undo them.
            return "\n".join([*shown, "Left out: time ran out before the remaining files could be read."])
        return "Reading the attachments took too long, so nothing is shown. Try asking for fewer emails."
    except Exception:  # noqa: BLE001 — a tool result, never a crash; nothing from the files is logged
        return "I couldn't look through attachments just now."


async def _run(query, name, start, end, label, count, account, question, list_only, shown: list[str],
               pages=None, sheet=None) -> str:
    from service import gmail_source
    deadline = time.monotonic() + DEADLINE_S
    cutoff = time.time() + FUTURE_HOURS * 3600
    terms = terms_of(query)
    explicit = start is not None or end is not None
    finite = lambda v: v is not None and v == v and abs(v) != float("inf")  # noqa: E731
    since = float(start) if finite(start) and start and start > 0 else 0.0
    until = float(end) if finite(end) and end and end > 0 else 0.0
    try:
        wanted = int(count) if count else DEFAULT_COUNT
    except (TypeError, ValueError):
        wanted = DEFAULT_COUNT
    wanted = max(1, min(wanted, MAX_EMAILS))
    connected = gmail_source.connected_accounts()
    accounts, account_note = _pick_accounts(connected, account)

    # Scheduled mail dated years ahead must not use up the newest-first row limit.
    jobs = [_search_apple(terms, since, until if explicit else cutoff)]
    jobs += [_search_gmail(gmail_source, a, terms, since or None, until or None, min(50, max(15, wanted * 3)))
             for a in accounts]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    problems: list[str] = []
    apple_rows: list[dict] = []
    apple_capped = False
    if isinstance(results[0], BaseException):
        if isinstance(results[0], asyncio.CancelledError):
            raise results[0]
        problems.append(f"Apple Mail's saved mail couldn't be searched: {_problem_of(results[0])}.")
    else:
        apple_rows, apple_capped = results[0]
    gmail_messages: list[dict] = []
    for acct, result in zip(accounts, results[1:]):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            problems.append(f"Gmail ({_pretty(acct, 60)}) couldn't be searched; signing in to Gmail again may fix it.")
        else:
            gmail_messages.extend(result)
    if not accounts:
        problems.append("Gmail isn't connected, so only Apple Mail's copies were searched.")
    if account_note:
        problems.append(account_note)

    emails = sorted(_merge(apple_rows, gmail_messages), key=lambda e: e.ts, reverse=True)
    notes: list[str] = list(problems)
    if not explicit:
        future = [e for e in emails if e.ts > cutoff]
        if future:
            notes.append(f"{len(future)} email{'' if len(future) == 1 else 's'} dated more than a day and a half "
                         "ahead (scheduled mail) not counted as recent.")
        emails = [e for e in emails if e.ts <= cutoff]
    named = str(name or "").strip().casefold()
    tiny = 0
    for e in emails:
        kept = []
        for f in e.files:
            if named:
                if named not in f.name.casefold():
                    continue
            elif (_is_picture(f.name, f.mime) and 0 < f.size < TINY_IMAGE_BYTES):
                tiny += 1
                continue
            kept.append(f)
        e.files = kept
    emails = [e for e in emails if e.files]
    if tiny:
        notes.append(f"{tiny} small picture{'' if tiny == 1 else 's'} under 20 KB (logos and icons) skipped; "
                     "name one to read it.")
    if apple_capped:
        notes.append("Apple Mail had more attachments than were listed; narrow the request to see others.")
    where = f" in {label}" if label else ""
    if not emails:
        what = f" matching {', '.join(terms)}" if terms else ""
        named_text = f" named {_pretty(name, 60)!r}" if named else ""
        return (f"I didn't find any email attachments{named_text}{what}{where}. "
                + " ".join(notes)).strip()

    chosen = emails[:wanted]
    if len(emails) > len(chosen):
        notes.append(f"{len(emails) - len(chosen)} more email{'' if len(emails) - len(chosen) == 1 else 's'} "
                     f"with attachments (showing {len(chosen)}, newest first).")
    top = f"Attachments in {len(chosen)} email{'' if len(chosen) == 1 else 's'}{where}, newest first."
    limit = RESULT_BUDGET_CHARS

    if list_only:
        return _list_result(top, chosen, notes, limit)

    # Reading starts here: the local-model check comes before any byte is fetched.
    from service import documents, large_document, vision
    try:
        client, model = documents.local_model()
    except (vision.VisionUnavailable, ValueError):
        return _LOCAL_ONLY
    except Exception:  # noqa: BLE001
        return _LOCAL_ONLY

    ctx = _Ctx(deadline=deadline, gmail_source=gmail_source, gmail_accounts=accounts,
               passes=large_document.PassBudget(deadline=deadline), client=client, model=model,
               question=(str(question).strip()[:300] if question else None),
               pages=(str(pages).strip()[:60] or None) if isinstance(pages, (str, int)) and not isinstance(pages, bool)
               else None,
               sheet=(str(sheet).strip()[:80] or None) if isinstance(sheet, (str, int)) and not isinstance(sheet, bool)
               else None)
    flat = [(e, f) for e in chosen for f in e.files]
    to_read = flat[:MAX_READS]
    if len(flat) > len(to_read):
        notes.append(f"{len(flat) - len(to_read)} more file{'' if len(flat) - len(to_read) == 1 else 's'} not read "
                     f"(at most {MAX_READS} per request).")
    pieces = shown
    pieces.append(top)
    used = len(top)
    announced: set[str] = set()
    unshown = 0
    for position, (email, f) in enumerate(to_read):
        index = chosen.index(email) + 1
        head = "" if email.key in announced else _email_header(index, email)
        room_total = limit - _RESERVE - used - 1
        share = room_total // (len(to_read) - position)
        # What the file's own lines and the fence take, estimated before the read.
        content_budget = max(120, min(room_total, share) - len(head) - 380)
        reading = await _read_file(ctx, email, f, content_budget)
        block = _block(f, reading, room_total - (len(head) + 1 if head else 0))
        if not block:
            unshown += 1
            continue
        if head:
            announced.add(email.key)
            pieces.append(head)
            used += len(head) + 1
        pieces.append(block)
        used += len(block) + 1
    if unshown:
        notes.append(f"{unshown} file{'' if unshown == 1 else 's'} read but without room to show "
                     "the result.")
    trailer = _trailer(notes, limit - used - 1)
    if trailer:
        pieces.append(trailer)
    return "\n".join(pieces)


def _list_result(top: str, chosen: list[_Email], notes: list[str], limit: int) -> str:
    pieces = [top + " Nothing was opened."]
    used = len(pieces[0])
    hidden = 0
    for index, email in enumerate(chosen, 1):
        block = "\n".join(_listing(index, email))
        if used + len(block) + 1 > limit - _RESERVE:
            hidden += 1
            continue
        pieces.append(block)
        used += len(block) + 1
    if hidden:
        notes.append(f"{hidden} more email{'' if hidden == 1 else 's'} not listed for lack of room.")
    trailer = _trailer(notes, limit - used - 1)
    if trailer:
        pieces.append(trailer)
    return "\n".join(pieces)


# --- view_emails annotation -------------------------------------------------------

def _has_line(row: dict) -> bool:
    return str(row.get("body") or "").lstrip().startswith("[Attached:")


async def _annotation_lines(ids: list[str], timeout: float) -> dict[str, str]:
    from service import gmail_source
    rows, _capped = await bridge.read_attachment_index([], 0, 0, ids, limit=bridge.ATTACHMENT_LIMIT_MAX,
                                                       timeout=timeout)
    by_id: dict[str, list[tuple[str, int]]] = {}
    for r in rows:
        if _is_picture(r["name"]) and 0 < r["size"] < TINY_IMAGE_BYTES:
            continue
        by_id.setdefault(bridge.norm_rfc(r["rfc_message_id"]), []).append((r["name"], r["size"]))
    return {key: gmail_source.attached_line(items) for key, items in by_id.items() if items}


async def annotate_rows(rows: list[dict], timeout: float = 1.5) -> list[dict]:
    """Put `[Attached: ...]` at the start of `body` for rows whose message Apple Mail's index
    knows has attachments. Best effort: the rows come back unchanged (the same list) when the
    app isn't connected, nothing matches, it is slow, or anything fails."""
    try:
        from service.assistant.hub import hub
        if not rows or not hub.has_subscribers:
            return rows
        ids: list[str] = []
        for row in rows:
            key = bridge.norm_rfc(row.get("message_id"))
            if key and not key.lstrip("-").isdigit() and not _has_line(row) and key not in ids:
                ids.append(key)
        ids = ids[:bridge.ATTACHMENT_RFC_IDS_MAX // 2]
        if not ids:
            return rows
        lines = await asyncio.wait_for(_annotation_lines(ids, timeout), timeout)
        if not lines:
            return rows
        out = []
        for row in rows:
            line = lines.get(bridge.norm_rfc(row.get("message_id"))) if not _has_line(row) else None
            out.append({**row, "body": f"{line}\n{row.get('body') or ''}".rstrip()} if line else row)
        return out
    except Exception:  # noqa: BLE001 — a read of mail must never fail because of this
        return rows
