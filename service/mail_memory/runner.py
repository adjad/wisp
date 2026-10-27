"""The background tick that walks 90 days of mail, one email per tick, only when idle.

Wisp has one resident model, so only the model's time is gated; reading Mail's
index and files costs nothing. Three levels (docs/plans/2026-10-26-email-digest-memory-research.md):

- busy:    a user's turn is in flight, a generation is running, the model was
           used by something else in the last 2 minutes, the fast model isn't
           resident, or the Mac is in Low Power Mode / on battery under 30%.
           No email is read and no model call runs. Mail's index is still read
           when it's due (it costs no model time), so the queue fills, and a
           missing permission shows, as soon as Wisp starts; only Low Power Mode
           or battery under 30% skips that too.
- between: otherwise. One call per tick, at least 90 s apart.
- away:    no keyboard or mouse for 10+ minutes, on power or battery over 50%.
           Calls run back to back, 5 s apart.

IMPORTANT rules:
- Never loads or evicts a model. The fast role must stay local
  (`role_target`), and under a remote-only profile the walk stands down, so
  mail never reaches a cloud model.
- One email that keeps failing (no reply, a truncated reply, an unreadable
  file) is retried once and then retired; it never holds up the queue.
- A user's turn wins: the call is cancelled the moment one starts.
- Mode "live" (the default, the user's choice for rc2) lets the Brief show
  the important suggestions (`importance.judge`); "shadow" extracts and records
  but shows nothing; "off" stops the walk.
- Logs and the store's meta carry counts and reason codes only, never mail text.
- A failed tick is never silent: the reason code is kept in meta (`last_error`,
  `last_error_at`, cleared by the next good read) and logged at WARNING, which
  reaches moe-backend.log (INFO does not: nothing configures the root logger, so
  Python's last-resort handler prints WARNING and above to stderr). A reason
  warns when it first appears or changes, and again at most once an hour while
  it repeats; other repeats log at INFO.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from service import idle
from service.mail_memory import extract, importance
from service.mail_memory.bridge import BridgeError, read_body, read_index
from service.mail_memory.store import store
from service.paths import MOE_DIR

log = logging.getLogger("wisp.mail_memory")

MODES = ("off", "shadow", "live")
SETTINGS_PATH = MOE_DIR / "mail_memory" / "settings.json"
WINDOW_DAYS = 90
INDEX_EVERY_S = 6 * 3600
BETWEEN_GAP_S = 90
AWAY_GAP_S = 5
BUSY_GAP_S = 60
IDLE_DONE_GAP_S = 1800
FAILURE_GAP_S = 900
WARN_REPEAT_S = 3600
QUIET_AFTER_USE_S = 120
AWAY_AFTER_S = 600
CALL_TIMEOUT_S = 30.0
MAX_TOKENS = 700
_PROBE_TTL_S = 30.0

_task: asyncio.Task | None = None
_next_at = 0.0
_last_own_end = 0.0
_probe: dict[str, tuple[float, object]] = {}
_warned: tuple[str, float] | None = None       # (reason code, when it last warned)
_REASON = re.compile(r"[A-Za-z0-9_.:-]{1,40}")


# --- settings ---------------------------------------------------------------------

def mode(path: Path | None = None) -> str:
    path = SETTINGS_PATH if path is None else path
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "live"
    except Exception:  # noqa: BLE001 — unreadable: the user's choice is unknown, so stop
        return "off"
    value = data.get("mode") if isinstance(data, dict) else None
    return value if value in MODES else "off"


def set_mode(value: str, path: Path | None = None) -> str:
    if value not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    path = SETTINGS_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mode": value}, f)
    tmp.replace(path)
    return value


# --- idle levels ------------------------------------------------------------------

def _cached(name: str, fn):
    now = time.monotonic()
    hit = _probe.get(name)
    if hit and now - hit[0] < _PROBE_TTL_S:
        return hit[1]
    value = fn()
    _probe[name] = (now, value)
    return value


def _hid_idle_s() -> float | None:
    """Seconds since the last keyboard or mouse input (IOHIDSystem; needs no permission)."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(m.group(1)) / 1e9 if m else None


def _power() -> tuple[bool, int | None, bool]:
    """(on AC power, battery percent or None, Low Power Mode on)."""
    try:
        batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
        general = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return True, None, False
    pct = re.search(r"(\d{1,3})%", batt)
    low = re.search(r"\blowpowermode\s+1\b", general) is not None
    return "AC Power" in batt or pct is None, int(pct.group(1)) if pct else None, low


_UNSET = object()


def level(model_key: str, *, hid_idle_s=_UNSET, power=_UNSET, now: float | None = None) -> str:
    """busy, between or away (see the module docstring). Pass the probes in from a
    worker thread; left unset they are read here (tests and callers off the loop)."""
    if idle.foreground_busy() or idle.in_flight(model_key) > 0:
        return "busy"
    now = time.monotonic() if now is None else now
    last = idle.last_used(model_key)
    if last is not None and last > _last_own_end + 1 and now - last < QUIET_AFTER_USE_S:
        return "busy"
    on_ac, pct, low = _cached("power", _power) if power is _UNSET else power
    if low or (not on_ac and pct is not None and pct < 30):
        return "busy"
    seconds = _cached("hid", _hid_idle_s) if hid_idle_s is _UNSET else hid_idle_s
    if seconds is not None and seconds >= AWAY_AFTER_S and (on_ac or (pct or 0) > 50):
        return "away"
    return "between"


# --- the model call -----------------------------------------------------------------

def _model_free(key: str) -> bool:
    """No turn and no other call on the model right now."""
    return not idle.foreground_busy() and idle.in_flight(key) == 0


async def _stand_down() -> str | None:
    """Why the fast model can't take a call now, or None when it can: a remote-only
    profile, the model not resident, something else resident, or the model busy."""
    from service.inference.remote_policy import remote_only_profile
    if remote_only_profile() is not None:
        return "remote_only"
    from service.assistant.brief import _c
    from service.config import role_to_model
    model = role_to_model("fast")
    client = _c()
    loaded = set(await client.loaded_models())
    try:
        warm = set(client.keep_warm())
    except Exception:  # noqa: BLE001
        warm = set()
    if model not in loaded or loaded - warm - {model}:
        return "not_resident"
    if not _model_free(_model_key()):
        return "busy"
    return None


async def _reply(msgs: list[dict]) -> str | None:
    """The fast model's reply, or None to stand down (a remote-only profile, not
    resident, something else resident, a turn started, or another call on the
    model). Mirrors mail_gists._resident_reply."""
    global _last_own_end
    if await _stand_down() is not None:
        return None
    from service.assistant.brief import _c
    from service.config import role_to_model, thinking_kwargs
    model = role_to_model("fast")
    client = _c()
    key = _model_key()
    # Sampling is left to the engine's profile for this model: the 2026-10-26 bench
    # measured these prompts at the production profile, not at temperature 0.
    request = asyncio.ensure_future(client.chat(model, msgs, max_tokens=MAX_TOKENS,
                                                **thinking_kwargs(model, on=False)))
    deadline = time.monotonic() + CALL_TIMEOUT_S
    try:
        while not request.done():
            # Ours counts as one; anything more is a turn or another background caller.
            if idle.foreground_busy() or idle.in_flight(key) > 1:
                request.cancel()
                return None
            if time.monotonic() >= deadline:
                request.cancel()
                raise TimeoutError("mail memory call took too long")
            await asyncio.wait({request}, timeout=0.5)
        response = request.result()
    finally:
        if not request.done():
            request.cancel()
        _last_own_end = time.monotonic()
    choice = response["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError(f"reply did not complete ({choice.get('finish_reason')})")
    content = (choice.get("message") or {}).get("content")
    if not isinstance(content, str):
        raise ValueError("reply had no text")
    return content


# --- failure visibility ---------------------------------------------------------------

def _code(reason: object) -> str:
    """A reason code and nothing else: the app's error string is untrusted, so
    anything that isn't a short token is reported as `other`."""
    text = str(reason)[:40]
    return text if _REASON.fullmatch(text) else "other"


def note_failure(reason: object, *, now: float | None = None, warn: bool = True) -> None:
    """Remember why mail memory could not read, and say so in the log. Never raises."""
    global _warned
    now = time.time() if now is None else now
    code = _code(reason)
    try:
        st = store()
        st.set_meta("last_error", code)
        st.set_meta("last_error_at", str(now))
    except Exception:  # noqa: BLE001 — bookkeeping must not break the scheduler
        log.exception("mail memory: could not record the failure")
    if not warn:
        return
    fresh = _warned is None or _warned[0] != code or now - _warned[1] >= WARN_REPEAT_S
    if fresh:
        _warned = (code, now)
    log.log(logging.WARNING if fresh else logging.INFO, "mail memory: read failed (%s)", code)


def note_ok(*, now: float | None = None) -> None:
    """A read worked: clear the last error so a stale one never lingers. The warning
    limit is kept: one email that keeps timing out between good reads still warns at
    most once an hour."""
    now = time.time() if now is None else now
    try:
        st = store()
        if st.meta("last_error"):
            st.set_meta("last_error", "")
            st.set_meta("last_error_at", "")
            log.info("mail memory: reads are working again")
        st.set_meta("last_ok_at", str(now))
    except Exception:  # noqa: BLE001
        log.exception("mail memory: could not record the recovery")


# --- the tick -----------------------------------------------------------------------

async def _gmail_excluded() -> frozenset:
    """Message-IDs Gmail filed under Promotions, Social or Forums, for connected Gmail
    accounts. Unknown (not connected, or a read failed) leaves Mail's own categories
    and the header guess to decide."""
    try:
        from service import gmail_source
        found = await gmail_source.tab_message_ids(WINDOW_DAYS)
    except Exception:  # noqa: BLE001
        found = None
    return frozenset(found or ())


def _model_key() -> str:
    from service.assistant.brief import _c
    from service.config import role_to_model
    model = role_to_model("fast")
    try:
        return _c().activity_key(model)
    except Exception:  # noqa: BLE001
        return model


async def tick(*, now: float | None = None) -> float:
    """One unit of work. Returns the seconds until the next tick may run."""
    now = time.time() if now is None else now
    st = store()
    from service.assistant.attention_runner import current_zone
    tz = current_zone()
    if tz is None:
        return FAILURE_GAP_S
    today = datetime.fromtimestamp(now, ZoneInfo(tz)).date()
    # ioreg and pmset run off the event loop: a slow probe must never stall a turn.
    power = await asyncio.to_thread(_cached, "power", _power)
    hid = await asyncio.to_thread(_cached, "hid", _hid_idle_s)
    lvl = level(_model_key(), hid_idle_s=hid, power=power)
    gap = {"busy": BUSY_GAP_S, "away": AWAY_GAP_S}.get(lvl, BETWEEN_GAP_S)
    from service.inference.remote_policy import remote_only_profile
    if remote_only_profile() is not None:
        return FAILURE_GAP_S                      # the walk stands down entirely

    # The index first: it costs no model time, so a busy or absent model never holds
    # it up (2026-10-27: a morning of use with Brief and proactive work kept the old
    # order from reading even the index, so nothing queued and nothing failed).
    on_ac, pct, low = power
    saving = low or (not on_ac and pct is not None and pct < 30)
    last_index = float(st.meta("last_index") or 0)
    if not saving and now - last_index >= INDEX_EVERY_S:
        rows, capped = await read_index(WINDOW_DAYS)
        excluded = await _gmail_excluded()
        merged = await asyncio.to_thread(st.upsert_index, rows, lambda r: extract.tier(r, excluded), now)
        merged["gmail_tab"] = await asyncio.to_thread(st.exclude, excluded) if excluded else 0
        st.set_meta("last_index", str(now))
        note_ok(now=now)
        log.info("mail memory index: %s capped=%s gmail_tab_exclusions=%d", merged, capped, len(excluded))
        return gap

    if lvl == "busy":
        return BUSY_GAP_S
    # Read no mail the model can't take: a body read now would only be read again.
    why_not = await _stand_down()
    if why_not == "remote_only":
        return FAILURE_GAP_S
    if why_not is not None:
        return BUSY_GAP_S

    st.expire(today.isoformat())
    row = st.next_pending(now)
    if row is None:
        return IDLE_DONE_GAP_S
    try:
        raw, why = await read_body(row["rowid"])
    except BridgeError as exc:
        if str(exc) == "app_not_connected":
            raise
        st.fail(row["key"], str(exc), now)         # this email's read timed out
        note_failure(exc, now=now)
        return gap
    if raw is None:
        if why in ("index_unreadable", "no_mail_index"):
            note_failure(why, now=now)
            return FAILURE_GAP_S                  # Mail's index, not this email: try later
        note_ok(now=now)                          # the app answered; only this email is unreadable
        if why in ("not_on_disk", "not_in_inbox_or_sent"):
            # Gone from disk, or moved to Trash, Archive or Junk since the index: never read.
            st.finish(row["key"], "skipped", why)
        else:
            st.fail(row["key"], why, now)
        return gap
    note_ok(now=now)
    try:
        # Off the loop: a 256 KB HTML body is parsed in pure Python, which yields the GIL.
        email = await asyncio.to_thread(extract.parse_raw, raw, row["ts"])
    except Exception as exc:  # noqa: BLE001 — a malformed message is this email's problem only
        st.fail(row["key"], f"parse:{type(exc).__name__}", now)
        return gap
    if row.get("rfc_message_id") and email.message_id and email.message_id != row["rfc_message_id"]:
        # Mail rebuilt its index since the last refresh: this ROWID is another email now.
        st.set_meta("last_index", "0")
        log.info("mail memory: index moved under us; refreshing")
        return gap
    if len(email.body) < 20:
        st.finish(row["key"], "skipped", "empty_body", email.message_id)
        return gap
    from service.memory.identity import identity_prompt_block
    msgs = extract.messages(email, sent_by_user=row["box"] == "sent",
                            identity=identity_prompt_block(messages=False), today=today)
    try:
        reply = await _reply(msgs)
    except (TimeoutError, ValueError, KeyError, IndexError, TypeError) as exc:
        st.fail(row["key"], f"model:{type(exc).__name__}", now)   # truncated, empty or slow
        return gap
    if reply is None:
        return BUSY_GAP_S
    items = extract.parse_reply(reply)
    if items is None:
        st.fail(row["key"], "bad_json", now)
        return gap
    kept, dropped = extract.validate(items, email, tz=tz, today=today)
    why: dict[str, int] = {}
    for it in kept:
        it["important"], it["why"] = importance.judge(it, row)
        why[it["why"]] = why.get(it["why"], 0) + 1
    added = st.add_items(row["key"], kept, extract.EXTRACTOR, now)
    st.finish(row["key"], "done", f"model={len(items)} kept={len(kept)} added={added}", email.message_id)
    st.set_meta("last_tick", str(now))
    log.info("mail memory tick: tier=%s box=%s model_items=%d kept=%d added=%d dropped=%s why=%s level=%s",
             row["tier"], row["box"], len(items), len(kept), added, dropped, why, lvl)
    return gap


async def _run() -> None:
    global _next_at
    try:
        gap = await tick()
    except BridgeError as exc:
        note_failure(exc)
        gap = FAILURE_GAP_S
    except Exception as exc:  # noqa: BLE001 — never break the scheduler; try again later
        # The type and where it was raised, never the message or a traceback: one built
        # from mail data would land in moe-backend.log and Report a Problem bundles.
        frames = traceback.extract_tb(exc.__traceback__)
        where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "?"
        log.error("mail memory tick failed: %s at %s", type(exc).__name__, where)
        note_failure(f"unexpected:{type(exc).__name__}", warn=False)
        gap = FAILURE_GAP_S
    _next_at = time.time() + gap


def _other_background_work() -> bool:
    """The Brief, its gists or the proactive engine has a model task going. They hold
    each other off the same way; mail memory waits for all of them."""
    try:
        from service.assistant import scheduler
        brief = scheduler._brief_task
        if brief is not None and not brief.done():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from service.assistant import mail_gists
        if mail_gists._task is not None and not mail_gists._task.done():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from service.proactive import cycle
        return bool(cycle.running())
    except Exception:  # noqa: BLE001 — not in every build
        return False


def schedule_tick() -> None:
    """Called from the assistant scheduler every 30 s; starts at most one tick."""
    global _task
    if _task is not None and not _task.done():
        return
    if time.time() < _next_at or mode() == "off" or idle.foreground_busy() or _other_background_work():
        return
    from service.assistant.hub import hub
    if not hub.has_subscribers:
        return
    _task = asyncio.create_task(_run())
