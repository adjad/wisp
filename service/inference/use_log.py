"""Learn from my use: a private, local log of what you ask Wisp and how it answered.

ON by default (Settings > Models > "Learn from my use"; overlay key
``learn_from_use.enabled``). Nothing is trained or loaded, nothing is sent
anywhere and there is no proxy or port: after the verified local engine finishes
a reply, OMLXClient hands the exchange to this module, which writes one JSON
line on a background thread.

What is recorded
- Only a managed local engine (bundled or managed oMLX) answering inside an Ask
  conversation. Never a cloud client, never a user's own loopback provider app,
  and never a background call with no conversation bound.
- Only complete replies. A request that fails or is cancelled records nothing.

Files (service.paths.MOE_DIR / "use-log", folder 0700, files 0600)
- ``use-YYYY-MM-DD.jsonl``, one file per local day, one JSON object per line.
- Total size is capped at MAX_BYTES (200 MB) and age at MAX_AGE_DAYS (90 days);
  the oldest files go first. Checked on write, at most every PRUNE_EVERY_S.
- Excluded from Time Machine once, when the folder is created (``tmutil
  addexclusion``, best effort). It is not under an iCloud-synced folder.

Line format (v = 1). Every string passes service.safety.redaction before it is
written; the engine key, headers and credentials are never read here.

  exchange: {"v": 1, "kind": "exchange", "id": "<12 hex>", "ts": <epoch s>,
             "conversation": "<session id>", "model": "<model id>",
             "endpoint": "local",
             "messages": [<request messages exactly as sent; inline images
                           become "[image omitted]", strings over 50,000
                           characters are cut with a marker>],
             "tools": ["<tool schema name>", ...],
             "reply": {"content": <str|null>, "tool_calls": [...]|null}}

  followup: {"v": 1, "kind": "followup", "ref": "<exchange id>", "ts": <epoch s>,
             "conversation": "...",
             "tool_results": [{"tool_call_id": "...", "content": "...",
                               "error": <bool>}],      # present after tool calls
             "user_message": "<the user's next message>"}  # present when one came

A followup is a second line that points at the exchange by id; lines are never
rewritten. Tool results are read from the next request of the same conversation
(it carries them); the next user message is noted by the Ask runner.

Recording never slows or breaks an Ask: a bounded queue (drops when full), one
daemon writer thread, and every error swallowed. Turning the setting off stops
writes at once, including lines already queued.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1
MAX_BYTES = 200 * 1024 * 1024
MAX_AGE_DAYS = 90
PRUNE_EVERY_S = 300.0
QUEUE_MAX = 256
MAX_STRING = 50_000
MAX_TOOL_RESULT = 4_000
MAX_CONVERSATIONS = 64

_FILE = re.compile(r"^use-(\d{4}-\d{2}-\d{2})\.jsonl$")

_lock = threading.RLock()            # guards the files: writer, prune and delete
_start_lock = threading.Lock()       # guards only starting the writer; never held during I/O
_queue: "queue.Queue[tuple[int, dict]]" = queue.Queue(maxsize=QUEUE_MAX)
_thread: threading.Thread | None = None
_enabled: bool | None = None         # cached switch; None = not read yet
_epoch = 0                           # bumped by delete_all: older queued lines are dropped
_last: "OrderedDict[str, dict]" = OrderedDict()   # conversation -> its latest exchange
_last_prune = 0.0
_dir_ready: Path | None = None


# ---- the switch ----

def enabled() -> bool:
    global _enabled
    if _enabled is None:
        try:
            from service.config import get_learn_from_use_enabled
            _enabled = bool(get_learn_from_use_enabled())
        except Exception:  # noqa: BLE001 - an unreadable setting is not consent
            return False
    return _enabled


def set_enabled(on: bool) -> bool:
    """Persist the switch and apply it to the running process at once."""
    global _enabled
    from service.config import set_learn_from_use_enabled
    set_learn_from_use_enabled(on)
    _enabled = bool(on)
    return _enabled


# ---- storage ----

def log_dir() -> Path:
    from service import paths
    return paths.MOE_DIR / "use-log"


def _files() -> list[Path]:
    try:
        return sorted(p for p in log_dir().iterdir() if _FILE.match(p.name) and p.is_file())
    except OSError:
        return []


def usage() -> dict:
    sizes = []
    for p in _files():
        try:
            sizes.append(p.stat().st_size)
        except OSError:
            pass
    return {"bytes": sum(sizes), "files": len(sizes)}


def delete_all() -> int:
    """Delete every recorded file and forget pending lines; returns the bytes freed."""
    global _epoch
    freed = 0
    with _lock:
        _epoch += 1
        _last.clear()
        for p in _files():
            try:
                size = p.stat().st_size
                p.unlink()
                freed += size
            except OSError:
                pass
    return freed


def _exclude_from_backup(path: Path) -> None:
    try:
        subprocess.run(["/usr/bin/tmutil", "addexclusion", str(path)], timeout=5,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    except Exception:  # noqa: BLE001 - not on macOS, no permission, timed out: skip
        pass


def _ensure_dir() -> Path:
    global _dir_ready
    directory = log_dir()
    if _dir_ready != directory or not directory.is_dir():
        from service.paths import private_mkdirs
        fresh = not directory.exists()
        private_mkdirs(directory)
        os.chmod(directory, 0o700)
        if fresh:
            _exclude_from_backup(directory)
        _dir_ready = directory
    return directory


def _prune(now: float) -> None:
    """Drop files older than MAX_AGE_DAYS, then the oldest until under MAX_BYTES."""
    global _last_prune
    _last_prune = now
    files = _files()
    cutoff = datetime.fromtimestamp(now - MAX_AGE_DAYS * 86400).strftime("%Y-%m-%d")
    for p in list(files):
        if _FILE.match(p.name).group(1) < cutoff:
            try:
                p.unlink()
            except OSError:
                pass
            files.remove(p)
    sizes = {}
    for p in files:
        try:
            sizes[p] = p.stat().st_size
        except OSError:
            sizes[p] = 0
    total = sum(sizes.values())
    for p in files:                                   # oldest first (names sort by date)
        if total <= MAX_BYTES:
            break
        try:
            p.unlink()
        except OSError:
            continue
        total -= sizes[p]


# ---- capture ----

def _clip(value: Any, depth: int = 0) -> Any:
    """A deep copy that cannot be changed by the caller later; images and
    giant strings are shortened. Raises nothing the caller must handle."""
    if depth > 12:
        return "[too deep]"
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value[:80]:
            return "[image omitted]"
        if len(value) > MAX_STRING:
            return value[:MAX_STRING] + f"...[cut, {len(value) - MAX_STRING} more characters]"
        return value
    if isinstance(value, dict):
        return {str(k): _clip(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v, depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _tool_names(tools: Any) -> list[str]:
    names = []
    for tool in tools or []:
        if isinstance(tool, dict):
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            if isinstance(fn.get("name"), str):
                names.append(fn["name"])
    return names


def recordable(client: Any) -> bool:
    """A managed local engine, inside an Ask conversation, with the switch on.

    Callers have already established that no cloud masker applies; a user's
    loopback provider app is unmanaged (and named local_provider) and is refused here."""
    if getattr(client, "managed", False) is not True:
        return False
    if getattr(client, "endpoint_name", "local") == "local_provider":
        return False
    if not enabled():
        return False
    from service.safety import cloud_mask
    return bool(cloud_mask.current_conversation())


class Pending:
    """A request that has been sent; `finish` records it once the reply is complete."""
    __slots__ = ("_head",)

    def __init__(self, head: dict):
        self._head = head

    def finish(self, message: Any) -> None:
        try:
            if not isinstance(message, dict):
                return
            head = self._head
            head["reply"] = _clip({"content": message.get("content"),
                                   "tool_calls": message.get("tool_calls") or None})
            _enqueue({"kind": "exchange", **head})
        except Exception:  # noqa: BLE001 - recording must never touch the Ask
            pass


def start(client: Any, model: str, messages: Any, kwargs: dict) -> Pending | None:
    try:
        if not recordable(client):
            return None
        from service.safety import cloud_mask
        return Pending({
            "v": VERSION, "id": uuid.uuid4().hex[:12], "ts": round(time.time(), 3),
            "conversation": cloud_mask.current_conversation(), "model": str(model),
            "endpoint": str(getattr(client, "endpoint_name", "local")),
            "messages": _clip(messages), "tools": _tool_names(kwargs.get("tools"))})
    except Exception:  # noqa: BLE001
        return None


def note_user_message(conversation: str | None, text: Any) -> None:
    """The user's next message in a conversation: links to its last exchange."""
    try:
        if conversation and isinstance(text, str) and text.strip() and enabled():
            _enqueue({"kind": "user", "conversation": str(conversation),
                      "ts": round(time.time(), 3), "text": _clip(text)})
    except Exception:  # noqa: BLE001
        pass


def _enqueue(item: dict) -> None:
    global _thread
    if not enabled():
        return
    with _start_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="use-log", daemon=True)
            _thread.start()
    try:
        _queue.put_nowait((_epoch, item))
    except queue.Full:
        pass                                             # drop on overflow


def flush(timeout: float = 5.0) -> bool:
    """Wait until queued lines are written (tests, and a clean shutdown)."""
    end = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.005)
    return not _queue.unfinished_tasks


# ---- writer thread ----

def _is_error(content: Any) -> bool:
    text = content.lstrip()[:200].lower() if isinstance(content, str) else ""
    return text.startswith(("error", "{\"error\"", "{'error'", "failed", "tool error", "denied"))


def _tool_followup(prev: dict, messages: list) -> list[dict]:
    wanted = {c.get("id") for c in (prev.get("calls") or []) if isinstance(c, dict)}
    out = []
    for m in messages if isinstance(messages, list) else []:
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") in wanted:
            content = m.get("content")
            text = content if isinstance(content, str) else json.dumps(content, default=str)
            out.append({"tool_call_id": m["tool_call_id"], "content": text[:MAX_TOOL_RESULT],
                        "error": _is_error(text)})
    return out


def _write_line(record: dict) -> None:
    from service.safety.redaction import scrub_obj
    line = json.dumps(scrub_obj(record), ensure_ascii=False, separators=(",", ":")) + "\n"
    now = time.time()
    with _lock:
        directory = _ensure_dir()
        if now - _last_prune >= PRUNE_EVERY_S:
            _prune(now)
        if usage()["bytes"] + len(line.encode()) > MAX_BYTES:
            return                                       # still over the cap: drop this line
        from service.paths import private_append_text
        private_append_text(directory / f"use-{datetime.fromtimestamp(now):%Y-%m-%d}.jsonl", line)


def _handle(item: dict) -> None:
    conv = item.get("conversation")
    if item["kind"] == "user":
        prev = _last.get(conv)
        if prev:
            _write_line({"v": VERSION, "kind": "followup", "ref": prev["id"], "ts": item["ts"],
                         "conversation": conv, "user_message": item["text"]})
        return
    prev = _last.get(conv)
    if prev and prev.get("calls"):
        results = _tool_followup(prev, item.get("messages"))
        if results:
            _write_line({"v": VERSION, "kind": "followup", "ref": prev["id"], "ts": item["ts"],
                         "conversation": conv, "tool_results": results})
    _write_line(item)
    reply = item.get("reply") or {}
    _last[conv] = {"id": item["id"], "calls": reply.get("tool_calls") or []}
    _last.move_to_end(conv)
    while len(_last) > MAX_CONVERSATIONS:
        _last.popitem(last=False)


def _worker() -> None:
    while True:
        epoch, item = _queue.get()
        try:
            with _lock:
                if enabled() and epoch == _epoch:
                    _handle(item)
        except Exception as exc:  # noqa: BLE001 - never raise, never stop the writer
            log.debug("use log write skipped: %s", exc)
        finally:
            _queue.task_done()
