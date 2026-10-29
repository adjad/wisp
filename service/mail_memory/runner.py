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
           Calls run back to back, 5 s apart on power; on battery at least
           AWAY_BATTERY_GAP_S apart, so an unplugged night doesn't drain it.

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
- Profile facts (profile_facts.py) ride on the same tick: after the reminder
  call, a second short call runs only for an email that can carry a fact, and
  only if the model is still free. Read mail whose fact pass never ran is
  picked up once the reminder queue is empty. Facts are saved to facts.db
  only in "live" mode (shadow collects candidates only); the `facts` setting
  turns the pass off.
- Logs and the store's meta carry counts and reason codes only, never mail text.
- A failed tick is never silent: the reason code is kept in meta (`last_error`,
  `last_error_at`, cleared by the next good read) and logged at WARNING, which
  reaches moe-backend.log (INFO does not: nothing configures the root logger, so
  Python's last-resort handler prints WARNING and above to stderr). A reason
  warns when it first appears or changes, and again at most once an hour while
  it repeats; other repeats log at INFO.
- A stand-down is never silent either: its reason code (`busy_reason`, the
  stand-down codes, or a scheduler gate) is kept in meta (`last_skip`, and
  `last_skip_at` for when that reason began), so GET /assistant/mail_memory
  names the gate behind a stall. It is written when the reason changes and at
  most every SKIP_REWRITE_S while it repeats, and cleared by the next step.
- On battery the walk keeps running (between, or away on battery over 50%),
  its calls capped by the BACKGROUND power tier; only Low Power Mode or battery
  under 30% stops it.
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
from service.mail_memory import extract, importance, profile_facts
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
AWAY_BATTERY_GAP_S = 60
BUSY_GAP_S = 60
IDLE_DONE_GAP_S = 1800
FAILURE_GAP_S = 900
WARN_REPEAT_S = 3600
SKIP_REWRITE_S = 600
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
_skip: tuple[str, float] | None = None          # (reason code kept in meta, when it was written)
_REASON = re.compile(r"[A-Za-z0-9_.:-]{1,40}")


# --- settings ---------------------------------------------------------------------

def _settings(path: Path) -> dict | None:
    """The settings file's object; {} when it doesn't exist; None when it can't be read."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def mode(path: Path | None = None) -> str:
    data = _settings(SETTINGS_PATH if path is None else path)
    if data is None:
        return "off"                  # unreadable: the user's choice is unknown, so stop
    value = data.get("mode", "live")
    return value if value in MODES else "off"


def facts_enabled(path: Path | None = None) -> bool:
    """The profile-fact pass (profile_facts.py): on unless the user turned it off.
    An unreadable file means off, like the mode."""
    data = _settings(SETTINGS_PATH if path is None else path)
    return data is not None and data.get("facts", True) is True


def _write_settings(path: Path, **changes) -> None:
    data = _settings(path) or {}
    data.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    tmp.replace(path)


def set_mode(value: str, path: Path | None = None) -> str:
    if value not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    _write_settings(SETTINGS_PATH if path is None else path, mode=value)
    return value


def set_facts(enabled: bool, path: Path | None = None) -> bool:
    _write_settings(SETTINGS_PATH if path is None else path, facts=bool(enabled))
    return bool(enabled)


# --- idle levels ------------------------------------------------------------------

def _cached(name: str, fn):
    now = time.monotonic()
    hit = _probe.get(name)
    if hit and now - hit[0] < _PROBE_TTL_S:
        return hit[1]
    value = fn()
    _probe[name] = (now, value)
    return value


def _read_hid_idle_s() -> float | None:
    """Seconds since the last keyboard or mouse input (IOHIDSystem; needs no permission).
    Only the one key, one level deep: the whole IOHIDSystem subtree is ~5x the output
    for the same number, and power_tier.refresh_away runs it every scheduler tick
    while "Save power when away" is on."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-r", "-k", "HIDIdleTime", "-d", "1"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out or "")
    return int(m.group(1)) / 1e9 if m else None


def _hid_idle_s() -> float | None:
    """A new idle reading, kept as the walk's cached one: power_tier.refresh_away
    reads it every scheduler tick, so the walk rarely runs ioreg itself."""
    value = _read_hid_idle_s()
    _probe["hid"] = (time.monotonic(), value)
    return value


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


def busy_reason(model_key: str, *, power=_UNSET, now: float | None = None) -> str | None:
    """Why the walk is busy, as a reason code, or None: foreground (a user's turn),
    in_flight (a call on the model), recent_use (someone else used it in the last
    QUIET_AFTER_USE_S), low_power (Low Power Mode) or battery_low (under 30%).
    Battery alone is never a reason."""
    if idle.foreground_busy():
        return "foreground"
    if idle.in_flight(model_key) > 0:
        return "in_flight"
    now = time.monotonic() if now is None else now
    last = idle.last_used(model_key)
    if last is not None and last > _last_own_end + 1 and now - last < QUIET_AFTER_USE_S:
        return "recent_use"
    on_ac, pct, low = _cached("power", _power) if power is _UNSET else power
    if low:
        return "low_power"
    if not on_ac and pct is not None and pct < 30:
        return "battery_low"
    return None


def level(model_key: str, *, hid_idle_s=_UNSET, power=_UNSET, now: float | None = None) -> str:
    """busy, between or away (see the module docstring). Pass the probes in from a
    worker thread; left unset they are read here (tests and callers off the loop)."""
    if power is _UNSET:
        power = _cached("power", _power)
    if busy_reason(model_key, power=power, now=now) is not None:
        return "busy"
    on_ac, pct, _low = power
    seconds = _cached("hid", _hid_idle_s) if hid_idle_s is _UNSET else hid_idle_s
    if seconds is not None and seconds >= AWAY_AFTER_S and (on_ac or (pct or 0) > 50):
        return "away"
    return "between"


def step_gap(lvl: str, power: tuple[bool, int | None, bool]) -> float:
    """Seconds until the next walk step at ``lvl``. Away runs back to back only
    on power: on battery it waits AWAY_BATTERY_GAP_S."""
    if lvl == "away":
        return AWAY_GAP_S if power[0] else AWAY_BATTERY_GAP_S
    return BUSY_GAP_S if lvl == "busy" else BETWEEN_GAP_S


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
    from service.inference.power_tier import BACKGROUND, power_tier
    with power_tier(BACKGROUND):
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


def note_skip(reason: str | None, *, now: float | None = None) -> None:
    """Remember why the walk stood down (None: it ran). Written when the reason
    changes and at most every SKIP_REWRITE_S while it repeats; `last_skip_at` is
    when the current reason began. Never raises."""
    global _skip
    now = time.time() if now is None else now
    code = _code(reason) if reason else ""
    if _skip is not None and _skip[0] == code and (not code or now - _skip[1] < SKIP_REWRITE_S):
        return
    try:
        st = store()
        if not code and not st.meta("last_skip"):
            _skip = ("", now)
            return
        if _skip is None or _skip[0] != code:
            st.set_meta("last_skip_at", str(now) if code else "")
            if code:
                log.info("mail memory: standing down (%s)", code)
        st.set_meta("last_skip", code)
        _skip = (code, now)
    except Exception:  # noqa: BLE001 — bookkeeping must not break the scheduler
        log.exception("mail memory: could not record the stand-down")


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
    busy = busy_reason(_model_key(), power=power)
    lvl = "busy" if busy else level(_model_key(), hid_idle_s=hid, power=power)
    gap = step_gap(lvl, power)
    from service.inference.remote_policy import remote_only_profile
    if remote_only_profile() is not None:
        note_skip("remote_only", now=now)
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
        if rows and not capped:
            merged["accounts_forgotten"] = await asyncio.to_thread(_forget_gone_accounts, st, rows, now)
        st.set_meta("last_index", str(now))
        note_ok(now=now)
        log.info("mail memory index: %s capped=%s gmail_tab_exclusions=%d", merged, capped, len(excluded))
        return gap

    if lvl == "busy":
        note_skip(busy or "busy", now=now)
        return BUSY_GAP_S
    # Read no mail the model can't take: a body read now would only be read again.
    why_not = await _stand_down()
    if why_not is not None:
        note_skip(why_not, now=now)
        return FAILURE_GAP_S if why_not == "remote_only" else BUSY_GAP_S
    note_skip(None, now=now)

    st.expire(today.isoformat())
    row = st.next_pending(now)
    facts_only = False
    if row is None:
        row = st.next_facts_pending(profile_facts.eligible) if facts_enabled() else None
        if row is None:
            if mode() == "live" and facts_enabled():
                await asyncio.to_thread(_promote, st, now, None)
            return IDLE_DONE_GAP_S
        facts_only = True
    try:
        raw, why = await read_body(row["rowid"])
    except BridgeError as exc:
        if str(exc) == "app_not_connected":
            raise
        if facts_only:
            st.set_facts_state(row["key"], "failed")   # never re-queue mail already read
        else:
            st.fail(row["key"], str(exc), now)         # this email's read timed out
        note_failure(exc, now=now)
        return gap
    if raw is None:
        if why in ("index_unreadable", "no_mail_index"):
            note_failure(why, now=now)
            return FAILURE_GAP_S                  # Mail's index, not this email: try later
        note_ok(now=now)                          # the app answered; only this email is unreadable
        if facts_only:
            st.set_facts_state(row["key"], "failed" if why not in ("not_on_disk", "not_in_inbox_or_sent")
                               else "skipped")
        elif why in ("not_on_disk", "not_in_inbox_or_sent"):
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
        if facts_only:
            st.set_facts_state(row["key"], "failed")
        else:
            st.fail(row["key"], f"parse:{type(exc).__name__}", now)
        return gap
    if row.get("rfc_message_id") and email.message_id and email.message_id != row["rfc_message_id"]:
        # Mail rebuilt its index since the last refresh: this ROWID is another email now.
        st.set_meta("last_index", "0")
        log.info("mail memory: index moved under us; refreshing")
        return gap
    if len(email.body) < 20:
        if facts_only:
            st.set_facts_state(row["key"], "skipped")
        else:
            st.finish(row["key"], "skipped", "empty_body", email.message_id)
        return gap
    from service.memory.identity import identity_prompt_block
    if facts_only:
        await _facts_pass(st, row, email, today, now, identity_prompt_block(messages=False))
        return gap
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
    try:
        kept, dropped = extract.validate(items, email, tz=tz, today=today)
        why: dict[str, int] = {}
        for it in kept:
            it["important"], it["why"] = importance.judge(it, row)
            why[it["why"]] = why.get(it["why"], 0) + 1
    except Exception as exc:  # noqa: BLE001 — e.g. a date phrase the resolver can't take
        # This email's problem only: retire it like any other failure, so it can't
        # hold the queue (it used to escape tick() and be retried forever).
        st.fail(row["key"], f"validate:{type(exc).__name__}", now)
        return gap
    added = st.add_items(row["key"], kept, extract.EXTRACTOR, now)
    st.finish(row["key"], "done", f"model={len(items)} kept={len(kept)} added={added}", email.message_id)
    st.set_meta("last_tick", str(now))
    log.info("mail memory tick: tier=%s box=%s model_items=%d kept=%d added=%d dropped=%s why=%s level=%s",
             row["tier"], row["box"], len(items), len(kept), added, dropped, why, lvl)
    if any(it.get("suspicious") for it in kept):
        st.set_facts_state(row["key"], "skipped")          # an email that tries to instruct gives no facts
    elif facts_enabled():
        await _facts_pass(st, row, email, today, now, identity_prompt_block(messages=False))
    return gap


def _own_addresses() -> list[str]:
    try:
        from service.memory.identity import user_emails
        return list(user_emails())
    except Exception:  # noqa: BLE001 — unknown addresses: nothing to check against
        return []


def _promote(st, now: float, groups) -> dict | None:
    from service.memory.facts import store as facts
    return profile_facts.promote(st, facts, now=now, groups=groups)


GONE_RUNS = 3
GONE_SPAN_S = 2 * 86400
QUIET_MARGIN_DAYS = 14


def _forget_gone_accounts(st, rows: list[dict], now: float) -> dict | None:
    """Forget what was learned from a Mail account that has left Mail
    (profile_facts.forget_accounts). An account counts as gone only when it
    should still be in the window (it had mail in the last WINDOW_DAYS -
    QUIET_MARGIN_DAYS days, so a quiet account never counts) and it is missing
    from GONE_RUNS complete index reads spanning GONE_SPAN_S or more: a re-import
    after a macOS update or a mailbox rebuild doesn't erase anything."""
    seen = {r.get("account", "") for r in rows}
    try:
        missing = json.loads(st.meta("accounts_missing") or "{}")
    except ValueError:
        missing = {}
    newest = st.account_newest()
    still, gone = {}, set()
    for account in st.candidate_accounts() - seen - {""}:
        if now - newest.get(account, 0) > (WINDOW_DAYS - QUIET_MARGIN_DAYS) * 86400:
            continue                               # quiet, not gone: it would fall out of the window anyway
        first, runs = (missing.get(account) or [now, 0])[:2]
        runs += 1
        if runs >= GONE_RUNS and now - first >= GONE_SPAN_S:
            gone.add(account)
        else:
            still[account] = [first, runs]
    st.set_meta("accounts_missing", json.dumps(still))
    if not gone:
        return None
    from service.memory.facts import store as facts
    return profile_facts.forget_accounts(st, facts, gone)


async def _facts_pass(st, row: dict, email, today, now: float, identity: str) -> None:
    """The profile-fact call for one email (profile_facts.py). Never raises: a
    failure here is this email's fact pass only, never its reminders."""
    if not profile_facts.eligible(row):
        st.set_facts_state(row["key"], "skipped")
        return
    expect = profile_facts.generation(st)
    sent = row["box"] == "sent"
    msgs = profile_facts.messages(email, sent_by_user=sent, identity=identity, today=today)
    try:
        reply = await _reply(msgs)
    except (TimeoutError, ValueError, KeyError, IndexError, TypeError):
        st.set_facts_state(row["key"], "failed")
        return
    if reply is None:
        st.set_facts_state(row["key"], "pending")         # a turn started: try again later
        return
    if not facts_enabled() or mode() == "off" or profile_facts.generation(st) != expect:
        # Switched off, or everything from mail was forgotten, while the model ran.
        st.set_facts_state(row["key"], "skipped")
        return
    candidates = profile_facts.parse_reply(reply)
    if candidates is None:
        st.set_facts_state(row["key"], "failed")
        return
    try:
        to_user = profile_facts.addressed_to(email, await asyncio.to_thread(_own_addresses))
        kept, dropped = profile_facts.validate(candidates, email, sent_by_user=sent, to_user=to_user)
        from service.memory.facts import store as facts
        saved = await asyncio.to_thread(
            profile_facts.record, st, facts, key=row["key"], account=row.get("account", ""),
            sent_ts=email.sent.timestamp(), kept=kept, now=now, expect=expect,
            save=mode() == "live" and facts_enabled())
    except Exception as exc:  # noqa: BLE001 — this email's fact pass only
        log.warning("mail memory facts failed: %s", type(exc).__name__)
        st.set_facts_state(row["key"], "failed")
        return
    st.set_facts_state(row["key"], "done" if saved is not None else "skipped")
    log.info("mail memory facts: box=%s model=%d kept=%d dropped=%s saved=%s",
             row["box"], len(candidates), len(kept), dropped, saved)


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
    if time.time() < _next_at or mode() == "off":
        return
    if idle.foreground_busy():
        note_skip("foreground")
        return
    if _other_background_work():
        note_skip("other_background")
        return
    from service.assistant.hub import hub
    if not hub.has_subscribers:
        note_skip("no_subscribers")
        return
    _task = asyncio.create_task(_run())
