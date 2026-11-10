"""Find email attachments across Apple Mail and Gmail, and read them (Ask: read_email_attachments).
With source="messages" the same engine finds and reads files people texted (Messages only: no Apple Mail,
no Gmail). Voice memos and other recordings are transcribed by the local model, and an email attached to
an email is read (headers, body, and up to three of its files, in memory).

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
- Only when asked: `run` and `whole_text` run for the tool call, and this module never schedules or starts
  anything by itself. The one exception is the caller `index_one`, which a background reader may call:
  it uses NO language model (plain extraction and the app's on-device text recognition only), keeps only
  what it reads from a copy saved on this Mac, never downloads from Gmail, never shows anything, and a
  picture that isn't text, a scan the app can't recognise, a recording, a program, an archive or a video
  is skipped.
- An attached email is parsed in a child process, in memory; the email and its files are never written out
  as files (apart from the child's private temporary copy used to convert a picture or recording, removed
  right after), and a program, script or archive inside it is inspected only. An email inside it is listed,
  not opened. The text read from inside it is kept like any other reading, so it can be found later.
- Text that was read is kept in service/attachment_store.py (this Mac only, dropped after
  30 days unused and when its account is disconnected; texted files belong to the account "messages"),
  so a follow-up question about the same file needs no fetch and no second read. Nothing is kept for a
  file that wasn't asked for. A recording is kept only when all of it was heard. Every kept or recalled
  reading is also noted (who sent it, when) with `attachment_store.note_seen`, for finding it later.
- Text in pictures and scanned pages is recognised by the app (Apple's on-device text
  recognition) from JPEGs the child process re-encoded; the app never decodes the sender's
  bytes. Without the app, or when a picture isn't text, the vision model reads it as before.
- Local only: `documents.local_model()` is called before any byte is fetched (Messages too), and a model
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
import contextvars
import inspect
import json
import re
import time
from dataclasses import dataclass, field, replace
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
_RESTING_NOTE = "Text recognition is resting for a few minutes, so this picture was not tried yet. "
_TEXT_ONLY = "Wisp has its text but hasn't looked at the picture itself. "
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
    "h", "cpp", "swift", "go", "rs", "kt", "tex", "plist", "env", "rtx"})
_AUDIO_NAMES = frozenset({"m4a", "mp3", "wav", "caf", "aac", "ogg", "opus", "amr", "aiff", "aif"})
_NESTED_READS = 3                   # files read inside an attached email
_NESTED_LISTED = 12                 # names listed inside an attached email
_MIN_NESTED_BUDGET = 1400           # below this much room, an attached email's files are listed, not read
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


def _plain(text) -> str:
    """`text` with any fence-marker-shaped run defused, for a line printed OUTSIDE a fence that carries a
    sender, subject, chat, file or account name. Ordinary text, `Dana <dana@x.com>` included, is unchanged."""
    return untrusted.neutralize(text)


def _safe_name(name: str) -> str:
    """A file name safe to display and to pass to a child process."""
    return _plain(_pretty(PurePath(str(name or "")).name.replace("\x00", ""), 100)) or "file"


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


def _is_audio(family: str, suffix: str) -> bool:
    """A recording: the bytes say audio, or the name says so and the bytes say nothing else. A file the
    inspection calls a video is a video, whatever it is named."""
    return family == "audio" or (suffix in _AUDIO_NAMES and family in ("", "unknown"))


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
    read_source: str = ""   # where the bytes came from: "mail" or "gmail" (set by _fetch)
    message_id: str = ""    # a Messages attachment's id (digits)
    source: str = ""        # "messages" for a texted file; "" for mail
    nested: bool = False    # a file inside an attached email: its bytes are in `data`, in memory only
    about: object = None    # the _Email this file is in (set while it is read, for note_seen)

    @property
    def has_messages(self) -> bool:
        return self.source == "messages" and bool(self.message_id)

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
    source: str = "mail"    # "mail" or "messages"
    chat: str = ""          # Messages: the conversation's name
    from_me: bool = False   # Messages: the person sent it


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
    extra: str = ""             # readings of files inside this one (each with its own fence), shown after it
    cut: bool = False           # the text kept is only the start of the file (set by `_whole`)
    inspected: bool = False     # only its bytes were inspected (a program or archive): its contents were not read


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
    background: bool = False    # the background reader: a failure of text recognition sets no rest
    resting: bool = False       # set by `_recognised` when it skipped a picture only because recognition is resting
    epoch: int | None = None    # the store's erase generation, read before any byte was fetched (see `_stamp`)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


_EPOCH: contextvars.ContextVar = contextvars.ContextVar("attachment_erase_epoch", default=None)


async def _stamp(ctx: _Ctx) -> _Ctx:
    """Read the store's erase generation (off the event loop) BEFORE the request fetches bytes. Every store
    write for the reading then carries it, so a write that an erase overtook stores nothing."""
    from service import attachment_store
    read = getattr(attachment_store, "epoch", None)
    try:
        ctx.epoch = await asyncio.to_thread(read) if read is not None else None
    except Exception:  # noqa: BLE001
        ctx.epoch = None
    _EPOCH.set(ctx.epoch)
    return ctx


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
    if f.nested and f.data is not None:
        return f.data, "the attached email"
    if f.source == "messages":
        return await _fetch_messages(ctx, f)
    reasons: list[str] = []
    if f.has_local and f.on_disk:
        try:
            data, _name, why = await bridge.read_attachment(f.rowid, f.attachment_id,
                                                            timeout=max(1.0, min(45.0, ctx.remaining())))
        except bridge.BridgeError as error:
            data, why = None, str(error)
        if data is not None:
            f.read_account, f.read_source = f.account, "mail"
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
        f.read_account, f.read_source = gmail_file.gmail_account, "gmail"
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


def _recalled_note(reading: _Reading, stored) -> None:
    """A picture's saved text came from text recognition alone: no description was kept with it."""
    if getattr(stored, "kind", "") == "ocr" and getattr(stored, "how", "") == "recognized":
        reading.note = (reading.note.rstrip() + " " if reading.note.strip() else "") + _TEXT_ONLY


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


async def _keep(f: _File, digest: str, *, kind: str, text: str, cut: bool, how: str, reading: _Reading) -> bool:
    from service import attachment_store
    account, meta = f.read_account, _meta_of(reading)
    # Only bytes that really came from this Gmail file may be filed under its Gmail locator.
    locator = _gmail_locator(f) if f.read_source == "gmail" and account and account == f.gmail_account else ""

    gen = {} if _EPOCH.get() is None else {"epoch": _EPOCH.get()}

    def save():
        if attachment_store.put(digest, kind=kind, text=text, cut=cut, note=meta, how=how, account=account, **gen):
            if locator:
                attachment_store.remember(locator, digest, account=account, **gen)
            _note_seen(f, digest, **gen)
            return True
        return False
    try:
        return bool(await asyncio.to_thread(save))
    except Exception:  # noqa: BLE001
        return False


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
    image: str              # the child's small overview JPEG for the vision model; '' when it sent none
    confidence: float       # the lowest page confidence that was accepted
    complete: bool          # every prepared page was tried (none skipped for a failure or for time)


def _exactness(confidence: float) -> str:
    """Wisp's own words for how sure the recognition was."""
    return "exactly on this Mac" if confidence >= _OCR_SURE_CONFIDENCE else "on this Mac; some words may be wrong"


def _defuse(text: str) -> str:
    """Recognised text can contain lines that look like the page markers Wisp itself writes
    (`[Page 2]`, `[Pages: 99]`, `[Slide 1]`, `[Sheet: x]`); the extractors' own defusing is used."""
    from service import document_extract
    fn = getattr(document_extract, "defuse_markers", None) or document_extract._neutralise
    return fn(text)


async def _recognised(ctx: _Ctx, name: str, data: bytes, *, paged: bool) -> _Recognised | None:
    """The app's text recognition of a picture or the pages of a scan.

    None means "read it the old way": the app isn't there, the picture isn't text, or anything
    went wrong. Only JPEGs the child re-encoded from decoded pixels go to the app. A scanned PDF's
    text is `[Pages: total]` then a `[Page n]` block per page that had words."""
    from service import documents
    from service.assistant.hub import hub
    global _ocr_resting_until
    ctx.resting = ctx.background and time.monotonic() < _ocr_resting_until
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
    from service import document_extract
    total = min(total, document_extract.MAX_DECLARED_PAGES)
    overview = result.get("overview")
    overview = overview if isinstance(overview, str) and overview.startswith("data:image/jpeg;base64,") else ""
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
            if not ctx.background and (str(error) == "app_not_connected" or (
                    str(error) == "recognize_timeout" and page_s >= _OCR_PAGE_S)):
                _ocr_resting_until = time.monotonic() + _OCR_REST_S
            if not parts:
                return None
            break
        attempted += 1
        text = _defuse(_UNPRINTABLE.sub(" ", str(text or "")).strip())
        if text and confidence >= _OCR_MIN_CONFIDENCE:
            parts.append((number, text, float(confidence)))
    if sum(len(re.sub(r"\s", "", text)) for _n, text, _c in parts) < _OCR_MIN_CHARS:
        return None
    joined = (f"[Pages: {total}]\n" + "\n\n".join(f"[Page {n}]\n{text}" for n, text, _c in parts)
              if paged else parts[0][1])
    return _Recognised(joined, len(parts), total, overview, min(c for _n, _t, c in parts),
                       attempted == len(images) and result.get("stopped_early") is not True)


async def _describe(ctx: _Ctx, image: str) -> str:
    """What a picture of text is and how it is laid out, from the vision model. '' when that can't be had."""
    from service import vision
    if not image or ctx.remaining() < 20.0:
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


def _expected_kind(f: _File, name: str, suffix: str, family: str) -> str:
    """The kind a fresh read of this file under this name would be saved as (the same order of
    checks as `_read_file`); '' for a file that is never saved."""
    if family == "email" or suffix == "eml":
        return "eml"
    if family == "script" or suffix in _TEXT_NAMES:
        return "txt"
    if _is_audio(family, suffix):
        return "audio"
    if _is_picture(name, f.mime) or (family == "image" and suffix not in _DOCUMENT_NAMES):
        return "ocr"
    if suffix in _DOCUMENT_NAMES or family == "document":
        return _MACRO_MAP.get(suffix, suffix)
    return ""


async def _read_file(ctx: _Ctx, email: _Email, f: _File, budget: int, depth: int = 0) -> _Reading:
    """Fetch, inspect and read one file. Never raises. `depth` is 1 for a file inside an attached email."""
    from service import attachment_store, documents, vision
    from service import large_document
    reading = _Reading()
    f.about = email
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
            _recalled_note(reading, stored)
            reading.where = _SAVED
            await _seen(f, stored.digest)
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
            reading.inspected = True
            return reading
        if family == "email" or suffix == "eml":
            if depth >= 1:
                reading.note = "An email inside it, not opened."
                return reading
            done = await _read_email(ctx, email, f, reading, digest, name, data, budget)
            if done is not None:
                return done
        stored = await _recall(digest=digest)
        expected = _expected_kind(f, name, suffix, family)
        if stored is not None and stored.kind != expected and not (expected == "eml" and stored.kind == "txt"):
            stored = None       # these bytes were read as another kind of file under another name
        if stored is not None:
            await _seen(f, digest)
            # The same bytes read before (this file again, or another email's copy): no second read.
            # The type and the risk are this file's own, from the inspection above; only the text,
            # how it was got and how much of it there is come from the saved reading.
            _apply_cover(reading, stored.note)
            _recalled_note(reading, stored)
            if stored.text.strip() and ENCRYPTED_PDF_REASON in reading.risk:
                reading.risk = [reason for reason in reading.risk if reason != ENCRYPTED_PDF_REASON]
                reading.risk_level = reading.risk_level if reading.risk else ""
            if _gmail_locator(f) and f.read_source == "gmail" and f.read_account == f.gmail_account:
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
        if family == "script" or suffix in _TEXT_NAMES or suffix == "eml" or family == "email":
            stem = PurePath(name).stem or "file"
            text, cut = await documents.extract_async(f"{stem}.txt", data, large=True,
                                                      timeout=max(1.0, min(45.0, ctx.remaining())))
            script = family == "script" or suffix in _SCRIPT_NAMES
            reading.note = ("Read as plain text; the script was not run. " if script else "Read as plain text. ")
            return await _settle(ctx, reading, f, digest, text, cut, name, "txt", budget)
        if _is_audio(family, suffix):
            return await _read_audio(ctx, f, reading, digest, name, data, budget)
        if family == "video":
            raise _Unreadable("It is a video, and Wisp doesn't read videos in "
                              + ("Messages." if f.source == "messages" else "email."))
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
                    reading.note = (f"Wisp read {with_text} of its {declared} pages; the rest were past the "
                                    "limit it reads. " if cut else
                                    f"{with_text} of its {declared} pages have text Wisp could read; "
                                    "the rest are blank or pictures. ")
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
    return _plain(_header_text(index, email))


def _header_text(index: int, email: _Email) -> str:
    if email.source == "messages":
        chat = _pretty(email.chat, 80) or "a conversation"
        who = f"from {_pretty(email.sender, 80) or 'an unknown sender'}"
        return (f"Message you sent in {chat}, {_when(email.ts)}" if email.from_me
                else f"Message {who} in {chat}, {_when(email.ts)}")
    return (f"Email {index}: from {_pretty(email.sender, 80) or 'unknown sender'} | "
            f"subject: {_pretty(email.subject, 100) or '(no subject)'} | {_when(email.ts)} | "
            f"source: {_source_label(email)}")


def _availability(f: _File) -> str:
    if f.source == "messages":
        return "saved on this Mac" if f.on_disk else "not downloaded by Messages"
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
    return _plain(head)


def _block(f: _File, reading: _Reading, room: int) -> str:
    """The text for one read file and the files read inside it, at most `room` characters (0 = doesn't fit)."""
    extra = "\n" + reading.extra if reading.extra else ""
    block = _block_one(f, reading, room - len(extra))
    return block + extra if block else ""


def _block_one(f: _File, reading: _Reading, room: int) -> str:
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
    base = _plain("\n".join(lines))
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
        base = _plain("\n".join(lines))
        free = room - len(base) - 1 - untrusted.overhead(_KIND)
        text = text[:max(0, free)]
    return base + "\n" + untrusted.fence(text, _KIND)


def _trailer(notes: list[str], room: int) -> str:
    """What was left out, whole notes only: files not read come first, since they matter most."""
    if not notes or room <= 0:
        return ""
    notes = [_plain(note) for note in notes]
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
        lines.append(_plain(f"  - {_safe_name(f.name)} ({_suffix(f.name).upper() or 'file'}{size}, "
                            f"{_availability(f)})"))
    return lines


# --- remembering where a kept file was seen ----------------------------------------------

def _note_seen(f: _File, digest: str, **gen) -> None:
    """Tell the store who sent this kept file and when (for finding it later). Best effort and
    synchronous: the store is a convenience, and a store without `note_seen` is simply not told."""
    try:
        from service import attachment_store
        note = getattr(attachment_store, "note_seen", None)
        email = f.about
        if note is None or email is None or not digest:
            return
        source = "messages" if f.source == "messages" else ("gmail" if f.read_source == "gmail" else "mail")
        account = f.read_account or (f.gmail_account if source == "gmail" else "")
        note(digest, source=source, account=account, name=_safe_name(f.name), sender=_pretty(email.sender, 200),
             subject=_pretty(email.subject, 300), ts=float(email.ts or 0), **gen)
    except Exception:  # noqa: BLE001
        pass


async def _seen(f: _File, digest: str) -> None:
    try:
        gen = {} if _EPOCH.get() is None else {"epoch": _EPOCH.get()}
        await asyncio.to_thread(_note_seen, f, digest, **gen)
    except Exception:  # noqa: BLE001
        pass


# --- files in Messages ---------------------------------------------------------------------

_MESSAGES_LIMIT = 300
_MESSAGES_WHY = {
    "not_on_disk": "Messages hasn't downloaded it to this Mac",
    "too_large": "it is larger than Wisp can read (32 MB)",
    "unreadable": "Messages' saved copy couldn't be read",
    "not_found": "Messages no longer has it",
    "no_messages_db": "Wisp can't read Messages; check Full Disk Access",
    "app_not_connected": "the Wisp app isn't connected",
}


def _messages_problem(error: BaseException) -> str:
    if isinstance(error, bridge.BridgeError):
        return _MESSAGES_WHY.get(str(error), "it couldn't be read just now")
    return "it couldn't be read just now"


def _sentence(reason: str) -> str:
    return reason[:1].upper() + reason[1:].rstrip(".") + "."


async def _fetch_messages(ctx: _Ctx, f: _File) -> tuple[bytes, str]:
    """(bytes, "Messages") for a file someone texted. Raises _Unreadable with a plain reason."""
    if not f.has_messages:
        raise _Unreadable(_sentence(_MESSAGES_WHY["not_found"]))
    if not f.on_disk:
        raise _Unreadable(_sentence(_MESSAGES_WHY["not_on_disk"])
                          + " Open the conversation in Messages to download it.")
    read = getattr(bridge, "read_message_attachment", None)
    why = "app_not_connected"
    data = None
    if read is not None:
        try:
            data, _name, why = await read(f.message_id, timeout=max(1.0, min(45.0, ctx.remaining())))
        except bridge.BridgeError as error:
            data, why = None, str(error)
    if data is not None:
        f.read_account, f.read_source = "messages", "messages"
        return data, "Messages"
    sentence = _sentence(_MESSAGES_WHY.get(why, "Messages' saved copy couldn't be fetched"))
    if why == "not_on_disk":
        sentence += " Open the conversation in Messages to download it."
    raise _Unreadable(sentence)


def _contact_words(terms: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(the words that are left, the handles of the contacts the words name, those contacts' names).

    A word names a contact only when it is that contact's whole name, or the first name of at least three
    letters, or the family name right after the first name (and never a word that is also an everyday
    word of a request, like "sent"). Without synced contacts nothing is taken."""
    try:
        from service.tools import imessage_tools
        names = dict(getattr(imessage_tools, "_name_handles", None) or {})
    except Exception:  # noqa: BLE001
        return terms, [], []
    rest: list[str] = []
    handles: list[str] = []
    who: list[str] = []
    previous: set[str] = set()
    for term in terms:
        found: list[str] = []
        matched: set[str] = set()
        if term not in _STOP_WORDS:
            for known, hs in names.items():
                parts = known.split()
                first = bool(parts) and term == parts[0] and len(term) >= 3
                if term == known or first or (known in previous and term in parts[1:]):
                    matched.add(known)
                    found.extend(hs)
                    if known not in who:
                        who.append(known)
        previous = {k for k in matched if k.split() and k.split()[0] == term}
        if not found:
            rest.append(term)
            continue
        for h in found:
            if h not in handles:
                handles.append(h)
    return rest, handles[:10], [_pretty(str(n).title(), 60) for n in who]


def _contact_split(terms: list[str]) -> tuple[list[str], list[str]]:
    """(the words that are left, the handles of the contacts the words name)."""
    rest, handles, _who = _contact_words(terms)
    return rest, handles


def _names_text(names: list[str]) -> str:
    """"Dana", "Dana and Sam", "Dana, Sam and 3 others"."""
    if len(names) <= 2:
        return " and ".join(names)
    left = len(names) - 2
    return f"{names[0]}, {names[1]} and {left} other{'' if left == 1 else 's'}"


def _who_of(handle: str) -> str:
    """The contact's name when the contacts map knows the handle, else the handle itself."""
    text = str(handle or "")
    try:
        from service.tools import imessage_tools
        text = imessage_tools.resolve_contact(text)
    except Exception:  # noqa: BLE001
        pass
    return _pretty(text, 80)


def _messages_emails(rows: list[dict]) -> list[_Email]:
    """One _Email per message: the rows are per attachment, so a message is (time, sender, chat)."""
    groups: dict[str, _Email] = {}
    counts: dict[str, dict] = {}
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        sender, chat = str(r.get("sender") or ""), str(r.get("chat") or "")
        from_me = r.get("from_me") is True
        key = f"m:{ts}|{sender}|{chat}|{int(from_me)}"
        email = groups.get(key)
        if email is None:
            label = _who_of(chat)
            email = groups[key] = _Email(key, ts, sender="" if from_me else _who_of(sender), subject=label,
                                         source="messages", chat=label, from_me=from_me)
        try:
            size = int(r.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        _add_file(email, "messages", counts.setdefault(key, {}),
                  _File(str(r["name"]), size, mime=str(r.get("mime") or ""), message_id=str(r.get("id") or ""),
                        on_disk=bool(r.get("on_disk")), source="messages"))
    return list(groups.values())


# --- audio ---------------------------------------------------------------------------------

async def _heard(ctx: _Ctx, name: str, data: bytes):
    """(Heard, "") or (None, a plain reason). Raises TimeoutError and anything unexpected."""
    from service import attachment_audio, voice
    try:
        heard = await asyncio.wait_for(
            attachment_audio.listen(name, data, client=ctx.client, model=ctx.model, deadline=ctx.deadline),
            max(1.0, ctx.remaining()))
    except voice.VoiceUnavailable:
        return None, "The model Ask is using on this Mac can't listen to audio."
    except ValueError as error:
        return None, _file_problem(error) or "It couldn't be listened to."
    if not _UNPRINTABLE.sub(" ", str(heard.text or "")).strip():
        return None, "No speech could be heard in it."
    return heard, ""


def _audio_note(heard) -> str:
    from service import attachment_audio
    how = _pretty(attachment_audio.how_much(heard), 200).rstrip()
    return "Transcribed on this Mac; some words may be wrong. " + (how + " " if how else "")


def _audio_text(heard) -> str:
    return _defuse(_UNPRINTABLE.sub(" ", str(heard.text or "")).strip())


async def _read_audio(ctx: _Ctx, f: _File, reading: _Reading, digest: str, name: str, data: bytes,
                      budget: int) -> _Reading:
    """A voice memo or other recording, written out by the model on this Mac. Kept only when all of it was heard."""
    heard, problem = await _heard(ctx, name, data)
    if heard is None:
        reading.problem = problem
        return reading
    reading.note = _audio_note(heard)
    reading.partial = not heard.complete
    return await _settle(ctx, reading, f, digest, _audio_text(heard), not heard.complete, name, "audio", budget,
                         how="transcribed", keep=bool(heard.complete))


# --- an email attached to an email ----------------------------------------------------------

_EML_NOTE = "An email attached to an email. Its files were read in memory; none was run or saved as a file. "
_EML_KEPT_NOTE = "An email attached to an email. Wisp kept its text; the files inside it were not read. "


@dataclass
class _Eml:
    text: str
    cut: bool
    files: list[dict]
    more: int
    walk_cut: bool = False      # the child skipped deep parts or left some uncounted
    more_unknown: bool = False  # there may be more files the child could not see


def _email_file(entry: dict) -> bool:
    return _suffix(entry["name"]) == "eml" or entry.get("mime", "").casefold() == "message/rfc822"


def _parse_eml(info) -> _Eml | None:
    """The headers and body as plain text, and the list of its files. None when there is nothing in it."""
    if not isinstance(info, dict):
        return None
    lines = []
    for label, key in (("From", "from"), ("To", "to"), ("Cc", "cc"), ("Subject", "subject"), ("Date", "date")):
        value = info.get(key)
        value = _pretty(value, 300) if isinstance(value, str) else ""
        if value:
            lines.append(f"{label}: {value}")
    body = info.get("body")
    body = _defuse(_UNPRINTABLE.sub(" ", body).strip()) if isinstance(body, str) else ""
    files = []
    for item in (info.get("files") if isinstance(info.get("files"), list) else [])[:50]:
        if not isinstance(item, dict) or type(item.get("index")) is not int or not isinstance(item.get("name"), str):
            continue
        size = item.get("size")
        files.append({"index": item["index"], "name": _safe_name(item["name"]),
                      "mime": _pretty(item.get("mime"), 80) if isinstance(item.get("mime"), str) else "",
                      "size": size if type(size) is int and size >= 0 else 0})
    more = info.get("more_files")
    more = more if type(more) is int and more > 0 else 0
    if not lines and not body and not files:
        return None
    text = "\n".join(lines) + (("\n\n" if lines else "") + body if body else "")
    return _Eml(text, info.get("body_cut") is True, files, more, info.get("walk_cut") is True,
                info.get("more_unknown") is True)


def _eml_listing(parsed: _Eml) -> str:
    """Every file in the email by cleaned name and size (inside the fence: the names are untrusted)."""
    if not parsed.files:
        return ""
    total = len(parsed.files) + parsed.more
    lines = [f"Files in this email ({'at least ' if parsed.more_unknown else ''}{total}):"]
    for entry in parsed.files[:_NESTED_LISTED]:
        facts = ", ".join(x for x in (_size_text(entry["size"]) if entry["size"] else "",
                                      "an email inside it, not opened" if _email_file(entry) else "") if x)
        lines.append(f"- {entry['name'][:60]}" + (f" ({facts})" if facts else ""))
    left = total - min(len(parsed.files), _NESTED_LISTED)
    if left:
        lines.append(f"(+{left} more not listed)")
    return "\n".join(lines)


async def _read_inner(ctx: _Ctx, email: _Email, outer: _File, outer_name: str, outer_data: bytes, entry: dict,
                      room: int, done: list | None = None, only: list | None = None) -> str:
    """The block for one file inside an attached email, read in memory. '' when it doesn't fit.
    `done`, when given, gets True appended when the file's content was really read; `only`, when given, gets
    True appended when it was only inspected (a program or archive). A problem counts as neither."""
    from service import documents
    inner = _File(entry["name"], entry["size"], mime=entry["mime"], nested=True, read_account=outer.read_account,
                  read_source=outer.read_source)
    reading = _Reading(where="the attached email")
    try:
        part = await documents.run_attachment_child("eml_part", outer_name, outer_data,
                                                    timeout=max(1.0, min(30.0, ctx.remaining())),
                                                    part=entry["index"])
        part = part if isinstance(part, dict) else {}
        raw = part.get("data_b64")
        data = base64.b64decode(raw, validate=True) if isinstance(raw, str) else b""
        if not data:
            raise _Unreadable("The file is empty.")
        inner.data, inner.size = data, len(data)
        if isinstance(part.get("name"), str) and part["name"].strip():
            inner.name = _safe_name(part["name"])
        if isinstance(part.get("mime"), str):
            inner.mime = _pretty(part["mime"], 80)
        reading = await _read_file(ctx, email, inner, max(120, room - 380), depth=1)
        if not reading.problem and reading.text.strip():
            target = only if reading.inspected else done
            if target is not None:
                target.append(True)
    except _Unreadable as error:
        reading.problem = str(error)
    except (TimeoutError, asyncio.TimeoutError):
        reading.problem = "Reading it took too long."
    except (ValueError, TypeError) as error:
        reading.problem = _file_problem(error) or "It couldn't be read."
    except Exception:  # noqa: BLE001 — one file, one reason
        reading.problem = "It couldn't be read."
    return _block(inner, reading, room)


async def _read_email(ctx: _Ctx, email: _Email, f: _File, reading: _Reading, digest: str, name: str, data: bytes,
                      budget: int) -> _Reading | None:
    """An attached email: its headers and body as the text, its files listed, and up to three read in
    memory beneath it. None when the child won't parse it (the caller then reads it as plain text)."""
    from service import documents
    try:
        info = await documents.run_attachment_child("eml", name, data, timeout=max(1.0, min(30.0, ctx.remaining())))
    except ValueError:
        return None
    parsed = _parse_eml(info)
    if parsed is None:
        return None
    candidates = [e for e in parsed.files
                  if not _email_file(e) and not (_is_picture(e["name"], e["mime"]) and 0 < e["size"] < TINY_IMAGE_BYTES)
                  ][:_NESTED_READS]
    listing = _eml_listing(parsed)
    avail = max(120, budget - len(listing) - 40)
    n = len(candidates) if budget >= _MIN_NESTED_BUDGET else 0
    outer_budget = max(120, avail // 2) if n else avail
    while n and (budget - outer_budget - len(listing) - 40) // n < 520:
        n -= 1
    if not n:
        outer_budget = avail
    reading.note = _EML_NOTE + (f"{parsed.more} more file{'' if parsed.more == 1 else 's'} not listed. "
                                if parsed.more else "")
    text, cut, keep = parsed.text, parsed.cut, True
    # Whatever the child could not show of the email itself: said plainly, and "Read the whole file." never is.
    gaps = [sentence for flag, sentence in (
        (parsed.cut, "Its text was cut short."),
        (parsed.walk_cut, "Some of its parts were too deep or too many to read."),
        (parsed.more_unknown, "There may be more files inside that Wisp couldn't see."),
        (parsed.more > 0, "")) if flag]       # the files not listed are already said in the note
    reading.partial = bool(gaps)
    gaps = [g for g in gaps if g]
    stored = await _recall(digest=digest)
    if stored is not None and stored.kind == "eml" and stored.text.strip():
        text, cut, keep = stored.text, stored.cut, False
        await _seen(f, digest)
    if text.strip():
        reading = await _settle(ctx, reading, f, digest, text, cut, name, "eml", outer_budget, keep=keep)
        if reading.problem:
            return reading
    if listing:
        reading.text = (reading.text + "\n\n" if reading.text.strip() else "") + listing
    reading.note = reading.note.rstrip() + " " if reading.note.strip() else ""
    if candidates and not n:
        reading.note += "Its files are listed, not read, for lack of room. "
    blocks: list[str] = []
    read_ok: list[bool] = []
    only_ok: list[bool] = []
    allowed = budget + 380
    skipped = 0
    for i, entry in enumerate(candidates[:n]):
        left = allowed - 380 - len(reading.text) - 40 - sum(len(b) + 1 for b in blocks)
        per = left // (n - i)
        if per < 400:
            skipped += n - i
            break
        block = await _read_inner(ctx, email, f, name, data, entry, per, read_ok, only_ok)
        if block:
            blocks.append(block)
        else:
            skipped += 1
    if skipped:
        reading.note += "Some of its files were not shown for lack of room. "
    if blocks:
        reading.extra = "  Files inside it that Wisp read (none was saved as a file or run):\n" + "\n".join(blocks)
    listed = len(parsed.files) + parsed.more
    if listed:
        inspected = f"; {len(only_ok)} only inspected" if only_ok else ""
        reading.note += (f"Read {len(read_ok)} of {'at least ' if parsed.more_unknown else 'its '}{listed} "
                         f"file{'' if listed == 1 else 's'}{inspected}. ")
        if len(read_ok) < listed:
            reading.partial = True
    if reading.partial:
        reading.note = reading.note.replace("Read the whole file.", "").replace("  ", " ").rstrip() + " "
        reading.note += " ".join(gaps + ["Only part of it was read."]) + " "
    return reading


# --- a file's whole text, for indexing and for questions across files ------------------------
#
# These do not shape the text to an answer's budget (no `question`, no cut), and they come in two
# strengths. `models=False` is the background strength: plain extraction and the app's text
# recognition only. It never reaches `vision`, `_describe`, `documents.local_chat`, `_shape` or
# `_finish_text`, so no language model can be loaded by it. The model-using branches (a scan the app
# can't recognise, a recording) are in `_whole_models`, which `_whole` calls only when `models` is true.

@dataclass
class _Whole:
    reading: _Reading
    text: str = ""
    digest: str = ""
    outcome: str = "unreadable"     # index_one's answer: kept | known | skipped | resting | unreadable


def _why(error: BaseException) -> str:
    """The plain reason for a failed read (the same sentences `_read_file` gives)."""
    from service import vision
    if isinstance(error, _Unreadable):
        return str(error)
    if isinstance(error, vision.VisionUnavailable):
        return _LOCAL_ONLY
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "Reading it took too long."
    if isinstance(error, bridge.BridgeError):
        return "Messages couldn't be asked just now." if "messages" in str(error) else "Mail couldn't be asked just now."
    if isinstance(error, (ValueError, httpx.HTTPError)):
        return _file_problem(error) or "It couldn't be read."
    from service.gmail_source import GmailError
    return ("Gmail couldn't give it up. Sign in to Gmail again in Settings." if isinstance(error, GmailError)
            else "It couldn't be read.")


async def _whole_models(ctx: _Ctx, out: _Whole, f: _File, name: str, suffix: str, family: str, data: bytes,
                        scanned_pdf: bool) -> bool:
    """The branches that need the model. True when this handled the file."""
    from service import vision
    reading = out.reading
    if _is_audio(family, suffix):
        heard, problem = await _heard(ctx, name, data)
        if heard is None:
            reading.problem = problem
            return True
        reading.note = _audio_note(heard)
        reading.partial = not heard.complete
        out.text = _audio_text(heard)
        if heard.complete and await _keep(f, out.digest, kind="audio", text=out.text, cut=False, how="transcribed",
                                          reading=reading):
            out.outcome = "kept"
        return True
    if scanned_pdf:
        from service import documents
        result = await documents.run_attachment_child("pdf_pages", name, data,
                                                      timeout=max(1.0, min(45.0, ctx.remaining())))
        images = [u for u in (result or {}).get("images") or [] if isinstance(u, str)]
        if not images:
            raise _Unreadable("That PDF has no text and no pages Wisp can read.")
        out.text = str(await vision.read_images(
            ctx.question or "Read the text on these pages and say what the document is.", images,
            what="the scanned pages of a PDF attached to an email", client=ctx.client, model=ctx.model,
            deadline=ctx.deadline) or "")
        reading.note = (f"A scanned PDF with no text: read the pictures of its first {len(images)} "
                        f"page{'' if len(images) == 1 else 's'} of {(result or {}).get('pages', '?')}.")
        reading.partial = True
        return True
    return False


def _resting(ctx: _Ctx) -> bool:
    """A picture was not tried because text recognition is resting (the background reader flags it itself; in
    the foreground the rest is read from the clock, so a skipped picture is not called 'without text')."""
    return ctx.resting or (not ctx.background and time.monotonic() < _ocr_resting_until)


async def _whole(ctx: _Ctx, email: _Email, f: _File, *, models: bool) -> _Whole:
    """Fetch, inspect and extract one file's whole text. Never raises (but cancellation)."""
    from service import attachment_store, documents
    out = _Whole(_Reading())
    reading = out.reading
    f.about = email
    name = _safe_name(f.name)
    suffix = _suffix(name)

    async def keep(text: str, kind: str, cut: bool, how: str, complete: bool = True) -> None:
        out.text = text
        reading.cut = bool(cut)
        if complete and text.strip():
            out.outcome = "kept" if await _keep(f, out.digest, kind=kind, text=text, cut=cut, how=how,
                                                reading=reading) else "skipped"
        else:
            out.outcome = "skipped"

    try:
        if ctx.remaining() < 2.0:
            raise _Unreadable("There wasn't time left to read it.")
        locator = _locator(f)
        stored = await _recall(locator=locator) if locator else None
        if stored is not None:
            _apply_meta(reading, stored.note)
            reading.where = _SAVED
            out.text, out.digest, out.outcome = stored.text, stored.digest, "known"
            reading.cut = bool(stored.cut)
            await _seen(f, stored.digest)
            return out
        data, where = await _fetch(ctx, email, f)
        reading.where = where
        if not data:
            raise _Unreadable("The file is empty.")
        digest = out.digest = attachment_store.digest(data)
        info = await documents.run_attachment_child("inspect", name, data,
                                                    timeout=max(1.0, min(30.0, ctx.remaining())))
        info = info if isinstance(info, dict) else {}
        reading.type_text = _pretty(info.get("real_type"), 80)
        reading.warnings = _strs(info.get("warnings"), 6, _MAX_NOTE)
        risk = info.get("risk") if isinstance(info.get("risk"), dict) else {}
        reading.risk = _strs(risk.get("reasons"), 5, _MAX_NOTE)
        reading.risk_level = risk.get("level") if risk.get("level") in ("high", "caution") and reading.risk else ""
        family = str(info.get("family") or "")
        if (family in ("program", "archive") or suffix in _PROGRAM_NAMES or suffix in _ARCHIVE_NAMES
                or (family == "zip" and suffix not in _OFFICE_ZIPPED)):
            reading.note = "Inspected only. Wisp never opens, unpacks or runs programs, scripts or archives."
            out.outcome = "skipped"
            return out
        emailish = family == "email" or suffix == "eml"
        stored = await _recall(digest=digest)
        expected = _expected_kind(f, name, suffix, family)
        if stored is not None and stored.kind != expected and not (expected == "eml" and stored.kind == "txt"):
            stored = None
        if stored is not None:
            _apply_cover(reading, stored.note)
            if stored.text.strip() and ENCRYPTED_PDF_REASON in reading.risk:
                reading.risk = [reason for reason in reading.risk if reason != ENCRYPTED_PDF_REASON]
                reading.risk_level = reading.risk_level if reading.risk else ""
            if _gmail_locator(f) and f.read_source == "gmail" and f.read_account == f.gmail_account:
                await _keep(f, digest, kind=stored.kind, text=stored.text, cut=stored.cut, how=stored.how,
                            reading=reading)
            await _seen(f, digest)
            out.text, out.outcome = stored.text, "known"
            reading.cut = bool(stored.cut)
            return out
        if emailish:
            try:
                eml = _parse_eml(await documents.run_attachment_child(
                    "eml", name, data, timeout=max(1.0, min(30.0, ctx.remaining()))))
            except ValueError:
                eml = None
            if eml is not None and eml.text.strip():
                reading.note = _EML_KEPT_NOTE
                # The files inside are listed but never read on this path, so the email is only partly read.
                reading.partial = bool(eml.cut or eml.walk_cut or eml.more > 0 or eml.more_unknown or eml.files)
                await keep(eml.text, "eml", eml.cut, "text")
                return out
        if suffix in _STRUCTURED_NAMES and family not in ("program", "archive", "zip", "image", "video"):
            try:
                result = await documents.run_attachment_child("structured", name, data,
                                                              timeout=max(1.0, min(30.0, ctx.remaining())))
            except ValueError:
                result = None
            text, what = _structured_text(result)
            if text:
                reading.note = ("Read as a calendar invite. Nothing was added to the calendar." if what == "calendar"
                                else "Read as a contact card. Nothing was added to Contacts.")
                out.text, out.outcome = text, "skipped"
                return out
        if family == "script" or suffix in _TEXT_NAMES or emailish:
            stem = PurePath(name).stem or "file"
            text, cut = await documents.extract_async(f"{stem}.txt", data, large=True,
                                                      timeout=max(1.0, min(45.0, ctx.remaining())))
            script = family == "script" or suffix in _SCRIPT_NAMES
            reading.note = ("Read as plain text; the script was not run. " if script else "Read as plain text. ")
            await keep(text, "txt", cut, "text")
            return out
        if _is_audio(family, suffix):
            if not models or not await _whole_models(ctx, out, f, name, suffix, family, data, False):
                reading.note = "Recordings are not transcribed in the background."
                out.outcome = "skipped"
            return out
        if family == "video":
            reading.problem = ("It is a video, and Wisp doesn't read videos in "
                               + ("Messages." if f.source == "messages" else "email."))
            out.outcome = "skipped"
            return out
        if _is_picture(name, f.mime) or (family == "image" and suffix not in _DOCUMENT_NAMES):
            recognised = await _recognised(ctx, name, data, paged=False)
            if recognised:
                reading.note = f"Its text was recognised {_exactness(recognised.confidence)}. "
                await keep(recognised.text, "ocr", False, "recognized", recognised.complete)
            elif _resting(ctx):
                reading.note = _RESTING_NOTE
                out.outcome = "resting"
            else:
                reading.note = ("It is a picture without text Wisp could recognise on this Mac, so it was not "
                                "read or described. ")
                out.outcome = "skipped"
            return out
        if suffix in _OLD_OFFICE:
            raise _Unreadable(f"It is an old .{suffix} file, which Wisp can't open. "
                              f"Saving it as .{_OLD_OFFICE[suffix]} first would work.")
        if suffix in _DOCUMENT_NAMES or family == "document":
            extract_name = f"{PurePath(name).stem or 'file'}.{_MACRO_MAP.get(suffix, suffix)}"
            scanned = False
            try:
                text, cut = await documents.extract_async(extract_name, data, large=True,
                                                          timeout=max(1.0, min(45.0, ctx.remaining())))
            except ValueError as error:
                if suffix != "pdf" or str(error) != _SCANNED_PDF:
                    raise
                text, cut, scanned = "", False, True
            if text.strip() and ENCRYPTED_PDF_REASON in reading.risk:
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
                    await keep(recognised.text, "pdf", read < total, "recognized", recognised.complete)
                    return out
                if models and await _whole_models(ctx, out, f, name, suffix, family, data, True):
                    out.outcome = "skipped"
                    return out
                if _resting(ctx):
                    reading.note, out.outcome = _RESTING_NOTE, "resting"
                    return out
                reading.note = "A scanned PDF that Wisp could not recognise on this Mac, so it was not read."
                out.outcome = "skipped"
                return out
            if not text.strip():
                raise _Unreadable("There was no text in it to read.")
            kind = _MACRO_MAP.get(suffix, suffix)
            if suffix == "pdf":
                from service import large_document
                declared, with_text = _page_facts(large_document, text)
                if declared and 0 < with_text < declared:
                    reading.partial = True
                    reading.note = (f"Wisp read {with_text} of its {declared} pages; the rest were past the "
                                    "limit it reads. " if cut else
                                    f"{with_text} of its {declared} pages have text Wisp could read; "
                                    "the rest are blank or pictures. ")
            await keep(text, kind, cut, "text")
            return out
        raise _Unreadable("Wisp can't read this kind of file."
                          + (f" Its bytes say it is {reading.type_text}." if reading.type_text else ""))
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 — one file, one reason
        reading.problem = _why(error)
        out.outcome = "unreadable"
    return out


# --- seams for the background reader, searching inside files, and questions across files -------

Unreadable = _Unreadable


def _bare_ctx(deadline: float, gmail_accounts: list[str]) -> _Ctx:
    from service import gmail_source
    return _Ctx(deadline=deadline, gmail_source=gmail_source, gmail_accounts=gmail_accounts, passes=None)


def from_mail_rows(rows: list[dict]) -> list[_Email]:
    """One merged _Email per message from Apple Mail attachment rows (what the index reads). Public seam."""
    return _merge(rows, [])


def is_picture(name: str, mime: str = "") -> bool:
    """True when the file name or MIME type says it is a picture. Public seam."""
    return _is_picture(name, mime)


async def fetch(email: _Email, f: _File, *, deadline: float) -> tuple[bytes, str]:
    """(bytes, where they came from) for one file of `email`, exactly as a read would get them: Apple
    Mail's saved copy first, then Gmail, or Messages for a texted file. Raises Unreadable(plain reason)."""
    from service import gmail_source
    try:
        accounts = sorted(gmail_source.connected_accounts())
    except Exception:  # noqa: BLE001
        accounts = []
    return await _fetch(_bare_ctx(deadline, accounts), email, f)


async def index_one(email: _Email, f: _File, *, deadline: float) -> str:
    """Read one file for the store, without any language model: "kept", "known" (already stored),
    "skipped" (nothing to keep, or only a model could read it), "resting" or "unreadable". Never raises.

    "resting" means a picture or scan was not tried because on-device text recognition is resting (the app
    failed to answer a foreground read in the last few minutes): the caller treats it as not tried and
    comes back to it. The background path never starts that rest itself.

    Only plain extraction and the app's on-device text recognition are used. A picture that isn't text,
    a scan the app can't recognise, a recording, a program, an archive, a video and an invite or card
    are "skipped". An attached email keeps its headers and body; the files inside it are not read.
    A file that is not on this Mac (a Gmail-only file, or any file with no saved Apple Mail copy) and a
    Messages file are "skipped": this never downloads from Gmail and never reads Messages."""
    try:
        if f.source == "messages" or getattr(email, "source", "") == "messages" or f.has_messages:
            return "skipped"
        if not (f.has_local and f.on_disk):
            return "skipped"
        # A copy with the Gmail fields cleared: the read can only try the local copy, even for a merged email.
        local = replace(f, gmail_account="", gmail_message="", gmail_attachment="", data=None)
        ctx = _bare_ctx(deadline, [])           # no client, no model, no Gmail accounts
        ctx.background = True
        await _stamp(ctx)
        began = ctx.epoch
        outcome = (await _whole(ctx, email, local, models=False)).outcome
        if outcome == "skipped" and began is not None:
            # An erase that overtook this read made the store refuse the write. That is not a file with
            # nothing to keep: say "resting" so the caller does not mark it tried and comes back to it.
            await _stamp(ctx)
            if ctx.epoch != began:
                return "resting"
        return outcome
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return "unreadable"


async def whole_text(ctx: _Ctx, email: _Email, f: _File) -> tuple[_Reading, str, str]:
    """(reading, the file's whole text unshaped, digest). The reading has the type, risk, where and note
    the normal path sets; the text is "" for a file with no text (and the reading says why). A picture
    that isn't text is not described. The text is kept as usual."""
    out = await _whole(ctx, email, f, models=True)
    return out.reading, out.text, out.digest


# --- run --------------------------------------------------------------------------

async def run(query: str | None = None, name: str | None = None, start: float | None = None,
              end: float | None = None, label: str = "", count: int = DEFAULT_COUNT,
              account: str | None = None, question: str | None = None, list_only: bool = False,
              pages: str | None = None, sheet: str | None = None, source: str = "mail",
              inside: str | None = None, across: bool = False) -> str:
    """Find attachments across Apple Mail and Gmail (or, with source="messages", in Messages only),
    then list or read them. Never raises.

    `pages` ("12", "3-5") and `sheet` (a tab's name or number) narrow the reading to that part.
    `inside` looks for words inside the files Wisp has already read (no new file is fetched). `across`
    goes through many files and pulls the same few facts from each (and wins over `inside`); `list_only`
    wins over `across`, and with `inside` it shows who, what and when but no snippets."""
    shown: list[str] = []
    try:
        return await asyncio.wait_for(
            _run(query, name, start, end, label, count, account, question, list_only, shown, pages, sheet,
                 "messages" if source == "messages" else "mail", inside, across is True),
            DEADLINE_S + 20.0)
    except (TimeoutError, asyncio.TimeoutError):
        if len(shown) > 1:
            # Files already read are whole blocks; running out of time doesn't undo them.
            return "\n".join([*shown, "Left out: time ran out before the remaining files could be read."])
        return "Reading the attachments took too long, so nothing is shown. Try asking for fewer emails."
    except Exception:  # noqa: BLE001 — a tool result, never a crash; nothing from the files is logged
        return "I couldn't look through attachments just now."


@dataclass
class _Found:
    chosen: list[_Email]
    notes: list[str]
    terms: list[str]
    accounts: list[str]
    named: str
    senders: list[str] = field(default_factory=list)    # Messages: contacts whose names were turned into senders
    errored: bool = False                                # Messages: the search itself failed


async def find(query: str | None = None, name: str | None = None, start: float | None = None,
               end: float | None = None, account: str | None = None, count: int = DEFAULT_COUNT,
               source: str = "mail", most: int = MAX_EMAILS) -> tuple[list[_Email], list[str]]:
    """The emails (or, with source="messages", the messages) that hold matching files, newest first,
    at most `count` and never more than `most`, and the notes about what was left out. Searches only;
    no model is used and no file is fetched."""
    found = await _find(query, name, start, end, account, count, "messages" if source == "messages" else "mail",
                        most)
    return found.chosen, found.notes


_MESSAGES_SEARCH_S = 12.0           # the Messages search and its one retry, together

_SENDER_NOTES = ("Looked for files sent by", "Nothing from ")


async def _find(query, name, start, end, account, count, source: str, most: int) -> _Found:
    """`_find_core`; in Messages, when words that name contacts were turned into senders and that found
    nothing, once more with the words as plain terms and no senders."""
    if source != "messages":
        return await _find_core(query, name, start, end, account, count, source, most, True)
    began = time.monotonic()
    slow = _Found([], ["Messages couldn't be searched: it took too long."], terms_of(query), [], "", [], True)
    try:
        found = await asyncio.wait_for(_find_core(query, name, start, end, account, count, source, most, True),
                                       _MESSAGES_SEARCH_S)
    except (TimeoutError, asyncio.TimeoutError):
        return slow
    left = _MESSAGES_SEARCH_S - (time.monotonic() - began)
    if found.senders and not found.chosen and not found.errored and left >= 1.0:
        # Both searches together get _MESSAGES_SEARCH_S, so the reads that follow still have time.
        try:
            again = await asyncio.wait_for(
                _find_core(query, name, start, end, account, count, source, most, False), left)
        except (TimeoutError, asyncio.TimeoutError):
            return slow
        again.notes = [f"Nothing from {_names_text(found.senders)}, so looked for the word instead.", *again.notes]
        return again
    return found


async def _find_core(query, name, start, end, account, count, source: str, most: int, contacts: bool) -> _Found:
    from service import gmail_source
    messages = source == "messages"
    noun = "message" if messages else "email"
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
    wanted = max(1, min(wanted, max(1, int(most))))
    problems: list[str] = []
    capped = False
    accounts: list[str] = []
    senders: list[str] = []
    errored = False
    if messages:
        handles: list[str] = []
        if contacts:
            terms, handles, senders = _contact_words(terms)
        try:
            rows, capped = await bridge.read_message_attachment_index(terms, handles, since, until,
                                                                      limit=_APPLE_LIMIT)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — a reason, never a crash
            rows = []
            errored = True
            problems.append(f"Messages couldn't be searched: {_messages_problem(error)}.")
        emails = sorted(_messages_emails(rows), key=lambda e: e.ts, reverse=True)
    else:
        connected = gmail_source.connected_accounts()
        accounts, account_note = _pick_accounts(connected, account)

        # Scheduled mail dated years ahead must not use up the newest-first row limit.
        jobs = [_search_apple(terms, since, until if explicit else cutoff)]
        jobs += [_search_gmail(gmail_source, a, terms, since or None, until or None, min(50, max(15, wanted * 3)))
                 for a in accounts]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        apple_rows: list[dict] = []
        if isinstance(results[0], BaseException):
            if isinstance(results[0], asyncio.CancelledError):
                raise results[0]
            problems.append(f"Apple Mail's saved mail couldn't be searched: {_problem_of(results[0])}.")
        else:
            apple_rows, capped = results[0]
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
    if senders:
        notes.append(f"Looked for files sent by {_names_text(senders)}.")
    if not explicit and not messages:
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
    if capped:
        notes.append("Messages had more attachments than were listed; narrow the request to see others."
                     if messages else
                     "Apple Mail had more attachments than were listed; narrow the request to see others.")
    chosen = emails[:wanted]
    if len(emails) > len(chosen):
        notes.append(f"{len(emails) - len(chosen)} more {noun}{'' if len(emails) - len(chosen) == 1 else 's'} "
                     f"with attachments (showing {len(chosen)}, newest first).")
    return _Found(chosen, notes, terms, accounts, named, senders, errored)


async def _run(query, name, start, end, label, count, account, question, list_only, shown: list[str],
               pages=None, sheet=None, source: str = "mail", inside=None, across: bool = False) -> str:
    from service import gmail_source
    deadline = time.monotonic() + DEADLINE_S
    messages = source == "messages"
    fell_through = ""
    if across and not list_only:
        return await _run_across(query, name, start, end, label, account, question, source)
    words = _inside_words(inside)
    if words:
        answer = await _run_inside(words, query, name, start, end, label, account, question, list_only, source)
        if answer is not None:
            return answer
        question = question or f"Find this in the file: {words}"
        fell_through = ("Nothing Wisp had already read matched, so these are listed instead." if list_only
                        else "Nothing Wisp had already read matched, so it read these now.")
    found = await _find(query, name, start, end, account, count, source, MAX_EMAILS)
    chosen, notes, terms, accounts, named = found.chosen, found.notes, found.terms, found.accounts, found.named
    if fell_through:
        notes.append(fell_through)
    where = f" in {label}" if label else ""
    if not chosen:
        what = f" matching {', '.join(terms)}" if terms else ""
        named_text = f" named {_pretty(name, 60)!r}" if named else ""
        if messages:
            return _plain(f"I didn't find any files in Messages{named_text}{what}{where}. "
                          + " ".join(notes)).strip()
        return _plain(f"I didn't find any email attachments{named_text}{what}{where}. "
                      + " ".join(notes)).strip()

    said = [n for n in notes if n.startswith(_SENDER_NOTES)]
    notes = [n for n in notes if n not in said]
    top = _plain((f"Files in {len(chosen)} message{'' if len(chosen) == 1 else 's'}{where}, newest first." if messages
                  else f"Attachments in {len(chosen)} email{'' if len(chosen) == 1 else 's'}{where}, newest first.")
                 + "".join(" " + n for n in said))
    limit = RESULT_BUDGET_CHARS

    if list_only:
        return _list_result(top, chosen, notes, limit, "message" if messages else "email")

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
    await _stamp(ctx)
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


def _list_result(top: str, chosen: list[_Email], notes: list[str], limit: int, noun: str = "email") -> str:
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
        notes.append(f"{hidden} more {noun}{'' if hidden == 1 else 's'} not listed for lack of room.")
    trailer = _trailer(notes, limit - used - 1)
    if trailer:
        pieces.append(trailer)
    return "\n".join(pieces)


# --- words inside the files Wisp has read, and the same facts from many files --------------------

_INSIDE_MAX = 120
_INSIDE_SHOWN = 5
_INSIDE_SEARCHED = 8
_SHAPE_MIN_ROOM = 200               # below this much room for it, the shaped text is left out
_SHAPE_MIN_S = 10.0
_ACROSS_FILES = 40
_ACROSS_SPARE_S = 6.0               # left unspent before the deadline, so the answer is always returned
_ACROSS_FACTS_S = 20.0
_ACROSS_MIN_S = 3.0                 # less than this left: no new file is started
_ACROSS_NOTES_MAX = 600
_NO_ALL_WORDS = "No file has all of those words; these have some of them."
_BACKGROUND_OFF = ('Wisp only knows files it has read for you; turning on "Read new attachments in the background" '
                   "in Settings lets it find others.")


def _inside_words(inside) -> str:
    """The words to look for inside files: cleaned and cut; '' when none were given."""
    return _pretty(inside, _INSIDE_MAX).strip() if isinstance(inside, str) else ""


def _finite_time(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and 0 < number < float("inf") else 0.0


def _background_on() -> bool:
    try:
        from service import attachment_index
        return bool(attachment_index.enabled())
    except Exception:  # noqa: BLE001 — not there (or broken) means off
        return False


def _search_failed() -> bool:
    """Whether the store's last search in THIS thread failed (a store that cannot say reports no failure)."""
    try:
        from service import attachment_store
        check = getattr(attachment_store, "last_search_failed", None)
        return bool(check()) if check is not None else False
    except Exception:  # noqa: BLE001
        return False


def _search_inside(words: str, name, start, end, account, source: str, failure: list | None = None) -> list:
    """The store's hits for `words`, narrowed like a normal search. Synchronous: run in a thread.
    `failure`, when given, gets True appended if any of the searches failed."""
    from service import attachment_store, gmail_source
    since, until = _finite_time(start), _finite_time(end)
    part = _pretty(name, 120) if isinstance(name, str) else ""
    wanted = str(account).strip() if isinstance(account, str) else ""
    jobs: list[dict] = []
    if source == "messages":
        jobs.append({"source": "messages"})
    elif wanted:
        try:
            connected = gmail_source.connected_accounts()
        except Exception:  # noqa: BLE001
            connected = {}
        chosen, no_match = _pick_accounts(connected, wanted)
        jobs.append({"source": "mail", "account": wanted})
        jobs += [{"source": "gmail", "account": a} for a in ([] if no_match else chosen)]
    else:
        jobs += [{"source": "mail"}, {"source": "gmail"}]
    lists = []
    for job in jobs:
        checked = getattr(attachment_store, "search_checked", None)
        if checked is not None:
            found, failed = checked(words, limit=_INSIDE_SEARCHED, since=since, until=until, name=part, **job)
        else:
            found = attachment_store.search(words, limit=_INSIDE_SEARCHED, since=since, until=until, name=part, **job)
            failed = _search_failed()
        lists.append(found)
        if failed and failure is not None:
            failure.append(True)
    if len(lists) == 1:
        return list(lists[0])
    newest: dict[str, object] = {}
    for hit in (h for hits in lists for h in hits):
        if hit.digest not in newest or hit.ts > newest[hit.digest].ts:
            newest[hit.digest] = hit
    merged = list(newest.values())
    if any(h.all_words for h in merged):
        merged = [h for h in merged if h.all_words]
    return sorted(merged, key=lambda h: (-h.ts, h.digest))


def _hit_head(index: int, hit) -> str:
    """Who, what and when, from the store's seen row, worded like a normal result."""
    if not (hit.sender or hit.subject or hit.name or hit.ts):
        return "A file Wisp read earlier"
    messages = hit.source == "messages"
    if messages and not hit.sender:
        # The store's seen row cannot tell a file the person sent from a text from someone unknown.
        chat = _pretty(str(hit.subject or ""), 80) or "a conversation"
        return _plain(f"A file in Messages, in {chat}" + (f", {_when(hit.ts)}" if hit.ts else ""))
    email = _Email("", float(hit.ts or 0), sender=str(hit.sender or ""), subject=str(hit.subject or ""),
                   apple=hit.source == "mail", gmail=[hit.account] if hit.source == "gmail" and hit.account else [],
                   source="messages" if messages else "mail", chat=str(hit.subject or "") if messages else "")
    return _email_header(index, email)


def _hit_lines(index: int, hit) -> tuple[list[str], _Reading]:
    reading = _Reading(where=_SAVED)
    _apply_meta(reading, hit.note)
    f = _File(str(hit.name or ""))
    # The sender, subject and file name are attacker-controlled and sit outside the fence: defuse markers.
    lines = [untrusted.neutralize(_hit_head(index, hit)), untrusted.neutralize(_file_head(f, reading))]
    how ={"recognized": "Its text was recognised on this Mac.",
           "transcribed": "Transcribed on this Mac; some words may be wrong."}.get(hit.how, "")
    lines.append("  Coverage: " + ((reading.note.strip() or how or "Read by Wisp earlier.")
                                    + (" Only the start of the file was kept." if hit.cut else "")
                                    + (" " + _TEXT_ONLY.strip() if hit.kind == "ocr" and hit.how == "recognized"
                                       else "")))
    if reading.risk:
        lines.append(("  Looks risky: " if reading.risk_level == "high" else "  Take care: ")
                     + " ".join(reading.risk))
    return lines, reading


def _fenced_block(lines: list[str], body: str, room: int) -> str:
    """The head lines and ONE fence holding `body`, at most `room` characters ('' when not even the lines fit)."""
    base = _plain("\n".join(lines))
    free = room - len(base) - 1 - untrusted.overhead(_KIND)
    if not body.strip():
        return base if len(base) <= room else ""
    if free < 40:
        return base if len(base) <= room else ""
    body = body.strip()[:free].rstrip()
    block = base + "\n" + untrusted.fence(body, _KIND)
    while len(block) > room and body:
        body = body[:-max(1, len(block) - room)].rstrip()
        block = base + "\n" + untrusted.fence(body, _KIND) if body else base
    return block if len(block) <= room else ""


async def _run_inside(words: str, query, name, start, end, label, account, question, list_only, source) -> str | None:
    """Search the text of the files Wisp has read for `words`. None means "nothing matched, and there are
    other filters": the caller then reads the files the normal way. Never fetches a file."""
    from service import attachment_store, documents, large_document, vision
    messages = source == "messages"
    asked = str(question).strip()[:300] if question else ""
    model_pair = (None, None)
    if asked and not list_only:
        try:
            model_pair = documents.local_model()
        except Exception:  # noqa: BLE001 — VisionUnavailable, ValueError, anything: nothing is shaped
            return _LOCAL_ONLY
    deadline = time.monotonic() + DEADLINE_S
    failure: list[bool] = []
    hits = await asyncio.to_thread(_search_inside, words, name, start, end, account, source, failure)
    if failure and not hits:
        return "Wisp's saved readings couldn't be searched just now. Try again in a moment."
    counter = getattr(attachment_store, "count_checked", None)
    try:
        counted = await asyncio.to_thread(counter if counter is not None else attachment_store.count)
        total = None if counted is None else int(counted)
    except Exception:  # noqa: BLE001
        total = -1                  # the count failed: the answer says nothing about how many
    if total is None and not hits:
        return "Wisp's saved readings couldn't be searched just now. Try again in a moment."
    notes = [] if total is None else [
        "Searched the files Wisp has read." if total < 0
        else f"Searched the text of {total} file{'' if total == 1 else 's'} Wisp has read."]
    if failure:
        notes.append("Some of Wisp's saved readings couldn't be searched just now.")
    if not messages and not _background_on():
        notes.append(_BACKGROUND_OFF)
    limit = RESULT_BUDGET_CHARS
    where = f" in {label}" if label else ""
    ctx = _Ctx(deadline=deadline, gmail_source=None, gmail_accounts=[],
               passes=large_document.PassBudget(deadline=deadline), client=model_pair[0], model=model_pair[1],
               question=asked or None)
    await _stamp(ctx)
    candidates = list(hits)
    blocks: list[tuple[list[str], _Reading, object, str]] = []   # (lines, reading, hit, text for the fence)
    stored_by_digest: dict[str, object] = {}
    for hit in candidates:
        if len(blocks) >= _INSIDE_SHOWN:
            break
        if asked and not list_only:
            try:
                stored = await asyncio.to_thread(attachment_store.get, hit.digest)
            except Exception:  # noqa: BLE001
                stored = None
            if stored is None or not str(stored.text or "").strip():
                continue                # aged out between the search and now
            stored_by_digest[hit.digest] = stored
        lines, reading = _hit_lines(len(blocks) + 1, hit)
        # A snippet is file content: a listing shows who, what and when only.
        blocks.append((lines, reading, hit, "" if list_only else
                       "\n".join("… " + _pretty(s, 200) for s in hit.snippets if s)))
    if not blocks:
        if query or name or start is not None or end is not None:
            return None
        return ("No file Wisp has read contains those words. " + " ".join(notes)).strip()

    count = len(blocks)
    top = f"Found {count} file{'' if count == 1 else 's'} with those words{where}, best match first."
    pieces = [top]
    used = len(top)
    if not any(h.all_words for _l, _r, h, _t in blocks):
        pieces.append(_NO_ALL_WORDS)
        used += len(_NO_ALL_WORDS) + 1
    for position, (lines, reading, hit, snippet_text) in enumerate(blocks):
        room_total = limit - _RESERVE - used - 1
        share = room_total // (count - position)
        body = snippet_text
        stored = stored_by_digest.get(hit.digest)
        if stored is not None and not list_only:
            fixed = len("\n".join(lines)) + 1 + untrusted.overhead(_KIND)
            shaped_room = share - fixed - len(snippet_text) - 40
            if shaped_room >= _SHAPE_MIN_ROOM and ctx.remaining() >= _SHAPE_MIN_S:
                try:
                    shaped = await _shape(ctx, reading, stored.text, stored.cut, _safe_name(hit.name), stored.kind,
                                          shaped_room)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — the snippets are already in hand
                    shaped = None
                if shaped is not None and not shaped.problem and shaped.text.strip():
                    body = (snippet_text + "\n\n" if snippet_text else "") + "For your question:\n" + shaped.text
                    # The shaping note replaces the stored one, but a recalled picture still has text only.
                    text_only = (" " + _TEXT_ONLY.strip() if hit.kind == "ocr" and hit.how == "recognized" else "")
                    lines = [*lines[:2], "  Coverage: " + (shaped.note.strip() or "Read by Wisp earlier.") + text_only,
                             *lines[3:]]
        block = _fenced_block(lines, body, min(room_total, share))
        if not block:
            continue
        pieces.append(block)
        used += len(block) + 1
    pieces.extend(notes)
    return "\n".join(pieces)


def _totals_once(attachment_fields, rows: list, unique: list) -> list[str]:
    """The totals with a repeated file counted once: through the keys when `totals` takes them, else
    over the de-duplicated rows."""
    all_facts = [facts for _e, _f, facts, _d, _p in rows]
    try:
        params = inspect.signature(attachment_fields.totals).parameters
        takes_keys = "keys" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        takes_keys = True
    if takes_keys:
        made = attachment_fields.totals(all_facts, keys=[digest for _e, _f, _x, digest, _p in rows])
    else:
        made = attachment_fields.totals([facts for _e, _f, facts, _d, _p in unique])
    return _sentences(made)


def _sentences(made) -> list[str]:
    """Whatever `totals` gave (a list of sentences, or one string), as sentences that each end in exactly one
    period, none dropped and none empty."""
    if isinstance(made, str):
        made = [made]
    out = []
    for item in made if isinstance(made, (list, tuple)) else []:
        text = str(item or "").strip()
        body = text.rstrip(". \t\r\n")
        if body:
            out.append(body + ".")
    return out


def _skip_kind(reading: _Reading) -> str:
    if reading.problem:
        return "unreadable"
    note = reading.note or ""
    if note.startswith("Inspected only"):
        return "program"
    if note.startswith(_RESTING_NOTE[:30]):
        return "unchecked"              # never tried: text recognition was resting
    if "scanned PDF" in note:
        return "scan"                   # pages that are pictures and gave no text, however they were tried
    if "not read or described" in note or "could not recognise" in note:
        return "picture"
    return "unreadable"


def _nothing_found(found: _Found, name, label, messages: bool) -> str:
    what = f" matching {', '.join(found.terms)}" if found.terms else ""
    named_text = f" named {_pretty(name, 60)!r}" if found.named else ""
    where = f" in {label}" if label else ""
    kind = "files in Messages" if messages else "email attachments"
    return _plain(f"I didn't find any {kind}{named_text}{what}{where}. " + " ".join(found.notes)).strip()


async def _run_across(query, name, start, end, label, account, question, source) -> str:
    """The same few facts from each of up to 40 files, newest first, with the totals added up by code."""
    from service import attachment_fields, attachment_store, documents, gmail_source, large_document
    messages = source == "messages"
    try:
        client, model = documents.local_model()
    except Exception:  # noqa: BLE001 — facts need the model, and only this Mac's
        return _LOCAL_ONLY
    deadline = time.monotonic() + DEADLINE_S
    work = deadline - _ACROSS_SPARE_S
    found = await _find(query, name, start, end, account, _ACROSS_FILES, source, _ACROSS_FILES)
    if not found.chosen:
        return _nothing_found(found, name, label, messages)
    flat = [(e, f) for e in found.chosen for f in e.files]
    considered = flat[:_ACROSS_FILES]
    ctx = _Ctx(deadline=work, gmail_source=gmail_source, gmail_accounts=found.accounts,
               passes=large_document.PassBudget(deadline=work), client=client, model=model)
    await _stamp(ctx)
    rows: list[tuple[_Email, _File, dict | None, str, bool]] = []     # (email, file, facts, digest, part only)
    skipped = {"program": 0, "picture": 0, "unchecked": 0, "scan": 0, "unreadable": 0}
    reached = risky = 0
    for email, f in considered:
        if ctx.remaining() < _ACROSS_MIN_S:
            break
        reading, text, digest = await whole_text(ctx, email, f)
        if reading.risk_level == "high":
            risky += 1
        if not str(text or "").strip():
            skipped[_skip_kind(reading)] += 1
            reached += 1
            continue
        facts = None
        if digest:
            try:
                facts = await asyncio.to_thread(attachment_store.get_facts, digest, attachment_fields.FACTS_VERSION)
            except Exception:  # noqa: BLE001
                facts = None
        if facts is None:
            try:
                facts = await attachment_fields.facts_of(
                    text, _suffix(_safe_name(f.name)), client=client, model=model,
                    deadline=min(work, time.monotonic() + _ACROSS_FACTS_S))
            except asyncio.CancelledError:
                raise
            except (TimeoutError, asyncio.TimeoutError):
                break                   # the engine is busy: end here with what is done
            except Exception:  # noqa: BLE001
                facts = None
            if isinstance(facts, dict) and digest:
                try:
                    await asyncio.to_thread(
                        attachment_store.put_facts, digest, attachment_fields.FACTS_VERSION, facts,
                        **({} if ctx.epoch is None else {"epoch": ctx.epoch}))
                except Exception:  # noqa: BLE001
                    pass
        rows.append((email, f, facts if isinstance(facts, dict) else None, digest or "",
                     bool(reading.partial or reading.cut)))
        reached += 1

    notes: list[str] = []
    not_reached = len(considered) - reached
    # The same file arriving in two emails is one file: one line, counted once.
    seen_digests: set[str] = set()
    unique: list[tuple[_Email, _File, dict | None, str, bool]] = []
    for item in rows:
        if item[3] and item[3] in seen_digests:
            continue
        if item[3]:
            seen_digests.add(item[3])
        unique.append(item)
    repeated = len(rows) - len(unique)
    # The first line counts files with facts out of the files considered; these notes account for the rest,
    # in this order: text but no facts, skipped by kind, not reached.
    without_facts = sum(1 for _e, _f, facts, _d, _p in unique if facts is None)
    if without_facts:
        notes.append(f"{without_facts} file{'' if without_facts == 1 else 's'} had text but no facts Wisp "
                     "could pull out.")
    if len(flat) > len(considered):
        left = len(flat) - len(considered)
        notes.append(f"{left} more file{'' if left == 1 else 's'} not looked at (at most {_ACROSS_FILES} per request).")
    labels = {"program": ("a program or archive", "programs or archives"),
              "picture": ("a picture without text", "pictures without text"),
              "unchecked": ("a picture Wisp couldn't check just now", "pictures Wisp couldn't check just now"),
              "scan": ("a scan Wisp couldn't read", "scans Wisp couldn't read"),
              "unreadable": ("one that could not be read", "that could not be read")}
    parts = [(labels[k][0] if n == 1 else f"{n} {labels[k][1]}") for k, n in skipped.items() if n]
    if parts:
        skipped_count = sum(skipped.values())
        notes.append(f"Skipped {skipped_count} file{'' if skipped_count == 1 else 's'} without text: "
                     + ", ".join(parts) + ".")
    if not_reached:
        notes.append(f"{not_reached} file{'' if not_reached == 1 else 's'} not reached; ask again and Wisp "
                     "continues, because what it read is kept.")
    if risky:
        notes.append(f"{risky} of these files look risky; read one on its own to see why.")
    found_notes = " ".join(found.notes)[:_ACROSS_NOTES_MAX]
    if found_notes:
        notes.append(found_notes)

    all_facts = [facts for _e, _f, facts, _d, _p in rows]
    with_facts = sum(1 for _e, _f, facts, _d, _p in unique if facts is not None)
    asked = _pretty(question, 80) if isinstance(question, str) else ""
    where = f" in {label}" if label else ""
    top = _plain(f"Facts from {with_facts} of {len(considered)} files{where}"
                 + (f" (for: {asked})" if asked else "") + ", newest first.")
    sentences = _totals_once(attachment_fields, rows, unique) if rows else []
    sentences = _sentences(sentences)
    total_line = _plain("Totals added up by Wisp: " + " ".join(sentences)) if sentences else ""
    if repeated and "same file" not in total_line:
        notes.append(f"{repeated} file{' was' if repeated == 1 else 's were'} the same file again and "
                     f"{'was' if repeated == 1 else 'were'} counted once.")
    partly = sum(1 for _e, _f, _facts, _d, part in unique if part)
    if partly:
        notes.append(f"{partly} file{' was' if partly == 1 else 's were'} only partly read, so "
                     f"{'its' if partly == 1 else 'their'} amounts may be missing.")
    lines = [attachment_fields.line(facts, name=_safe_name(f.name), when=_when(email.ts))
             + (" · part of the file" if part else "") for email, f, facts, _d, part in unique]

    def fit(room: int) -> list[str]:
        kept, used = [], 0
        for text in lines:
            if used + len(text) + 1 > room:
                break
            kept.append(text)
            used += len(text) + 1
        return kept

    fixed = len(top) + 1 + (len(total_line) + 1 if total_line else 0) + len(" ".join(notes)) + 1
    room = RESULT_BUDGET_CHARS - fixed - untrusted.overhead(_KIND) - 1
    kept = fit(room)
    if len(kept) < len(lines):
        kept = fit(room - 110)
        dropped = len(lines) - len(kept)
        notes.append(f"{dropped} line{'' if dropped == 1 else 's'} (the oldest) left out for lack of room; "
                     "the totals still cover them.")
    out = [top]
    if kept:
        out.append(untrusted.fence("\n".join(kept), _KIND))
    if total_line:
        out.append(total_line)
    out.extend(_plain(note) for note in notes)
    return "\n".join(out)


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
