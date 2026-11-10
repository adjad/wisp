"""Save one email (or Messages) attachment to a folder on this Mac (Ask: save_email_attachment).

Two steps. `prepare` settles on exactly ONE file and the folder, fetches nothing and writes nothing, and
returns a `frozen` description plus the one-line card text. `save` runs after the person said yes.

Rules:
- IMPORTANT: the file is attacker-controlled (its name, its bytes, its sender) and saving puts it where the
  person may double-click it. It is never run, opened or unpacked here. Only its name is used to make a
  file name, and that name is cleaned (no path parts, no control, bidi or zero-width characters, no leading
  dot, a real extension kept, at most 120 characters).
- IMPORTANT: `frozen` is signed with a per-process random key. `save` refuses a `frozen` that was altered
  between the card and the save, so the model cannot swap in another file, name or folder. After a restart
  of the backend the key is new, and an old card's `frozen` is refused too.
- IMPORTANT: `save` finds the SAME file again (time, Message-ID or sender and subject, name, which of that
  name, size), fetches it, has the child process inspect it, and refuses a `high` risk file.
- The folder must be a real existing directory inside the home folder (symlinks resolved), outside
  ~/Library, ~/Applications, any hidden folder and any application bundle, and must pass the same location
  rules the safety floor applies to a write (`policy._PATH_DENY`, `policy.credential_path_hit`). It is never
  created. It is checked at the card and again right before the write, and the write goes through a
  directory file descriptor of the verified folder, so a link swapped in afterwards cannot redirect it.
- The new file is created exclusively (never overwrites; " (2)", " (3)" ... up to 99), without following a
  link, 0600 while it is written, marked `com.apple.quarantine` (downloaded, not yet approved) before it
  gets its final 0644 mode. If the mark cannot be set and read back, the file is removed and not saved.
  Any failure removes the partial file. Then Finder is asked to show it (a failure there keeps the save).
- Nothing from the file is logged or printed: not its name, path, sender or bytes. Neither function raises.
"""
from __future__ import annotations

import asyncio
import ctypes
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from service import documents, mail_attachments

_KEY = secrets.token_bytes(32)
_NOT_YOURS = "That isn't the file you confirmed."
_GONE = "That file isn't there any more, so nothing was saved."
_FOLDER_RULE = "Wisp only saves into a folder in your home folder, like Downloads or Documents."
_NO_MARK = "Wisp couldn't mark the file as downloaded, so it didn't save it."
_NOT_WRITTEN = "Wisp couldn't write the file, so nothing was saved."
_QUARANTINE = b"com.apple.quarantine"
_MAX_CHARS = 120
_MAX_BYTES = 200                    # a file name is limited to 255 bytes; room for " (99)"
_MAX_EXT = 20
_MAX_COPIES = 99
_WINDOW_S = 3 * 3600
_FETCH_S = 60.0
_INSPECT_S = 30.0
_REVEAL_S = 10.0
_HOME_FOLDERS = {"downloads": "Downloads", "desktop": "Desktop", "documents": "Documents"}
_BAD_FIRST = frozenset({"library", "applications"})
_BUNDLE_SUFFIXES = (".app", ".bundle", ".framework", ".plugin", ".kext", ".xpc", ".appex", ".photoslibrary")
_DROPPED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_FIELDS: dict[str, tuple] = {
    "source": (str,), "ts": (int, float), "sender": (str,), "subject": (str,), "rfc": (str,), "chat": (str,),
    "from_me": (bool,), "name": (str,), "nth": (int,), "size": (int,), "rowid": (int,), "attachment_id": (str,),
    "gmail_account": (str,), "gmail_message": (str,), "gmail_attachment": (str,), "message_id": (str,),
    "folder": (str,), "save_name": (str,), "check": (str,),
}


class _Stop(Exception):
    """A plain reason nothing was saved."""


# --- names ---------------------------------------------------------------------------

def clean_name(raw) -> str:
    """A safe file name made from an attacker's file name."""
    text = unicodedata.normalize("NFC", str(raw or "")).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    kept = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in _DROPPED:
            continue
        kept.append(" " if category == "Zs" else ch)
    text = unicodedata.normalize("NFC", "".join(kept).replace(":", "-")).strip(" .")
    stem, ext = os.path.splitext(text)
    if len(ext) > _MAX_EXT:
        stem, ext = text, ""
    while stem and (len(stem) + len(ext) > _MAX_CHARS or len((stem + ext).encode("utf-8")) > _MAX_BYTES):
        stem = stem[:-1]
    stem = stem.rstrip(" .")
    if not stem:
        stem = "attachment"
    return stem + ext


def _extension(name: str) -> str:
    return os.path.splitext(name)[1].lstrip(".").lower()


def _numbered(name: str, n: int) -> str:
    if n <= 1:
        return name
    stem, ext = os.path.splitext(name)
    return f"{stem} ({n}){ext}"


def _show(text, limit: int) -> str:
    """Text from a file or sender for the card: printable, no quote marks that could end the file's name early."""
    cleaned = mail_attachments._pretty(str(text or ""), 400)
    for mark in ("“", "”", '"'):
        cleaned = cleaned.replace(mark, "'")
    return cleaned[:limit].strip()


def _who(email) -> str:
    sender = str(email.sender or "")
    name = sender.split("<", 1)[0].strip().strip("\"'") if "<" in sender else sender.strip()
    return _show(name or sender.strip("<> "), 60)


# --- folder --------------------------------------------------------------------------

def _policy_refuses(path: str) -> bool:
    """True when the safety floor would refuse a write at `path`, or cannot tell."""
    try:
        from service.safety import policy
        rules = policy._rules("path_deny", policy._PATH_DENY)
        for form in policy._path_forms(path):
            if policy._any(rules, form):
                return True
        return policy.credential_path_hit({"path": path}, mutating=True) is not None
    except Exception:  # noqa: BLE001
        return True


def _home() -> str:
    return os.path.realpath(str(Path.home()))


def _spell_out(folder: str | None) -> str | None:
    """The folder as a path (not yet checked). None when it is a name Wisp does not know."""
    text = str(folder or "").strip()
    if not text:
        return os.path.join(_home(), "Downloads")
    if "\x00" in text:
        return None
    if text.startswith("~") or text.startswith("/"):
        return os.path.expanduser(text)
    words = text.replace("\\", "/").strip("/").split("/")
    first = words[0].strip().casefold()
    for prefix in ("my ", "the "):
        if first.startswith(prefix):
            first = first[len(prefix):].strip()
    if first.endswith(" folder"):
        first = first[:-7].strip()
    if first not in _HOME_FOLDERS:
        return None
    return os.path.join(_home(), _HOME_FOLDERS[first], *[w for w in words[1:] if w])


def check_folder(folder: str | None) -> tuple[str, str]:
    """(the real absolute folder, "") when Wisp may save there, else ("", a plain reason)."""
    try:
        spelled = _spell_out(folder)
        if spelled is None:
            return "", _FOLDER_RULE
        real = os.path.realpath(spelled)
        home = _home()
        if not real.casefold().startswith(home.casefold() + os.sep):
            return "", _FOLDER_RULE
        parts = real[len(home) + 1:].split(os.sep)
        if any(p.startswith(".") or not p for p in parts) or parts[0].casefold() in _BAD_FIRST:
            return "", _FOLDER_RULE
        if any(p.casefold().endswith(_BUNDLE_SUFFIXES) for p in parts):
            return "", _FOLDER_RULE
        if _policy_refuses(real) or _policy_refuses(spelled):
            return "", _FOLDER_RULE
        if not os.path.isdir(real):
            return "", "That folder doesn't exist, and Wisp doesn't create folders."
        if not os.access(real, os.W_OK | os.X_OK):
            return "", "Wisp can't write to that folder."
        return real, ""
    except (OSError, ValueError, RuntimeError):
        return "", _FOLDER_RULE


def _relative(folder: str) -> str:
    home = _home()
    return folder[len(home) + 1:] if folder.casefold().startswith(home.casefold() + os.sep) else folder


# --- integrity -----------------------------------------------------------------------

def _seal(fields: dict) -> str:
    body = json.dumps({k: v for k, v in fields.items() if k != "check"}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hmac.new(_KEY, body, hashlib.sha256).hexdigest()[:32]


def _genuine(frozen) -> bool:
    if not isinstance(frozen, dict) or set(frozen) != set(_FIELDS):
        return False
    for key, kinds in _FIELDS.items():
        value = frozen[key]
        if not isinstance(value, kinds) or (isinstance(value, bool) and bool not in kinds):
            return False
    try:
        return hmac.compare_digest(str(frozen["check"]), _seal(frozen))
    except Exception:  # noqa: BLE001
        return False


# --- prepare -------------------------------------------------------------------------

def _fetchable(f) -> bool:
    return bool(f.has_local or f.has_gmail or f.has_messages)


def _menu(pairs: list) -> str:
    items = []
    for email, f in pairs[:5]:
        items.append(f"“{_show(f.name, 60)}” from {_who(email) or 'someone'} on {mail_attachments._when(email.ts)}")
    return "Which file? " + "; ".join(items)


def _card(email, f, save_name: str, folder: str) -> str:
    ext = _extension(save_name)
    kind = ext.upper() if 0 < len(ext) <= 6 and ext.isalnum() else "file"
    detail = f"{kind}, {mail_attachments._size_text(int(f.size))}" if f.size and f.size > 0 else kind
    where = _show(_relative(folder), 60)
    who = _who(email)
    name = _show(save_name, 80)
    if email.source == "messages":
        own = f"{who}'s message" if who else "a message"
        if email.from_me:
            own = "your message"
        chat = _show(email.chat, 50)
        origin = own + (f" in {chat}" if chat else "")
    else:
        subject = _show(email.subject, 50)
        origin = (f"{who}'s email" if who else "an email") + (f" “{subject}”" if subject else "")
    return f"Save “{name}” ({detail}) from {origin} to {where}"[:200]


async def prepare(query=None, name=None, start=None, end=None, account=None,
                  source="mail", folder=None) -> dict:
    """Settle on ONE file and the folder. Fetches nothing, writes nothing."""
    try:
        where = "messages" if source == "messages" else "mail"
        real, why = check_folder(folder)
        if not real:
            return {"ok": False, "reason": why, "question": ""}
        try:
            emails, _notes = await mail_attachments.find(query=query, name=name, start=start, end=end,
                                                         account=account, count=5, source=where, most=5)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return {"ok": False, "reason": "I couldn't search for that file right now.", "question": ""}
        pairs = [(e, f) for e in emails or [] for f in e.files if _fetchable(f)]
        if not pairs:
            return {"ok": False, "reason": "I didn't find an attachment like that.", "question": ""}
        if len(pairs) > 1 and name:
            wanted = str(name).strip().casefold()
            exact = [p for p in pairs if p[1].name.strip().casefold() == wanted]
            if len(exact) == 1:
                pairs = exact
        if len(pairs) > 1:
            return {"ok": False, "reason": "More than one file matches.", "question": _menu(pairs)}
        email, f = pairs[0]
        save_name = clean_name(f.name)
        frozen = {
            "source": "messages" if email.source == "messages" else "mail",
            "ts": float(email.ts or 0), "sender": str(email.sender or "")[:300],
            "subject": str(email.subject or "")[:300], "rfc": str(email.rfc or "")[:300],
            "chat": str(email.chat or "")[:300], "from_me": bool(email.from_me),
            "name": str(f.name or "")[:300], "nth": int(f.nth or 1), "size": int(f.size or 0),
            "rowid": int(f.rowid or 0), "attachment_id": str(f.attachment_id or "")[:300],
            "gmail_account": str(f.gmail_account or "")[:300], "gmail_message": str(f.gmail_message or "")[:300],
            "gmail_attachment": str(f.gmail_attachment or "")[:600], "message_id": str(f.message_id or "")[:100],
            "folder": real, "save_name": save_name,
        }
        frozen["check"] = _seal(frozen)
        return {"ok": True, "frozen": frozen, "card": _card(email, f, save_name, real)}
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "I couldn't get that file ready to save.", "question": ""}


# --- save ----------------------------------------------------------------------------

def _same(frozen: dict, email, f) -> bool:
    if email.source != frozen["source"] or abs(float(email.ts or 0) - frozen["ts"]) > 1.0:
        return False
    if f.name.casefold() != frozen["name"].casefold() or int(f.nth or 1) != frozen["nth"]:
        return False
    if int(f.size or 0) != frozen["size"]:
        return False
    if frozen["source"] == "messages" and f.message_id != frozen["message_id"]:
        return False
    if frozen["rfc"] and email.rfc:
        return email.rfc[:300] == frozen["rfc"]
    return str(email.sender or "")[:300] == frozen["sender"] and str(email.subject or "")[:300] == frozen["subject"]


async def _locate(frozen: dict):
    emails, _notes = await mail_attachments.find(
        name=frozen["name"], start=frozen["ts"] - _WINDOW_S, end=frozen["ts"] + _WINDOW_S,
        source=frozen["source"], count=10, most=10)
    for email in emails or []:
        for f in email.files:
            if _same(frozen, email, f):
                return email, f
    return None, None


def _set_quarantine(fd: int, value: bytes) -> bool:
    """Set com.apple.quarantine on the open file and read it back. False when either step fails."""
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.fsetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_uint32, ctypes.c_int]
        libc.fsetxattr.restype = ctypes.c_int
        libc.fgetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_uint32, ctypes.c_int]
        libc.fgetxattr.restype = ctypes.c_ssize_t
        if libc.fsetxattr(fd, _QUARANTINE, value, len(value), 0, 0) != 0:
            return False
        buffer = ctypes.create_string_buffer(256)
        got = libc.fgetxattr(fd, _QUARANTINE, buffer, 256, 0, 0)
        return got == len(value) and buffer.raw[:got] == value
    except Exception:  # noqa: BLE001
        return False


def _fd_path(fd: int) -> str:
    try:
        raw = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH"), b"\0" * 1024)
        return os.fsdecode(raw.split(b"\0", 1)[0])
    except Exception:  # noqa: BLE001
        return ""


def _remove(dirfd: int, name: str, fd: int) -> None:
    """Remove the file this call created, if the name still points at it."""
    try:
        mine, there = os.fstat(fd), os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        if (mine.st_dev, mine.st_ino) == (there.st_dev, there.st_ino):
            os.unlink(name, dir_fd=dirfd)
    except OSError:
        pass


def _write(folder: str, save_name: str, data: bytes) -> str:
    """Create the file in the verified `folder` and return its final name. Raises _Stop(plain reason)."""
    real, why = check_folder(folder)
    if not real or real != folder:
        raise _Stop(why or _FOLDER_RULE)
    try:
        dirfd = os.open(real, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    except OSError:
        raise _Stop(_NOT_WRITTEN)
    try:
        opened = _fd_path(dirfd)
        if opened and opened != real:
            raise _Stop(_FOLDER_RULE)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        for n in range(1, _MAX_COPIES + 1):
            candidate = _numbered(save_name, n)
            try:
                fd = os.open(candidate, flags, 0o600, dir_fd=dirfd)
            except FileExistsError:
                continue
            except OSError:
                raise _Stop(_NOT_WRITTEN)
            break
        else:
            raise _Stop("There are too many files with that name in the folder, so nothing was saved.")
        try:
            try:
                view, sent = memoryview(data), 0
                while sent < len(view):
                    sent += os.write(fd, view[sent:])
                os.fsync(fd)
            except OSError:
                raise _Stop(_NOT_WRITTEN)
            if not _quarantine(fd):
                raise _Stop(_NO_MARK)
            try:
                os.fchmod(fd, 0o644)
            except OSError:
                raise _Stop(_NOT_WRITTEN)
        except BaseException:
            _remove(dirfd, candidate, fd)
            raise
        finally:
            os.close(fd)
        return candidate
    finally:
        os.close(dirfd)


def _quarantine(fd: int) -> bool:
    return _set_quarantine(fd, f"0083;{int(time.time()):x};Wisp;".encode("ascii"))


def _open_in_finder(path: str) -> bool:
    try:
        done = subprocess.run(["/usr/bin/open", "-R", path], env={"PATH": "/usr/bin:/bin"}, shell=False,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=_REVEAL_S, check=False)
        return done.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _stopped(reason: str) -> dict:
    return {"ok": False, "wrote": False, "reason": reason}


def _sentence(text) -> str:
    cleaned = mail_attachments._pretty(str(text or ""), 300)
    return cleaned if not cleaned or cleaned[-1] in ".!?" else cleaned + "."


async def save(frozen: dict) -> dict:
    """Save the frozen file. Never raises; a failed save leaves no file behind."""
    try:
        if not _genuine(frozen):
            return _stopped(_NOT_YOURS)
        real, why = check_folder(frozen["folder"])
        if not real or real != frozen["folder"] or clean_name(frozen["name"]) != frozen["save_name"]:
            return _stopped(why or _NOT_YOURS)
        email, f = await _locate(frozen)
        if f is None:
            return _stopped(_GONE)
        try:
            data, _where = await mail_attachments.fetch(email, f, deadline=time.monotonic() + _FETCH_S)
        except mail_attachments.Unreadable as error:
            return _stopped(_sentence(error) or "Wisp couldn't read that file, so nothing was saved.")
        if not data:
            return _stopped("The file is empty, so nothing was saved.")
        try:
            info = await documents.run_attachment_child("inspect", frozen["name"], data, timeout=_INSPECT_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return _stopped("Wisp couldn't check the file, so it didn't save it.")
        risk = info.get("risk") if isinstance(info, dict) else None
        level = str(risk.get("level") or "") if isinstance(risk, dict) else ""
        reasons = [r for r in (risk.get("reasons") or []) if isinstance(r, str) and r.strip()] \
            if isinstance(risk, dict) and isinstance(risk.get("reasons"), list) else []
        first = _sentence(reasons[0]) if reasons else ""
        if level == "high":
            where = "message in Messages" if frozen["source"] == "messages" else "email in Mail"
            return _stopped(f"Wisp didn't save it: {first or 'it looks unsafe.'} Open the {where} if you still want it.")
        if level != "caution" and level not in ("", "low", "none", "ok"):
            return _stopped("Wisp couldn't check the file, so it didn't save it.")
        try:
            final = await asyncio.to_thread(_write, real, frozen["save_name"], data)
        except _Stop as stop:
            return _stopped(str(stop))
        path = os.path.join(real, final)
        shown = await asyncio.to_thread(_open_in_finder, path)
        text = f"Saved “{_show(final, 130)}” to {_relative(real)}."
        if shown:
            text += " Shown in Finder."
        if level == "caution" and first:
            text += f" Take care: {first}"
        return {"ok": True, "text": text, "path": path}
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return _stopped("Wisp couldn't save that file, so nothing was saved.")
