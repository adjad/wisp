"""Round-3 families ``device_r3``, ``contacts_r3`` and ``reminders_r3``.

Why these exist. Toolbench (378 real-life cases, run 20261101) scored untuned Gemma
device 21/33, contacts 9/12, reminders 19/28; r2 (the Ask LoRA) regressed contacts and
device by one case each. The failing rows of both runs
(~/.moe/evals/toolbench/20261101-151918 untuned, 20261101-123417 r2) fall into these causes:

device
  - manage_timers called with no ``action`` (list / time-left / cancel-all all arrived as
    ``manage_timers({})``, "action: missing"); r2 invented ``duration_min`` / ``title`` /
    ``duration_minutes`` argument names on manage_timers and set_timer, never recovered, and
    ended "I don't have a start timer command".
  - "any timers running" answered from ``search_reminders`` (a timer is not a reminder);
    "kill all my timers" went to ``clear_past_reminders`` (an unsafe call, no card); a
    cancel-by-name final that never said "cancelled".
  - "turn it down": ``mac_get_volume`` twice and no ``mac_set_volume`` (the read was
    never followed by the write).
  - answers that were not the tool's numbers: disk space and the macOS version restated
    from memory ("macOS 14.3.1") instead of what ``system_status`` printed; calculations
    and unit conversions answered from memory with no ``calculate`` / ``convert_units`` call
    ("$24 each", "224 kg"); ``world_time`` called without its ``place``.
  - a status question ("is X open") answered with a different shape than the evidence.

contacts
  - birthday: r2 called ``lookup_contact`` (names and numbers only) and then said the
    birthday was not known; the birthday tool is ``contact_dates``, and the named person's
    date must be read out of its list exactly.
  - saving a number for someone not in Contacts: ``add_phone`` came back "No contact
    matches", and the model stopped; the fix is ``create`` then ``add_phone``.
  - a bare statement of a contact fact ("its <email> btw") was refused with a
    receipt-style non-reply instead of ``manage_contacts add_email``; the other direction,
    a phone-only contact, had its number used as the email address in ``send_email``.
  - untuned Gemma wrote a manage_contacts call with ``name`` / ``email`` keys and no
    ``action`` ("action: missing; value: missing").

reminders
  - asked for a time that the user had already given as words ("friday morning",
    "tomorrow"): the product resolver turns those into a time, so the right move is to
    call ``add_reminder`` at once with an ISO ``when_iso``.
  - ``when_iso`` malformed: a date alone ("2026-11-02") or the user's words
    ("tomorrow 12 PM") came back "bad when_iso"; the title was mangled ("about the invoice").
  - "what's pending" went to ``list_scheduled_sends`` (a send queue, not reminders); an
    "overdue" answer listed reminders that were not overdue; a filing-away answer named a
    reminder that the tool had hidden.
  - clears: "clear all of my reminders" and "delete my reminders for today" went to
    ``clear_past_reminders`` (wrong scope: it deletes every past-due item); the right tool
    is ``clear_reminders`` with the named scope (it raises a card). Removing a named stale
    reminder ended after a search with "nothing was removed" instead of the delete.
  - a reminder removed by name must be ``cancel_event``; "done" is ``complete_reminder``.

Host facts measured on this branch and scripted around (the dry-run in ``_attempt`` drops any
candidate the host would not run as scripted; DROPPED lists them):
  - mac_* skill tools (volume, mute, lock, media) cannot be verified in the loop today: the loop
    wants a BuiltinCompletion from the tool, ``registry.run_tool`` turns it into a str for any
    tool outside BUILTIN_COMPLETION_MODULES, so the host ends the turn with "Action did not return
    a verified completion receipt" (this is also toolbench's dev_mac_volume_down no_effect). They
    are an opaque outbound action, so a card is raised. Those turns stop at the call (the step
    that matters); there are no mac_* finals in this data.
  - a second call to the same effect tool in one turn is not run ("toggle_setting already
    succeeded earlier this turn"), so two toggles in a turn cannot be built; manage_contacts
    create then add_phone does run.
  - a failed or ambiguous write result (cancel with no match, complete_reminder with two
    matches, add_phone to an unknown contact) ends the turn with the host's own text, so those
    are not scripted; the model asks or looks first instead (lookup_contact before a contact write).
  - clear_reminders / clear_past_reminders run only when the prompt names "reminders"; a stub call
    with empty arguments is a schema request, so ``clear_past_reminders`` is called with days=365.
  - "how long / how much time ..." questions leave the turn with no tools at all (the time-question
    guard), so timer questions are worded without them.

The scenarios below use new wordings, the Sam Rivera world, and the tools' real result
formats (copied from service/tools/*.py); the device tools, contact_dates and a few reminder
handlers are installed per scenario through ``World.overrides``.

Families
  device_r3     volume, mute, Do Not Disturb / Focus, single-setting toggles, timers
                (set, alarm, sleep, list, time left, cancel one / all, stopwatch), battery /
                disk / memory / macOS / Wi-Fi status, calculate / convert / world_time, and
                the honest "I can't do that" and "which one?" turns.
  contacts_r3   lookup (phone / email / has-no-email), birthdays, two people with one first
                name, creating and updating a contact.
  reminders_r3  add (the user's time words resolved), list open / overdue / due soon,
                complete, rename, move, delete by name, clear a named scope (card),
                reminder-versus-event.

All three families live in this file (the brief names this one), so they share its helpers.
"""
from __future__ import annotations

import datetime as dt
import random
import re

import scen_r2_act as A
from harness import Call, Final
from replies import clock, join
from world import Reminder

D = dt.timedelta
DROPPED: list[tuple[str, str, str]] = []
WEEKDAYS = A.WEEKDAYS

WRITE_ALLOW_R3 = {
    "device_r3": frozenset({"mac_get_volume", "mac_set_volume", "mac_mute", "toggle_setting", "set_timer", "manage_timers"}),
    "contacts_r3": frozenset({"manage_contacts"}),
    "reminders_r3": frozenset({"add_reminder", "update_reminder", "complete_reminder", "cancel_event",
                               "clear_reminders", "clear_past_reminders", "add_calendar_event"}),
}


# =============================================================================
# plumbing: dry-run every candidate, cap repeated openers, never repeat a prompt
# =============================================================================
_SEEN: set[str] = set()
_OPEN: dict[str, int] = {}
OPEN_CAP = 4


def _okey(text: str) -> str:
    m = re.match(r"[A-Za-z]+", text.strip().lstrip("“\"'"))
    return m.group(0).lower() if m else ""


def pick(rng, opts):
    """rng.choice over final wordings, skipping openers already used OPEN_CAP times."""
    opts = list(opts)
    ok = [o for o in opts if _OPEN.get(_okey(o), 0) < OPEN_CAP]
    return rng.choice(ok or opts)


def _attempt(make, rng, family, *, tries=6):
    for _ in range(tries):
        seed = rng.random()
        sc = make(random.Random(seed))
        if sc is None:
            continue
        key = sc.prompt.strip().lower()
        if key in _SEEN:
            continue
        ok, why = A._check(sc)
        if not ok and getattr(sc, "no_effect", False) and why.startswith("write ") and why.endswith("had no effect"):
            ok = True          # the tool is scripted to fail: that is the behaviour being taught
        if ok:
            _SEEN.add(key)
            kept = make(random.Random(seed))
            if kept.steps and isinstance(kept.steps[-1], Final):
                k = _okey(kept.steps[-1].text)
                _OPEN[k] = _OPEN.get(k, 0) + 1
            return kept
        DROPPED.append((family, sc.prompt, why))
    return None


def _fill_ordered(out, makers, rng, family, *, tries=6):
    for mkr in makers:
        sc = _attempt(mkr, rng, family, tries=tries)
        if sc is not None:
            out.append(sc)


def mk(family, prompt, now, w, steps, notes, *, cards=None, approve=None, must_see=(), no_effect=False):
    sc = A.mk(family, prompt, now, w, steps, notes, approve=approve, cards=cards or [], must_see=must_see)
    sc.no_effect = no_effect
    return sc


def say_dur(mins: float) -> str:
    """'25 minutes' / 'an hour and a half' / '90 seconds' for a final."""
    if mins < 2:
        return f"{int(round(mins * 60))} seconds"
    mins = int(round(mins))
    if mins == 60:
        return "an hour"
    if mins == 90:
        return "an hour and a half"
    if mins % 60 == 0 and mins < 600:
        return f"{mins // 60} hours"
    if mins > 60:
        h, m = divmod(mins, 60)
        return f"{h} hour{'s' if h != 1 else ''} {m} minutes"
    return f"{mins} minute{'s' if mins != 1 else ''}"


def cap1(s):
    return s[:1].upper() + s[1:]


def effect(w, tool, **args):
    w.effects.append((tool, {k: v for k, v in args.items() if v is not None}))


# =============================================================================
# DEVICE: per-scenario handlers (formats copied from service/tools)
# =============================================================================
from service.tools.timers_alarms import _human, _parse_duration  # noqa: E402


class Dev:
    """The Mac's state for one scenario, behind the device tools' real result formats."""

    def __init__(self, w, rng, *, volume=None, muted=False, timers=(), stopwatch=None, battery=None,
                 charging=False, macos=True, ssid="Linden-Student", focus=None, fail=None):
        self.w, self.rng = w, rng
        self.volume = volume if volume is not None else rng.choice([20, 30, 40, 45, 55, 60, 70])
        self.muted = muted
        self.timers = [dict(t) for t in timers]       # kind, label, left (seconds), id
        self.stopwatch = stopwatch                      # elapsed seconds or None
        self.battery = battery if battery is not None else rng.choice([23, 38, 47, 61, 72, 85, 94])
        self.charging = charging
        self.cycles = rng.choice([87, 142, 203, 318, 411])
        self.health = rng.choice([88, 91, 94, 97])
        self.remaining = rng.choice([2 * 60 + 5, 3 * 60 + 40, 5 * 60 + 15, 6 * 60 + 30, 8 * 60 + 5])
        self.free = rng.choice([("118G", "494G", 76), ("212G", "494G", 57), ("341G", "1.0T", 66),
                                ("73G", "494G", 85), ("526G", "1.0T", 47)])
        self.mem_free = rng.choice([22, 31, 44, 58, 67])
        self.load = (round(rng.uniform(0.9, 3.4), 2), round(rng.uniform(1.0, 3.0), 2))
        self.cores = rng.choice([8, 10, 12])
        self.uptime = rng.choice(["2 days, 3:41", "5 days, 11:07", "9:12", "1 day, 22:30", "6 days, 4:58"])
        self.macos = macos
        self.version = rng.choice([("26.0.1", "25A362"), ("26.1", "25B78"), ("26.0", "25A354")])
        self.ssid = ssid
        self.focus = focus                              # name of a Focus that is on, or None
        self.fail = fail or {}                          # setting -> error text
        self.settings = {"wifi": True, "bluetooth": True, "do_not_disturb": bool(focus), "low_power_mode": False,
                         "airdrop": True, "dark_mode": False, "voiceover": False, "firewall": True,
                         "auto_appearance": False}
        self.tid = 0
        w.overrides.update({
            "mac_get_volume": self.get_volume, "mac_set_volume": self.set_volume, "mac_mute": self.mute,
            "system_status": self.status, "toggle_setting": self.toggle, "set_timer": self.set_timer,
            "manage_timers": self.manage_timers})

    # ---- volume
    def get_volume(self, a):
        return f"volume {self.volume}%, muted: {str(self.muted).lower()}"

    def set_volume(self, a):
        lv = a.get("level")
        if not re.fullmatch(r"\d{1,3}", str(lv)) or int(lv) > 100:
            return "(exit code 2)\nlevel must be a whole number from 0 to 100"
        self.volume = int(lv)
        effect(self.w, "mac_set_volume", level=lv)
        return f"volume set to {int(lv)}%"

    def mute(self, a):
        if a.get("state") not in ("on", "off"):
            return "(exit code 2)\nstate must be on or off"
        self.muted = a["state"] == "on"
        effect(self.w, "mac_mute", state=a["state"])
        return "muted" if self.muted else "unmuted"

    # ---- system_status (system_extras.system_status)
    def _battery(self):
        parts = [f"charge: {self.battery}%"]
        if self.charging:
            parts.append("charging")
        else:
            parts.append("on battery power")
            h, m = divmod(self.remaining, 60)
            parts.append(f"~{h}h{m:02d}m remaining")
        parts.append(f"{self.cycles} charge cycles")
        parts.append(f"battery health: {self.health}% of original design capacity (Normal)")
        return "; ".join(parts)

    def status(self, a):
        sec = (a.get("section") or "overview").strip().lower()
        if sec == "battery":
            return self._battery()
        if sec == "network":
            return f"Local IP (Wi-Fi): 10.24.8.113\nWi-Fi network: {self.ssid}\nPublic IP: 203.0.113.58"
        if sec == "bluetooth":
            if not self.settings["bluetooth"]:
                return "Bluetooth: off\nNo devices currently connected."
            return "Bluetooth: on\n1 device(s) connected:\n  - AirPods Pro, signal -52 dBm"
        if sec == "focus":
            if self.focus:
                return f"Focus is on: {self.focus}."
            return ("No Focus is turned on by hand right now. A scheduled or automatic Focus "
                    "(a set time, an app, Sleep) isn't recorded where Wisp can read it.")
        if sec in ("sound", "volume"):
            return f"Output volume: {self.volume}%" + (", muted" if self.muted else "") + ". Input (microphone) volume: 72%."
        if sec != "overview":
            return (f"(error: unknown section {sec!r}. Use overview, battery, network, "
                    "bluetooth, focus, or sound.)")
        lines = [f"Battery: {self._battery()}"]
        if self.macos:
            lines.append(f"System: macOS {self.version[0]} ({self.version[1]})")
        free, total, used = self.free
        lines += ["Installed memory: 24 GB", f"Disk: {free} free of {total} ({used}% used)",
                  f"Memory: {self.mem_free}% free",
                  f"CPU load: {self.load[0]:.2f} (1m), {self.load[1]:.2f} (5m), across {self.cores} cores",
                  f"Uptime: {self.uptime}"]
        return "\n".join(lines)

    # ---- toggle_setting (system_extras)
    def toggle(self, a):
        key = {"dnd": "do_not_disturb", "focus": "do_not_disturb"}.get(a.get("setting"), a.get("setting"))
        on = a.get("on")
        if key not in self.settings:
            return f"(error: I can't toggle {a.get('setting')!r}. Options: {', '.join(sorted(self.settings))}.)"
        if not isinstance(on, bool):
            return "(error calling toggle_setting: `on` must be true or false.)"
        if key in self.fail:
            return self.fail[key]
        self.settings[key] = on
        effect(self.w, "toggle_setting", setting=key, on=on)
        word = "on" if on else "off"
        return {"wifi": f"Wi-Fi turned {word}", "bluetooth": f"Bluetooth turned {word}.",
                "do_not_disturb": f"Do Not Disturb turned {word}.", "low_power_mode": f"Low Power Mode turned {word}.",
                "firewall": f"Firewall turned {word}.", "airdrop": f"AirDrop {'enabled' if on else 'disabled'}.",
                "voiceover": f"VoiceOver turned {word}.",
                "dark_mode": f"Appearance set to {'dark' if on else 'light'}.",
                "auto_appearance": "Appearance set to auto." if on else "Appearance set to light."}[key]

    # ---- timers (timers_alarms)
    def _nid(self):
        self.tid += 1
        return f"{self.rng.randrange(16 ** 8):08x}"

    def set_timer(self, a):
        kind = (a.get("kind") or "timer").strip().lower()
        label = (a.get("label") or "").strip()
        if kind == "alarm":
            raw = (a.get("time") or "").strip().lower().replace(".", ":")
            m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", raw)
            if not m:
                return f"(error: I couldn't read {a.get('time')!r} as a clock time. Try '7:30am' or '18:45'.)"
            hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            when = self.w.now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when <= self.w.now:
                when += D(days=1)
            self.timers.append({"kind": "alarm", "label": label, "left": (when - self.w.now).total_seconds(),
                                "id": self._nid()})
            effect(self.w, "set_timer", kind="alarm", time=a.get("time"), label=label or None)
            return (f"Alarm set for {when.strftime('%-I:%M %p on %A')}"
                    + (" (repeating daily)." if a.get("repeat_daily") else "."))
        if kind not in {"timer", "sleep"}:
            return f"(error: unknown kind {kind!r}. Use timer, alarm, or sleep.)"
        if not str(a.get("duration") or "").strip():
            return "(error: a timer needs a `duration`, e.g. '10 minutes'.)"
        secs = _parse_duration(str(a["duration"]))
        if secs is None:
            return (f"(error: I couldn't read {a['duration']!r} as a length of time. "
                    f"Try '10 minutes', '1h30m', or '90s'.)")
        effect(self.w, "set_timer", kind=kind, duration=a.get("duration"), label=label or None)
        if kind == "sleep":
            self.timers.append({"kind": "sleep", "label": "Sleep timer", "left": secs, "id": self._nid()})
            return f"Playback will pause in {_human(secs)}."
        self.timers.append({"kind": "timer", "label": label, "left": secs, "id": self._nid()})
        name = f" ({label})" if label else ""
        return f"Timer set for {_human(secs)}{name}. I'll notify you when it's up."

    def manage_timers(self, a):
        action = (a.get("action") or "list").strip().lower()
        label = (a.get("label") or "").strip()
        if action.startswith("stopwatch_"):
            sub = action.removeprefix("stopwatch_")
            if sub == "start":
                self.stopwatch = 0
                effect(self.w, "manage_timers", action=action)
                return "Stopwatch started."
            if self.stopwatch is None:
                return "The stopwatch isn't running. Say 'start the stopwatch' first."
            if sub == "stop":
                el, self.stopwatch = self.stopwatch, None
                effect(self.w, "manage_timers", action=action)
                return f"Stopped at {_human(el)}."
            if sub == "lap":
                effect(self.w, "manage_timers", action=action)
                return f"Lap 1: {_human(self.stopwatch)}."
            return f"{_human(self.stopwatch)} elapsed."
        if action == "list":
            if not self.timers:
                return "No timers or alarms are running."
            rows = []
            for t in sorted(self.timers, key=lambda t: t["left"]):
                kind = {"alarm": "Alarm", "sleep": "Sleep timer"}.get(t["kind"], "Timer")
                nm = f" '{t['label']}'" if t["label"] and t["kind"] != "sleep" else ""
                rows.append(f"{kind}{nm} — {_human(t['left'])} left  [{t['id']}]")
            return "\n".join(rows)
        if action == "cancel_all":
            n = len(self.timers)
            self.timers = []
            if n:
                effect(self.w, "manage_timers", action=action)
            return f"Cancelled {n} timer{'s' if n != 1 else ''}." if n else "Nothing to cancel."
        if action == "cancel":
            if not label:
                return "(error: say which timer to cancel — its label or id. Or use action='cancel_all'.)"
            key = label.lower()
            hits = [t for t in self.timers if t["id"] == key or t["label"].lower() == key]
            hits = hits or [t for t in self.timers if key in t["label"].lower()]
            if not hits:
                return f"No timer matching {label!r}. Use action='list' to see what's running."
            if len(hits) > 1:
                names = ", ".join(f"{t['label'] or 'unnamed'} [{t['id']}]" for t in hits)
                return f"That matches several timers — which one? {names}"
            self.timers.remove(hits[0])
            effect(self.w, "manage_timers", action="cancel", label=label)
            nm = f" '{hits[0]['label']}'" if hits[0]["label"] else ""
            return f"Cancelled the timer{nm}."
        return (f"(error: unknown action {action!r}. Use list, cancel, cancel_all, or "
                "stopwatch_start/lap/read/stop.)")


def dev_world(rng, *, hour=None, **kw):
    now, w = A.base_world(rng, **({"hour": hour} if hour is not None else {}))
    return now, w, Dev(w, rng, **kw)


def uptime_say(u):
    """'5 days, 11:07' -> '5 days, 11 hours and 7 minutes'; '9:12' -> '9 hours and 12 minutes'."""
    m = re.fullmatch(r"(?:(\d+) days?, )?(\d+):(\d+)", u)
    days, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
    t = f"{h} hour{'s' if h != 1 else ''} and {mi} minute{'s' if mi != 1 else ''}"
    return f"{days} day{'s' if days != '1' else ''}, {t}" if days else t


def pct(n):
    return f"{n}%"


# ---------------------------------------------------------------- volume
# mac_set_volume / mac_mute are skill tools: the loop raises a card for them (opaque outbound
# action) and, today, cannot verify a skill tool's receipt in this harness (run_tool str()s the
# BuiltinCompletion; see the report), so the turn ends at the call. The scenarios keep the step
# that matters: the right tool with an integer level or state, after a read where one is needed.
def _vend(prompt, now, w, steps, notes, tool):
    return mk("device_r3", prompt, now, w, steps, notes, cards=[tool], approve=[True], no_effect=True)


def _vol_set(i):
    tmpls = ("set the volume to {n}", "volume to {n}% please", "can you put my sound at {n}",
             "bring the volume to {n}", "make the volume {n} percent")

    def make(rng):
        now, w, dev = dev_world(rng)
        n = rng.choice([10, 15, 25, 35, 45, 65, 75, 80, 90])
        prompt = A.style(rng, tmpls[i % len(tmpls)].format(n=n))
        return _vend(prompt, now, w, [[Call("mac_set_volume", level=n)]],
                     "an exact level: mac_set_volume directly (integer level)", "mac_set_volume")
    return make


def _vol_words(i):
    cases = [("put the volume at half", 50), ("turn the volume all the way up", 100),
             ("set the volume to zero", 0), ("max volume please", 100), ("volume to a quarter", 25)]
    prompt, n = cases[i % len(cases)]

    def make(rng):
        now, w, dev = dev_world(rng)
        return _vend(A.style(rng, prompt), now, w, [[Call("mac_set_volume", level=n)]],
                     "volume in words -> an integer level", "mac_set_volume")
    return make


def _vol_rel(direction, i):
    ups = ["make it a bit louder", "can you turn my volume up a little", "bump the sound up", "louder please",
           "i can barely hear anything, turn it up"]
    downs = ["a bit quieter please", "lower the volume a little", "bring the sound down", "quieter please",
             "way too loud, turn it down"]

    def make(rng):
        now, w, dev = dev_world(rng, volume=rng.choice([30, 40, 50, 60, 70]) if direction > 0 else
                                rng.choice([40, 50, 60, 70, 85]))
        new = max(0, min(100, dev.volume + 20 * direction))
        prompt = A.style(rng, (ups if direction > 0 else downs)[i % 5])
        return _vend(prompt, now, w,
                     [[Call("system_status", section="sound")], [Call("mac_set_volume", level=new)]],
                     "relative volume: read the level, then set about 20 away; the write follows the read",
                     "mac_set_volume")
    return make


def _vol_mute(on, i):
    ons = ["mute my mac", "can you mute the sound", "shh, mute everything for me"]
    offs = ["unmute the speakers", "bring the sound back on", "unmute please"]

    def make(rng):
        now, w, dev = dev_world(rng, muted=not on)
        prompt = A.style(rng, (ons if on else offs)[i % 3])
        return _vend(prompt, now, w, [[Call("mac_mute", state="on" if on else "off")]],
                     "mute / unmute: mac_mute (not volume 0)", "mac_mute")
    return make


def _vol_read(i):
    prompts = ["how loud is my mac right now", "what's the volume set to", "is my sound muted or on",
               "where's my volume at"]

    def make(rng):
        now, w, dev = dev_world(rng, muted=rng.random() < 0.3)
        prompt = A.style(rng, prompts[i % len(prompts)])
        if dev.muted:
            final = pick(rng, [f"It's muted, with the volume at {dev.volume}%.", f"The sound is muted. Volume is {dev.volume}%."])
        else:
            final = pick(rng, [f"The volume is at {dev.volume}%, and it isn't muted.", f"Your volume is {dev.volume}%.",
                               f"It's at {dev.volume}% and not muted."])
        return mk("device_r3", prompt, now, w, [[Call("system_status", section="sound")], Final(final)],
                  "volume read: system_status sound; the exact level from the result")
    return make


def _no_brightness(i):
    prompts = ["make my screen brighter", "dim the display a bit", "the screen is too dark, raise the brightness"]

    def make(rng):
        now, w, dev = dev_world(rng)
        prompt = A.style(rng, prompts[i % len(prompts)])
        final = pick(rng, ["I can't change the screen brightness. The brightness keys on your keyboard, or Control Center, will do it.",
                           "Brightness isn't something I can adjust from here. Your keyboard's brightness keys will.",
                           "I can't reach the display brightness. The brightness keys or Control Center can change it."])
        return mk("device_r3", prompt, now, w, [Final(final)],
                  "no brightness tool: say so plainly, no call and no shell guess")
    return make


# ---------------------------------------------------------------- Focus / toggles
SETTINGS = {
    "wifi": ("Wi-Fi", ["wifi", "wi-fi"]),
    "bluetooth": ("Bluetooth", ["bluetooth", "bt"]),
    "do_not_disturb": ("Do Not Disturb", ["do not disturb", "dnd"]),
    "low_power_mode": ("Low Power Mode", ["low power mode", "battery saver"]),
    "airdrop": ("AirDrop", ["airdrop"]),
    "dark_mode": ("dark mode", ["dark mode"]),
    "voiceover": ("VoiceOver", ["voiceover", "the screen reader"]),
}


def _toggle(key, on, i):
    label, names = SETTINGS[key]
    nm = names[i % len(names)]
    if key == "dark_mode":
        ask_on = ["switch to dark mode", "go dark mode", "turn on dark mode"]
        ask_off = ["switch back to light mode", "turn the dark mode off", "light mode please"]
    elif key == "do_not_disturb":
        ask_on = ["turn on do not disturb", f"{nm} on please", "silence my notifications for now", "i need quiet, switch on dnd"]
        ask_off = [f"switch {nm} off", "bring my notifications back", "i'm done focusing, turn dnd off"]
    else:
        ask_on = [f"turn {nm} on", f"switch on {nm}", f"enable {nm}", f"{nm} on please"]
        ask_off = [f"turn {nm} off", f"switch off {nm}", f"disable {nm}", f"{nm} off please"]
    bank = ask_on if on else ask_off

    def make(rng):
        now, w, dev = dev_world(rng, focus=("Do Not Disturb" if key == "do_not_disturb" and not on else None))
        dev.settings[key] = not on
        text = bank[(i + rng.randrange(3)) % len(bank)]
        prompt = A.style(rng, text)
        if key == "dark_mode":
            fin = ("Dark mode is on." if on else "It's back to light mode now.")
            final = pick(rng, [fin, "Done, " + fin[0].lower() + fin[1:]])
        else:
            state = "on" if on else "off"
            final = pick(rng, [f"{label} is {state} now.", f"Done, {label} is {state}.", f"{label} turned {state}.",
                               f"I switched {label} {state}."])
        return mk("device_r3", prompt, now, w, [[Call("toggle_setting", setting=key, on=on)], Final(final)],
                  f"one named setting: toggle_setting {key} on={on}")
    return make


def _toggle_fail(key, on, text, final_a, final_b, prompt):
    def make(rng):
        now, w, dev = dev_world(rng, fail={key: text})
        final = pick(rng, [final_a, final_b])
        return mk("device_r3", A.style(rng, prompt), now, w, [[Call("toggle_setting", setting=key, on=on)], Final(final)],
                  f"{key}: the tool errors; say it did not change and the real reason", no_effect=True)
    return make


def _vague_off(i):
    prompts = ["switch everything off", "turn all my settings off", "kill all the wireless stuff", "disable everything for tonight",
               "shut off all the toggles"]
    finals = ["Which ones do you mean: Wi-Fi, Bluetooth, AirDrop, or Do Not Disturb?",
              "Which settings should I turn off? I can switch Wi-Fi, Bluetooth, AirDrop or Do Not Disturb individually.",
              "That could be a lot of things. Which would you like off: Wi-Fi, Bluetooth or AirDrop?",
              "Tell me which ones: Wi-Fi, Bluetooth, AirDrop, Low Power Mode or Do Not Disturb?",
              "Which of them do you want off? Wi-Fi, Bluetooth and AirDrop are the usual ones."]

    def make(rng):
        now, w, dev = dev_world(rng)
        return mk("device_r3", A.style(rng, prompts[i % len(prompts)]), now, w, [Final(finals[i % len(finals)])],
                  "a vague 'everything off': one short question, no toggle called")
    return make


def _focus_read(i):
    prompts = ["is do not disturb on right now", "am i in a focus mode at the moment", "are my notifications silenced"]

    def make(rng):
        on = rng.random() < 0.5
        now, w, dev = dev_world(rng, focus="Do Not Disturb" if on else None)
        if on:
            final = pick(rng, ["Yes, Do Not Disturb is on.", "Do Not Disturb is on right now."])
        else:
            final = pick(rng, ["No Focus is on right now, though a scheduled one wouldn't show up for me.",
                               "None that I can see. A scheduled or automatic Focus wouldn't appear on my side."])
        return mk("device_r3", A.style(rng, prompts[i % 3]), now, w, [[Call("system_status", section="focus")], Final(final)],
                  "focus read: system_status focus; the scheduled-focus caveat when it reads off")
    return make


# ---------------------------------------------------------------- timers
DUR_SAY = [("25 minutes", "25 minutes", 25), ("ten minutes", "10 minutes", 10), ("45 min", "45 minutes", 45),
           ("half an hour", "30 minutes", 30), ("an hour and a half", "90 minutes", 90), ("two minutes", "2 minutes", 2),
           ("a quarter of an hour", "15 minutes", 15), ("5 mins", "5 minutes", 5), ("90 seconds", "90 seconds", 1.5),
           ("an hour", "60 minutes", 60), ("20 minutes", "20 minutes", 20), ("forty minutes", "40 minutes", 40)]
TIMER_LABELS = ["tea", "laundry", "rice", "stretching", "oven", "reading", "bread", "plank", "break", "revision"]


def _timer_set(i):
    tmpl = ["start a timer for {d}", "can you time {d} for me", "count down {d}", "give me a countdown of {d}",
            "start a timer for {d}, it's for the {lab}", "{d} on the clock for {lab}", "put a timer on for {d}, it's for the {lab}",
            "timer: {d}", "set the timer to {d}"]

    def make(rng):
        now, w, dev = dev_world(rng)
        say, arg, mins = DUR_SAY[i % len(DUR_SAY)]
        lab = TIMER_LABELS[i % len(TIMER_LABELS)]
        t = tmpl[i % len(tmpl)]
        uses = "{lab}" in t
        prompt = A.style(rng, t.format(d=say, lab=lab))
        args = {"duration": arg}
        if uses:
            args["label"] = lab
        name = f" for the {lab}" if uses else ""
        d = say_dur(mins)
        final = pick(rng, [f"Timer set for {d}{name}.", f"Okay, {d}{name}, counting down now.", f"The timer is running: {d}{name}.",
                           f"{cap1(d)} on the clock{name}."])
        return mk("device_r3", prompt, now, w, [[Call("set_timer", **args)], Final(final)],
                  "countdown: set_timer with the spoken length as duration (and a label if one was named)")
    return make


def _alarm_set(i):
    cases = [("wake me up at {t}", "alarm"), ("alarm for {t}", "alarm"), ("set an alarm at {t}", "alarm")]

    def make(rng):
        h, m = rng.choice([(6, 30), (7, 0), (6, 45), (8, 15), (5, 50), (7, 20)])
        now, w, dev = dev_world(rng, hour=rng.choice([20, 21, 22, 23]))
        tstr = f"{h}:{m:02d}am" if m else f"{h}am"
        say = f"{h}:{m:02d}" if m else f"{h}"
        tmpl = cases[i % 3][0]
        prompt = A.style(rng, tmpl.format(t=say + rng.choice(["", "", " am", "am"])))
        when = (now + D(days=1)).strftime("%A")
        clockw = f"{h}:{m:02d} AM"
        final = pick(rng, [f"Your alarm is set for {clockw} on {when}.", f"Alarm set: {clockw}, {when} morning.",
                           f"I set an alarm for {clockw} on {when}."])
        return mk("device_r3", prompt, now, w, [[Call("set_timer", kind="alarm", time=tstr)], Final(final)],
                  "alarm: set_timer kind=alarm with a clock time (late evening, so it lands tomorrow)")
    return make


def _sleep_set(i):
    prompts = ["pause the music in {d}", "stop playback after {d}", "i'll fall asleep to music, stop it in {d}"]
    cases = [("forty minutes", "40 minutes", 40), ("half an hour", "30 minutes", 30), ("an hour", "60 minutes", 60)]

    def make(rng):
        now, w, dev = dev_world(rng)
        say, arg, mins = cases[i % 3]
        final = pick(rng, [f"The music will pause in {say_dur(mins)}.", f"Playback stops in {say_dur(mins)}.",
                           f"Okay, the sleep timer is set for {say_dur(mins)}."])
        return mk("device_r3", A.style(rng, prompts[i % 3].format(d=say)), now, w,
                  [[Call("set_timer", kind="sleep", duration=arg)], Final(final)], "sleep timer: kind=sleep")
    return make


def _timers_running(i):
    prompts = ["what timers do i have going", "are there any timers on", "check if i have a timer running",
               "which alarms or timers are active"]

    def make(rng):
        n = rng.choice([0, 1, 2, 2, 3])
        labs = rng.sample(TIMER_LABELS, n)
        timers = [{"kind": "timer", "label": lb, "left": rng.choice([95, 240, 600, 900, 1500, 2700]), "id": f"{rng.randrange(16**8):08x}"}
                  for lb in labs]
        now, w, dev = dev_world(rng, timers=timers)
        listing = sorted(timers, key=lambda t: t["left"])
        if not listing:
            final = pick(rng, ["Nothing is running right now.", "No timers or alarms are running."])
        elif len(listing) == 1:
            t = listing[0]
            final = pick(rng, [f"One timer: {t['label']}, with {_human(t['left'])} left.",
                               f"Just the {t['label']} timer, {_human(t['left'])} left."])
        else:
            parts = join([f"{t['label']} with {_human(t['left'])} left" for t in listing])
            final = pick(rng, [f"You have {len(listing)} running: {parts}.", f"There are {len(listing)} timers: {parts}."])
        return mk("device_r3", A.style(rng, prompts[i % 4]), now, w, [[Call("manage_timers", action="list")], Final(final)],
                  "timer inventory: manage_timers list (never search_reminders); say what the list says")
    return make


def _timer_left(i):
    prompts = ["is the {lab} timer almost up", "how's the {lab} timer looking", "where is the {lab} timer at"]

    def make(rng):
        lab = TIMER_LABELS[(i * 3) % len(TIMER_LABELS)]
        other = TIMER_LABELS[(i * 3 + 1) % len(TIMER_LABELS)]
        left = rng.choice([75, 210, 495, 760, 1180])
        timers = [{"kind": "timer", "label": lab, "left": left, "id": f"{rng.randrange(16**8):08x}"}]
        if i % 2:
            timers.append({"kind": "timer", "label": other, "left": left + 900, "id": f"{rng.randrange(16**8):08x}"})
        now, w, dev = dev_world(rng, timers=timers)
        final = pick(rng, [f"The {lab} timer has {_human(left)} left.", f"{_human(left)} left on {lab}.",
                           f"About {_human(left)} to go on the {lab} timer."])
        return mk("device_r3", A.style(rng, prompts[i % 3].format(lab=lab)), now, w,
                  [[Call("manage_timers", action="list")], Final(final)], "time left: manage_timers list, read the named row")
    return make


def _timer_cancel(i):
    prompts = ["stop the {lab} timer", "scrap my {lab} timer", "kill {lab}, i'm done with that one", "turn the {lab} timer off"]

    def make(rng):
        lab = TIMER_LABELS[(i * 2) % len(TIMER_LABELS)]
        timers = [{"kind": "timer", "label": lab, "left": rng.choice([300, 600, 1200]), "id": f"{rng.randrange(16**8):08x}"}]
        if i % 2:
            timers.append({"kind": "timer", "label": TIMER_LABELS[(i * 2 + 1) % len(TIMER_LABELS)], "left": 1800,
                           "id": f"{rng.randrange(16**8):08x}"})
        now, w, dev = dev_world(rng, timers=timers)
        final = pick(rng, [f"Cancelled the {lab} timer.", f"The {lab} timer is cancelled.", f"Done, I cancelled the {lab} timer."])
        return mk("device_r3", A.style(rng, prompts[i % 4].format(lab=lab)), now, w,
                  [[Call("manage_timers", action="cancel", label=lab)], Final(final)],
                  "cancel one named timer: manage_timers cancel + label; the answer says cancelled",
                  must_see=["Cancelled the timer"])
    return make


def _timer_cancel_unnamed(i):
    prompts = ["cancel my timer", "stop the timer", "get rid of the timer i set"]

    def make(rng):
        lab = TIMER_LABELS[(i * 4 + 2) % len(TIMER_LABELS)]
        timers = [{"kind": "timer", "label": lab, "left": rng.choice([420, 900]), "id": f"{rng.randrange(16**8):08x}"}]
        now, w, dev = dev_world(rng, timers=timers)
        final = pick(rng, [f"Cancelled the {lab} timer, the only one that was running.", f"Done, the {lab} timer is cancelled."])
        return mk("device_r3", A.style(rng, prompts[i % 3]), now, w,
                  [[Call("manage_timers", action="list")], [Call("manage_timers", action="cancel", label=lab)], Final(final)],
                  "cancel with no name: list, then cancel the one timer by its label", must_see=["Cancelled the timer"])
    return make


def _timer_cancel_which(i):
    prompts = ["cancel the timer", "turn off my timer please"]

    def make(rng):
        a, b = rng.sample(TIMER_LABELS, 2)
        timers = [{"kind": "timer", "label": a, "left": 360, "id": f"{rng.randrange(16**8):08x}"},
                  {"kind": "timer", "label": b, "left": 1080, "id": f"{rng.randrange(16**8):08x}"}]
        now, w, dev = dev_world(rng, timers=timers)
        final = pick(rng, [f"You have two running, {a} and {b}. Which should I cancel?", f"Which one: {a} or {b}?"])
        return mk("device_r3", A.style(rng, prompts[i % 2]), now, w, [[Call("manage_timers", action="list")], Final(final)],
                  "two timers and no name: list, then one short question; nothing cancelled")
    return make


def _timer_cancel_all(i):
    prompts = ["clear every timer i have running", "cancel all of my timers", "stop all timers and alarms"]

    def make(rng):
        labs = rng.sample(TIMER_LABELS, rng.choice([2, 3]))
        timers = [{"kind": "timer", "label": lb, "left": rng.choice([200, 600, 1500]), "id": f"{rng.randrange(16**8):08x}"} for lb in labs]
        now, w, dev = dev_world(rng, timers=timers)
        n = len(labs)
        final = pick(rng, [f"Cancelled all {n} timers.", f"All {n} timers are cancelled.", f"Done, {n} timers cancelled."]) if n > 2 \
            else pick(rng, ["Cancelled both timers.", "Both timers are cancelled.", "Done, both timers are cancelled."])
        return mk("device_r3", A.style(rng, prompts[i % 3]), now, w, [[Call("manage_timers", action="cancel_all")], Final(final)],
                  "cancel every timer: manage_timers cancel_all (never a reminder tool)", must_see=[f"Cancelled {n} timers"])
    return make


def _stopwatch(kind, i):
    def make(rng):
        if kind == "start":
            now, w, dev = dev_world(rng)
            prompt = A.style(rng, ["time me with the stopwatch", "begin a stopwatch", "stopwatch, go"][i % 3])
            steps = [[Call("manage_timers", action="stopwatch_start")],
                     Final(pick(rng, ["The stopwatch is running.", "Stopwatch started.", "Timing has started."]))]
        else:
            el = rng.choice([95, 242, 608, 1010])
            now, w, dev = dev_world(rng, stopwatch=el)
            if kind == "read":
                prompt = A.style(rng, ["what does the stopwatch say right now", "check the stopwatch"][i % 2])
                steps = [[Call("manage_timers", action="stopwatch_read")],
                         Final(pick(rng, [f"The stopwatch is at {_human(el)}.", f"{_human(el)} so far."]))]
            else:
                prompt = A.style(rng, ["stop the stopwatch", "end the stopwatch, what was the time"][i % 2])
                steps = [[Call("manage_timers", action="stopwatch_stop")],
                         Final(pick(rng, [f"Stopped at {_human(el)}.", f"The stopwatch stopped at {_human(el)}."]))]
        return mk("device_r3", prompt, now, w, steps, f"stopwatch {kind}: manage_timers stopwatch_{kind}")
    return make


# ---------------------------------------------------------------- status
def _status(kind, i):
    def make(rng):
        macos = not (kind == "macos" and i % 2 == 1)
        now, w, dev = dev_world(rng, charging=(kind == "battery" and i % 3 == 2), macos=macos)
        free, total, used = dev.free
        if kind == "battery":
            prompt = A.style(rng, ["how much battery do i have left", "what's my battery at", "how charged is the laptop"][i % 3])
            if dev.charging:
                final = pick(rng, [f"Your battery is at {dev.battery}% and charging.", f"It's charging, currently at {dev.battery}%."])
            else:
                h, m = divmod(dev.remaining, 60)
                final = pick(rng, [f"Your battery is at {dev.battery}%, with about {h} hours {m} minutes left.",
                                   f"It's at {dev.battery}% and should last roughly {h} hours {m} minutes.",
                                   f"{dev.battery}% charge, and about {h}h {m}m of use left.",
                                   f"You have {dev.battery}% left, which is around {h} hours {m} minutes."])
            step = Call("system_status", section="battery")
        elif kind == "health":
            prompt = A.style(rng, ["how worn out is my battery", "is my battery still in good shape"][i % 2])
            final = pick(rng, [f"Battery health is {dev.health}% of its original capacity, after {dev.cycles} charge cycles.",
                               f"It holds {dev.health}% of its original capacity and has {dev.cycles} charge cycles, which macOS rates Normal."])
            step = Call("system_status", section="battery")
        elif kind == "disk":
            prompt = A.style(rng, ["how much storage is free on this mac", "am i running low on disk space",
                                   "what's left on my drive"][i % 3])
            final = pick(rng, [f"{free} free of {total}, so the disk is {used}% used.", f"You have {free} free out of {total}.",
                               f"There's {free} left on a {total} disk ({used}% used)."])
            step = Call("system_status")
        elif kind == "memory":
            prompt = A.style(rng, ["is my mac short on memory", "how much memory is free"][i % 2])
            final = pick(rng, [f"{dev.mem_free}% of memory is free.", f"About {dev.mem_free}% of your memory is free right now."])
            step = Call("system_status")
        elif kind == "macos":
            prompt = A.style(rng, ["which macos is this machine running", "what version of macos do i have"][i % 2])
            if macos:
                final = pick(rng, [f"macOS {dev.version[0]}, build {dev.version[1]}.", f"You're on macOS {dev.version[0]}."])
            else:
                final = pick(rng, ["The status report doesn't show the macOS version, so I can't say.",
                                   "I can't tell, since the system report doesn't list the version."])
            step = Call("system_status")
        elif kind == "uptime":
            prompt = A.style(rng, ["uptime on this machine?", "when did i last restart"][i % 2])
            final = pick(rng, [f"It's been up for {uptime_say(dev.uptime)}.", f"Your Mac has been running for {uptime_say(dev.uptime)}."])
            step = Call("system_status")
        elif kind == "wifi":
            prompt = A.style(rng, ["which network am i connected to", "what's the name of the wifi i'm on"][i % 2])
            final = pick(rng, [f"You're on {dev.ssid}.", f"Connected to {dev.ssid}."])
            step = Call("system_status", section="network")
        elif kind == "bt":
            prompt = A.style(rng, ["what's connected over bluetooth", "are my airpods connected"][i % 2])
            final = pick(rng, ["Bluetooth is on, with AirPods Pro connected.", "Yes, AirPods Pro are connected."])
            step = Call("system_status", section="bluetooth")
        else:
            prompt = A.style(rng, ["how busy is my cpu"][0])
            final = pick(rng, [f"CPU load is {dev.load[0]:.2f} over the last minute across {dev.cores} cores."])
            step = Call("system_status")
        return mk("device_r3", prompt, now, w, [[step], Final(final)], f"status {kind}: system_status, answered from its text only")
    return make


def _calc(i):
    cases = [("work out 18% of 240", "18% of 240", "43.2", "18% of 240 is 43.2."),
             ("divide 375 by 15", "375/15", "25", "375 divided by 15 is 25."),
             ("if four of us split a 138 dollar dinner, what's each person's share", "138/4", "34.5", "Each person pays $34.50."),
             ("multiply 64 by 7", "64*7", "448", "64 times 7 is 448."),
             ("what is 15% tip on 82", "15% of 82", "12.3", "A 15% tip on $82 is $12.30."),
             ("square root of 169", "sqrt(169)", "13", "The square root of 169 is 13.")]
    q, expr, res, final = cases[i % len(cases)]

    def make(rng):
        now, w, dev = dev_world(rng)
        return mk("device_r3", A.style(rng, q), now, w, [[Call("calculate", expression=expr)], Final(final)],
                  "arithmetic goes through calculate, never from memory", must_see=[res])
    return make


def _convert(i):
    cases = [("how many pounds is 70 kg", 70, "kg", "lb", "70 kg is about 154.3 pounds."),
             ("convert 30 centimeters to inches", 30, "cm", "in", "30 cm is about 11.81 inches."),
             ("what's 6 feet in meters", 6, "ft", "m", "6 feet is 1.829 meters."),
             ("100 fahrenheit in celsius", 100, "F", "C", "100°F is about 37.8°C."),
             ("how many miles is 10 km", 10, "km", "miles", "10 km is about 6.21 miles.")]
    q, val, fu, tu, final = cases[i % len(cases)]

    def make(rng):
        now, w, dev = dev_world(rng)
        return mk("device_r3", A.style(rng, q), now, w, [[Call("convert_units", value=val, from_unit=fu, to_unit=tu)], Final(final)],
                  "unit conversion goes through convert_units")
    return make


def _time_place(i):
    places = ["Tokyo", "London", "Paris", "Sydney", "Berlin"]

    def make(rng):
        p = places[i % len(places)]
        now, w, dev = dev_world(rng)
        res = w.t_world_time(place=p)
        m = re.match(r"In (\S+(?: \S+)?) it's (.+?) \((\S+)h ahead of you\)\.", res)
        final = f"It's {m.group(2)} in {p}, {m.group(3)} hours ahead of you."
        prompt = A.style(rng, ["what's the time over in {p}", "current time in {p}?", "tell me what time it is in {p} right now"][i % 3].format(p=p))
        return mk("device_r3", prompt, now, w, [[Call("world_time", place=p)], Final(final)],
                  "time elsewhere: world_time with place", must_see=[m.group(2)])
    return make


# =============================================================================
def fam_device_r3(rng):
    _SEEN.clear()
    _OPEN.clear()
    out: list = []
    fo = lambda mk_list: _fill_ordered(out, mk_list, rng, "device_r3")
    # volume (the write turns end at the call; see _vend), brightness
    fo([_vol_set(i) for i in range(2)] + [_vol_words(1), _vol_rel(1, 0)] + [_vol_rel(-1, i) for i in range(2)]
       + [_vol_mute(True, 0), _vol_mute(False, 0)] + [_vol_read(0)])
    fo([_no_brightness(i) for i in range(2)])
    # Focus + single-setting toggles
    fo([_toggle("do_not_disturb", True, i) for i in range(2)] + [_toggle("do_not_disturb", False, 0)] + [_focus_read(i) for i in range(2)])
    fo([_toggle("wifi", False, 2), _toggle("wifi", True, 1), _toggle("bluetooth", False, 1), _toggle("bluetooth", True, 0),
        _toggle("low_power_mode", True, 0), _toggle("airdrop", False, 0), _toggle("dark_mode", True, 0),
        _toggle("dark_mode", False, 1), _toggle("voiceover", True, 0)])
    fo([_toggle_fail("do_not_disturb", True,
                     "(Do Not Disturb needs a Shortcut to toggle it — macOS exposes no direct API since Monterey. Create a "
                     "Shortcut named 'Wisp DND On' with the 'Set Focus' action, then ask again.)",
                     "Do Not Disturb didn't change. It needs a Shortcut named “Wisp DND On” that uses the Set Focus action, and then I can switch it.",
                     "I couldn't turn Do Not Disturb on: macOS only lets me do it through a Shortcut named “Wisp DND On” with a Set Focus action.",
                     "mute my notifications with dnd"),
        _toggle_fail("low_power_mode", True, "(Low Power Mode needs an admin password, so it can't be set from here.)",
                     "I couldn't turn on Low Power Mode, since it needs an admin password. You can switch it in System Settings under Battery.",
                     "Low Power Mode stays off: it needs an admin password, so it can't be set from here. System Settings has it under Battery.",
                     "put the mac in low power mode")])
    fo([_vague_off(i) for i in range(4)])
    # timers
    fo([_timer_set(i) for i in range(6)] + [_alarm_set(i) for i in range(2)] + [_sleep_set(0)])
    fo([_timers_running(i) for i in range(2)] + [_timer_left(i) for i in range(2)])
    fo([_timer_cancel(i) for i in range(2)] + [_timer_cancel_unnamed(0), _timer_cancel_which(0), _timer_cancel_all(0), _timer_cancel_all(1)])
    fo([_stopwatch("start", 0), _stopwatch("read", 0), _stopwatch("stop", 0)])
    # status + compute
    fo([_status("battery", 0), _status("battery", 1), _status("battery", 2), _status("health", 0), _status("disk", 0),
        _status("disk", 1), _status("memory", 0), _status("macos", 0), _status("macos", 1), _status("uptime", 0),
        _status("wifi", 0)])
    fo([_calc(i) for i in range(3)] + [_convert(i) for i in range(2)] + [_time_place(0)])
    return out


# =============================================================================
# CONTACTS
# =============================================================================
FIRSTS = ["Noor", "Elena", "Tariq", "Leila", "Owen", "Ines", "Kofi", "Nadia", "Silas", "Rhea", "Callum", "Mei",
          "Dario", "Hazel", "Wren", "Anil", "Farah", "Jonas", "Lucia", "Emeka", "Sana", "Teodor"]
LASTS = ["Okoye", "Lindqvist", "Brandt", "Haddad", "Moreno", "Tran", "Novak", "Abara", "Castillo", "Sorensen",
         "Varga", "Pillai", "Duarte", "Ellison", "Kasprzak", "Nwosu", "Hartley", "Quint", "Salazar", "Bergstrom"]
SAME_FIRST = [("Jordan", "Ellis", "Pierce"), ("Maya", "Chen", "Brandt"), ("Ben", "Ortiz", "Wallace"),
              ("Dana", "Whitfield", "Reyes"), ("Theo", "Park", "Lindqvist"), ("Aisha", "Karim", "Mbeki"),
              ("Chris", "Okoye", "Lund"), ("Noor", "Haddad", "Qureshi")]
BIRTHDAYS = [(4, 22), (8, 5), (10, 17), (12, 8), (5, 30), (9, 19), (1, 27), (2, 11), (6, 25), (11, 14), (7, 9), (3, 3)]


def phone(rng):
    return f"({rng.choice([503, 971, 541])}) 555-01{rng.randint(10, 99)}"


def person(rng, used):
    while True:
        f, l = rng.choice(FIRSTS), rng.choice(LASTS)
        if f not in used:
            used.add(f)
            return f, l


def email_of(f, l, rng):
    return rng.choice([f"{f.lower()}.{l.lower()}@example.com", f"{f.lower()}{l[0].lower()}@linden.edu",
                       f"{f[0].lower()}{l.lower()}@example.com"])


def con_world(rng, **kw):
    now, w = A.base_world(rng, **kw)
    return now, w


def install_birthdays(w, table):
    """contact_dates (contacts_tools.contact_dates) over `table`: {name: (month, day)}."""
    keyed: dict[str, list[str]] = {}
    for name, (m, d) in table.items():
        keyed.setdefault(f"{m:02d}-{d:02d}", []).append(name)

    def handler(args):
        try:
            window = max(1, min(366, int(args.get("days", 30))))
        except (TypeError, ValueError):
            window = 30
        if not keyed:
            return ("No birthdays found — either none are saved in Contacts, or "
                    "Contacts hasn't synced yet on this Mac.")
        today = w.now.date()
        up = []
        for key, names in keyed.items():
            m, d = int(key[:2]), int(key[3:])
            this_year = today.replace(month=m, day=d)
            target = this_year if this_year >= today else this_year.replace(year=today.year + 1)
            delta = (target - today).days
            if delta <= window:
                up.append((delta, target, names))
        if not up:
            return f"No birthdays in the next {window} days."
        up.sort(key=lambda t: t[0])
        lines = []
        for delta, when, names in up:
            when_str = ("today" if delta == 0 else "tomorrow" if delta == 1 else when.strftime("%A, %B %-d"))
            lines.append(f"  {', '.join(names)} — {when_str}")
        return f"Upcoming birthdays (next {window} days):\n" + "\n".join(lines)
    w.overrides["contact_dates"] = handler
    return handler


def when_of_bday(w, m, d):
    today = w.now.date()
    t = today.replace(month=m, day=d)
    target = t if t >= today else t.replace(year=today.year + 1)
    delta = (target - today).days
    return ("today" if delta == 0 else "tomorrow" if delta == 1 else target.strftime("%A, %B %-d")), delta


# ---------------------------------------------------------------- lookups
def _c_phone(i):
    tmpls = ["what number do i have for {n}", "give me {n}'s phone number", "{n}'s number?", "what's {n}'s mobile",
             "i need a number for {n}", "look up {n}'s cell"]

    def make(rng):
        now, w = con_world(rng)
        f, l = person(rng, set())
        ph = phone(rng)
        w.contacts[f"{f} {l}"] = (ph, "")
        prompt = A.style(rng, tmpls[i % len(tmpls)].format(n=f.lower() if i % 2 else f))
        final = pick(rng, [f"{f} {l}'s number is {ph}.", f"{ph} is the number for {f} {l}.", f"You have {ph} for {f} {l}."])
        return mk("contacts_r3", prompt, now, w, [[Call("lookup_contact", name=f)], Final(final)],
                  "phone lookup: lookup_contact by the name as typed; the exact digits in the answer", must_see=[ph])
    return make


def _c_email(i):
    tmpls = ["which email address is saved for {n}", "{n}'s email?", "what email do i have on file for {n}",
             "pull up {n}'s email", "i need {n}'s address for a form, the email one"]

    def make(rng):
        now, w = con_world(rng)
        f, l = person(rng, set())
        em = email_of(f, l, rng)
        w.contacts[f"{f} {l}"] = ("", em) if i % 2 else (phone(rng), em)
        prompt = A.style(rng, tmpls[i % len(tmpls)].format(n=f))
        final = pick(rng, [f"{f}'s email is {em}.", f"{em} is what you have for {f} {l}.", f"You have {em} saved for {f}."])
        return mk("contacts_r3", prompt, now, w, [[Call("lookup_contact", name=f)], Final(final)],
                  "email lookup: lookup_contact; the exact address in the answer", must_see=[em])
    return make


def _c_both(i):
    tmpls = ["everything i have for {n} in contacts", "show me {n}'s contact details", "how can i reach {n}"]

    def make(rng):
        now, w = con_world(rng)
        f, l = person(rng, set())
        ph, em = phone(rng), email_of(f, l, rng)
        w.contacts[f"{f} {l}"] = (ph, em)
        prompt = A.style(rng, tmpls[i % len(tmpls)].format(n=f))
        final = pick(rng, [f"{f} {l}: {ph}, and {em} by email.", f"For {f} {l} you have {ph} and {em}."])
        return mk("contacts_r3", prompt, now, w, [[Call("lookup_contact", name=f)], Final(final)],
                  "both handles: say both exactly", must_see=[ph, em])
    return make


def _c_no_email(i):
    tmpls = ["does mom have an email address saved", "is mom's email on file anywhere", "do i have an email for mom"]

    def make(rng):
        now, w = con_world(rng)
        prompt = A.style(rng, tmpls[i % len(tmpls)])
        ph = w.contacts["Mom"][0]
        final = pick(rng, [f"Mom only has a phone number saved, {ph}. There's no email address.",
                           f"No email for Mom, just the number {ph}."])
        return mk("contacts_r3", prompt, now, w, [[Call("lookup_contact", name="Mom")], Final(final)],
                  "a phone-only contact: say there is no email; never offer the phone as an email", must_see=[ph])
    return make


def _c_missing(i):
    names = ["Bob", "Greta", "Pavel", "Imani", "Rosa"]

    def make(rng):
        now, w = con_world(rng)
        n = names[i % len(names)]
        prompt = A.style(rng, ["do i have a number for {n}", "is {n} saved in my phone", "what's {n}'s number"][i % 3].format(n=n.lower()))
        final = pick(rng, [f"I don't have a contact named {n}.", f"There's no {n} in your contacts.",
                           f"No {n} is saved in your contacts."])
        return mk("contacts_r3", prompt, now, w, [[Call("lookup_contact", name=n)], Final(final)],
                  "no match: say so plainly, never guess a number")
    return make


def _c_count(i):
    prompts = ["how many people are in my address book", "list the people saved in my contacts", "who do i have saved as contacts"]

    def make(rng):
        now, w = con_world(rng)
        names = sorted(w.contacts)
        if i % 3 == 0:
            res_count = len(names)
            final = pick(rng, [f"You have {res_count} contacts saved: {join(names)}.", f"There are {res_count} people saved: {join(names)}."])
        else:
            final = f"You have {len(names)} contacts saved: {join(names)}."
        steps = [[Call("list_contacts")], Final(final)]
        w.overrides["list_contacts"] = lambda a, names=names: f"{len(names)} contact(s):\n" + "\n".join(f"- {n}" for n in names)
        return mk("contacts_r3", A.style(rng, prompts[i % 3]), now, w, steps, "list_contacts: the count and names it returns")
    return make


# ---------------------------------------------------------------- birthdays
def _c_bday(i):
    tmpls = ["what day is {n}'s birthday", "when should i expect {n}'s birthday", "do you know {n}'s birthday",
             "{n}'s birthday, what's the date"]

    def make(rng):
        now, w = con_world(rng)
        pool = ["Mom", "Maya Chen", "Theo Park", "Carmen Rivera"]
        who = pool[i % len(pool)]
        others = rng.sample([n for n in pool if n != who], 2)
        picks = rng.sample(BIRTHDAYS, 3)
        table = {who: picks[0], others[0]: picks[1], others[1]: picks[2]}
        install_birthdays(w, table)
        word, delta = when_of_bday(w, *picks[0])
        first = who.split()[0]
        poss = f"{first}'s"
        prompt = A.style(rng, tmpls[i % len(tmpls)].format(n=first.lower()))
        if word in ("today", "tomorrow"):
            final = f"{poss} birthday is {word}."
        else:
            final = pick(rng, [f"{poss} birthday is {word}.", f"It's {word} for {first}.", f"You have {first}'s birthday down for {word}."])
        return mk("contacts_r3", prompt, now, w, [[Call("contact_dates", days=366)], Final(final)],
                  "a named birthday: contact_dates over a full year, read that person's date exactly", must_see=[who])
    return make


def _c_bday_none(i):
    def make(rng):
        now, w = con_world(rng)
        pool = ["Mom", "Maya Chen", "Theo Park", "Carmen Rivera"]
        who = pool[(i + 1) % len(pool)]
        others = [n for n in pool if n != who]
        table = {others[0]: BIRTHDAYS[(i * 2) % 12], others[1]: BIRTHDAYS[(i * 2 + 5) % 12]}
        install_birthdays(w, table)
        first = who.split()[0]
        prompt = A.style(rng, ["is {n}'s birthday saved anywhere", "what's {n}'s birthday"][i % 2].format(n=first.lower()))
        final = pick(rng, [f"I don't see a birthday saved for {first} in your contacts.", f"{first} has no birthday on file in Contacts."])
        return mk("contacts_r3", prompt, now, w, [[Call("contact_dates", days=366)], Final(final)],
                  "birthday not in the list: say it is not saved, no guess")
    return make


def _c_bday_soon(i):
    def make(rng):
        now, w = con_world(rng)
        names = ["Mom", "Maya Chen", "Theo Park", "Carmen Rivera"]
        # birthdays at 3, 12 and 80 days out, so the 30-day window shows two
        base = now.date()
        offs = rng.sample([3, 9, 12, 21, 80, 140], 3)
        table = {n: ((base + D(days=o)).month, (base + D(days=o)).day) for n, o in zip(rng.sample(names, 3), offs)}
        install_birthdays(w, table)
        prompt = A.style(rng, ["who is celebrating soon", "is anyone having a birthday in the next month"][i % 2])
        res = w.overrides["contact_dates"]({"days": 30})
        rows = [l.strip() for l in res.splitlines()[1:]]
        if not rows:
            final = "No birthdays in the next 30 days."
        else:
            parts = [f"{r.split(' — ')[0]} ({r.split(' — ')[1]})" for r in rows]
            final = pick(rng, [f"Coming up: {join(parts)}.", f"In the next 30 days: {join(parts)}."])
        return mk("contacts_r3", prompt, now, w, [[Call("contact_dates", days=30)], Final(final)],
                  "birthdays soon: contact_dates with the window; list exactly what returns", must_see=["Upcoming birthdays"])
    return make


# ---------------------------------------------------------------- two people, one first name
def _c_same_first(i, mode):
    first, l1, l2 = SAME_FIRST[i % len(SAME_FIRST)]

    def make(rng):
        now, w = con_world(rng)
        w.contacts[f"{first} {l1}"] = (phone(rng), email_of(first, l1, rng))
        w.contacts[f"{first} {l2}"] = (phone(rng), email_of(first, l2, rng))
        q = [f"{first} {l1}", f"{first} {l2}"]
        question = pick(rng, [f"Which {first}: {q[0]} or {q[1]}?", f"You have two: {q[0]} and {q[1]}. Which one?",
                              f"Do you mean {q[0]} or {q[1]}?"])
        if mode == "phone":
            prompt = A.style(rng, ["what's {n}'s number", "i need {n}'s phone number", "get me {n}'s cell"][i % 3].format(n=first.lower()))
            return mk("contacts_r3", prompt, now, w, [[Call("lookup_contact", name=first)], Final(question)],
                      "two contacts share the first name: one short question naming both, no guess")
        if mode == "email":
            prompt = A.style(rng, ["email address for {n}?", "what's {n}'s email"][i % 2].format(n=first.lower()))
            return mk("contacts_r3", prompt, now, w, [[Call("lookup_contact", name=first)], Final(question)],
                      "two contacts share the first name: one short question naming both, no guess")
        # a write: lookup first, then the question; nothing is saved
        val = phone(rng)
        prompt = A.style(rng, ["save {v} as {n}'s new number", "{n} got a new phone, it's {v}"][i % 2].format(n=first.lower(), v=val))
        return mk("contacts_r3", prompt, now, w, [[Call("lookup_contact", name=first)], Final(question)],
                  "a contact write with an ambiguous first name: look it up, ask which one, save nothing")
    return make


# ---------------------------------------------------------------- creating / updating
def _c_add_existing(kind, i):
    tmpls_p = ["{n}'s number is {v}, save it", "put {v} on {n}'s contact", "here's {n}'s cell: {v}"]
    tmpls_e = ["{n}'s email is {v}, keep that on file", "add {v} to {n}'s card", "save {v} as {n}'s email"]

    def make(rng):
        now, w = con_world(rng)
        f, l = person(rng, set())
        full = f"{f} {l}"
        v = phone(rng) if kind == "phone" else email_of(f, l, rng)
        # a saved contact always has one handle already: an email when a phone is added, a phone when an email is
        w.contacts[full] = ("", email_of(f, l, rng)) if kind == "phone" else (phone(rng), "")
        tmpl = (tmpls_p if kind == "phone" else tmpls_e)[i % 3]
        prompt = A.style(rng, tmpl.format(n=f, v=v))
        action = "add_phone" if kind == "phone" else "add_email"
        what = "number" if kind == "phone" else "email"
        final = pick(rng, [f"Saved {v} to {full}'s contact.", f"{full} now has {v} saved.", f"Added {v} to {full}'s contact."])
        return mk("contacts_r3", prompt, now, w,
                  [[Call("lookup_contact", name=f)], [Call("manage_contacts", action=action, name=full, value=v)], Final(final)],
                  f"update a contact's {what}: look the person up, then {action} with the full saved name",
                  must_see=[full])
    return make


def _c_add_known(kind, i):
    """Mom / a default-world contact the user names: look up, then add."""
    def make(rng):
        now, w = con_world(rng)
        who = ["Mom", "Maya Chen", "Theo Park", "Carmen Rivera"][i % 4]
        first = who.split()[0]
        lo = first.lower()
        if kind == "email":
            v = f"{lo}.{rng.choice(['home', 'new', 'family', 'main'])}{rng.randint(11, 98)}@example.com"
            prompt = A.style(rng, [f"{first}'s email changed to {v}, update the contact", f"{lo}'s new email is {v}",
                                   f"add {v} to {lo}'s contact"][i % 3])
            action = "add_email"
        else:
            v = phone(rng)
            prompt = A.style(rng, [f"add {v} to {lo}'s contact", f"{lo} has a new number: {v}", f"save {v} for {lo}"][i % 3])
            action = "add_phone"
        old = w.contacts[who][0] if kind == "phone" else w.contacts[who][1]
        final = pick(rng, [f"Added {v} to {who}'s contact.", f"{who} has {v} saved now."])
        if old and kind == "phone":
            final = pick(rng, [f"Added {v} to {who}'s contact. The old number is still there too.",
                               f"Saved {v} for {who}; the earlier number stays on the card."])
        return mk("contacts_r3", prompt, now, w,
                  [[Call("lookup_contact", name=first)], [Call("manage_contacts", action=action, name=who, value=v)], Final(final)],
                  f"{action} on a known contact; a number is added, the old one stays", must_see=[who])
    return make


def _c_create_new(i, with_number=True):
    tmpls = ["add {n} to my contacts, {v}", "new contact: {n}, {v}", "make a contact for {n} with the number {v}",
             "save a new person, {n} {v}"]

    def make(rng):
        now, w = con_world(rng)
        f, l = person(rng, set())
        full = f"{f} {l}"
        v = phone(rng)
        prompt = A.style(rng, tmpls[i % len(tmpls)].format(n=full, v=v))
        final = pick(rng, [f"Created {full} with {v}.", f"{full} is in your contacts now, with {v}.", f"Saved {full}: {v}."])
        return mk("contacts_r3", prompt, now, w,
                  [[Call("lookup_contact", name=full)], [Call("manage_contacts", action="create", name=full)],
                   [Call("manage_contacts", action="add_phone", name=full, value=v)], Final(final)],
                  "a new person: check, create, then add the number (create first, add second)", must_see=[full])
    return make


def _c_save_unknown(i):
    tmpls = ["save {n}'s number as {v}", "can you store {v} for {n}", "{n}'s number is {v}, remember it in contacts"]

    def make(rng):
        now, w = con_world(rng)
        f = rng.choice(["Bob", "Greta", "Pavel", "Imani", "Rosa", "Henrik", "Yuki"])
        v = phone(rng)
        prompt = A.style(rng, tmpls[i % 3].format(n=f, v=v))
        final = pick(rng, [f"Saved {f} with {v}.", f"{f} is in your contacts now: {v}.", f"Created {f} and saved {v}."])
        return mk("contacts_r3", prompt, now, w,
                  [[Call("lookup_contact", name=f)], [Call("manage_contacts", action="create", name=f)],
                   [Call("manage_contacts", action="add_phone", name=f, value=v)], Final(final)],
                  "a name that is not saved: lookup finds nothing, so create then add_phone", must_see=[f])
    return make


def _c_missing_value(i):
    prompts = ["add an email for {n}", "update {n}'s phone number", "save a number for {n}"]

    def make(rng):
        now, w = con_world(rng)
        who = ["Maya", "Theo", "Carmen", "Mom"][i % 4]
        what = "email" if i % 3 == 0 else "number"
        prompt = A.style(rng, prompts[i % 3].format(n=who.lower()))
        final = pick(rng, [f"What's the {what} for {who}?", f"Which {what} should I save for {who}?", f"What {what} do you want on {who}'s card?"])
        return mk("contacts_r3", prompt, now, w, [Final(final)], "a contact write with no value given: one short question")
    return make


def fam_contacts_r3(rng):
    _SEEN.clear()
    _OPEN.clear()
    out: list = []
    fo = lambda mk_list: _fill_ordered(out, mk_list, rng, "contacts_r3")
    fo([_c_phone(i) for i in range(6)])
    fo([_c_email(i) for i in range(5)])
    fo([_c_both(i) for i in range(2)])
    fo([_c_no_email(i) for i in range(2)])
    fo([_c_missing(i) for i in range(3)])
    fo([_c_count(i) for i in range(2)])
    fo([_c_bday(i) for i in range(5)] + [_c_bday_none(i) for i in range(2)] + [_c_bday_soon(i) for i in range(2)])
    fo([_c_same_first(i, "phone") for i in range(3)] + [_c_same_first(i + 3, "email") for i in range(2)]
       + [_c_same_first(i + 5, "write") for i in range(2)])
    fo([_c_add_existing("phone", i) for i in range(3)] + [_c_add_existing("email", i) for i in range(3)])
    fo([_c_add_known("phone", i) for i in range(3)] + [_c_add_known("email", i) for i in range(3)])
    fo([_c_create_new(i) for i in range(3)] + [_c_save_unknown(i) for i in range(3)])
    fo([_c_missing_value(i) for i in range(3)])
    return out




# =============================================================================
# REMINDERS
# =============================================================================
R_TASKS = [
    ("call the landlord about the heater", "landlord"), ("buy a train pass", "train pass"),
    ("email Hana the lab data", "lab data"), ("upload the scholarship essay", "scholarship essay"),
    ("pick up the dry cleaning", "dry cleaning"), ("sign up for a flu shot", "flu shot"),
    ("return the camera lens", "camera lens"), ("renew my library card", "library card"),
    ("bring the poster to class", "poster"), ("take out the compost", "compost"),
    ("message Ben about the long run", "long run"), ("refill the water filter", "water filter"),
    ("book a campsite for the trip", "campsite"), ("send Carmen the concert link", "concert link"),
    ("print the group project outline", "project outline"), ("transfer money for rent", "money for rent"),
    ("drop the jacket off at the tailor", "tailor"), ("order the textbook for CS 210", "textbook"),
    ("follow up with Fernbrook Labs", "fernbrook"), ("review my flashcards", "flashcards"),
    ("clean my climbing shoes", "climbing shoes"), ("buy a gift for Theo", "gift for theo"),
    ("call Aunt Priya", "aunt priya"), ("download the syllabus", "syllabus"),
    ("check my financial aid portal", "financial aid"), ("wash the bedsheets", "bedsheets"),
    ("pay the gym membership", "gym"), ("mail the rent check", "rent check"),
    ("pick up the package from the front desk", "package"), ("rehearse my interview answers", "interview answers"),
    ("email Prof. Okafor a thank-you", "thank-you"), ("back up the OS project", "os project"),
    ("buy a bus ticket home", "bus ticket"), ("check the oven timer settings", "oven settings"),
    ("get passport photos taken", "passport photos"), ("start the ethics essay outline", "ethics essay"),
]
HANDLE = {t.lower(): h for t, h in R_TASKS}
TASKS = [t for t, _ in R_TASKS]
R_NOUNS = ["the invoice from the print shop", "the parking ticket", "the roommate meeting", "my dentist follow-up",
           "the lab report draft"]
R_TMPL = ["tap me on the shoulder {w} to {task}", "i need to {task} {w}, remind me", "heads up {w}: {task}",
          "can you make sure i {task} {w}", "give me a nudge {w} so i {task}", "reminder please, {task}, {w}",
          "{w}: nudge me to {task}", "{w} is when i should {task}, set that up"]
R_NOUN_TMPL = ["nudge me {w} about {noun}", "remind me about {noun} {w}"]


def rline(r, now):
    d = r.due
    delta = (d.date() - now.date()).days
    day = ("today" if delta == 0 else "tomorrow" if delta == 1 else "yesterday" if delta == -1
           else d.strftime("%a") if 0 < delta < 7 else d.strftime("%a %b %-d"))
    if d < now:
        return f"{r.title} (was due {day} at {clock(d)})"
    return f"{r.title} ({day} at {clock(d)})"


def _r_add(kind, i):
    def make(rng):
        hrs = A._HOURS.get(kind)
        now, w = A.base_world(rng, **({"hour": rng.choice(hrs)} if hrs else {}))
        spec = A._spec(rng, now, kind)
        if spec is None:
            return None
        phrase, d, k = spec
        if d <= now + D(minutes=5):
            return None
        use_noun = (i % 5 == 4)
        if use_noun:
            noun = R_NOUNS[(i // 5) % len(R_NOUNS)]
            prompt = R_NOUN_TMPL[(i // 5) % 2].format(w=phrase, noun=noun)
            title = cap1(noun)
            task = noun
        else:
            task = TASKS[(i * 5 + len(kind)) % len(TASKS)]
            prompt = R_TMPL[i % len(R_TMPL)].format(w=phrase, task=task)
            title = cap1(task)
        prompt = A.style(rng, prompt)
        when = A._when_say(d, now, k)
        wb = A._when_say(d, now, k, to=True)
        t2 = A.second(task)
        if use_noun:
            final = pick(rng, [f"I'll remind you {when} about {A.second(task)}.", f"Set for {wb}: {A.second(task)}.",
                               f"You'll get a nudge {when} about {A.second(task)}."])
        else:
            final = pick(rng, [f"I'll nudge you {when} to {t2}.", f"Reminder set for {wb}: {t2}.", f"Okay, you'll hear from me {when} to {t2}.",
                               f"You'll get a reminder {when} to {t2}.", f"Done, a reminder to {t2} is set for {wb}."])
        return mk("reminders_r3", prompt, now, w, [[Call("add_reminder", title=title, when_iso=A.iso(d))], Final(final)],
                  f"reminder ({kind}): the user's time words resolved to an ISO time, no question, a clean title")
    return make


def _r_add_dateonly(i):
    """A day with no clock time: the product default (9 AM) is used and said."""
    def make(rng):
        now, w = A.base_world(rng, hour=rng.choice([8, 10, 12, 15, 18]))
        part = rng.choice(["tomorrow", "tmrw"])
        task = TASKS[(i * 7 + 3) % len(TASKS)]
        d = A.at(now, "tomorrow")
        prompt = A.style(rng, ["set a reminder for {w} to {task}", "{task} {w}, put that in as a reminder",
                               "i have to {task} {w}, please remind me"][i % 3].format(w=part, task=task))
        final = pick(rng, [f"Set for tomorrow at {clock(d)}: {A.second(task)}.", f"I'll remind you tomorrow at {clock(d)} to {A.second(task)}.",
                           f"Done, the reminder is for tomorrow at {clock(d)}."])
        return mk("reminders_r3", prompt, now, w, [[Call("add_reminder", title=cap1(task), when_iso=A.iso(d))], Final(final)],
                  "a day without a clock time: the default time is used and named, no question")
    return make


def _r_add_notime(i):
    def make(rng):
        now, w = A.base_world(rng)
        task = TASKS[(i * 3 + 11) % len(TASKS)]
        prompt = A.style(rng, ["put a nudge in for me to {task}", "i have to {task}, can you ping me about it"][i % 2].format(task=task))
        final = pick(rng, ["When should I remind you?", "What time would you like the reminder?", "Sure, and when should it go off?"])
        return mk("reminders_r3", prompt, now, w, [Final(final)], "no day or time at all: one short question about when")
    return make


def _rem_world(rng, *, hour=None, overdue=2, today=1, upcoming=3, undated=1, done=1):
    now, w = A.base_world(rng, **({"hour": hour} if hour is not None else {}))
    titles = rng.sample(TASKS, 12)
    ti = iter(titles)
    rems = []
    for _ in range(overdue):
        rems.append(Reminder(cap1(next(ti)), dt.datetime.combine(now.date() - D(days=rng.randint(1, 4)), dt.time(rng.choice([9, 12, 17]), 0))))
    for _ in range(today):
        h = min(23, now.hour + rng.randint(2, 5))
        rems.append(Reminder(cap1(next(ti)), now.replace(hour=h, minute=rng.choice([0, 30]), second=0, microsecond=0)))
    for _ in range(upcoming):
        rems.append(Reminder(cap1(next(ti)), dt.datetime.combine(now.date() + D(days=rng.randint(1, 6)), dt.time(rng.choice([9, 12, 17, 20]), 0))))
    for _ in range(undated):
        rems.append(Reminder(cap1(next(ti)), None))
    for _ in range(done):
        rems.append(Reminder(cap1(next(ti)), dt.datetime.combine(now.date() + D(days=rng.randint(0, 3)), dt.time(9, 0)), done=True))
    w.reminders = rems
    return now, w


def _open(w):
    return [r for r in w.reminders if not r.done]


def _bullet_or_prose(head, rows, now):
    lines = [rline(r, now) for r in rows]
    if len(lines) >= 4:
        return head + ":\n" + "\n".join(f"- {l}" for l in lines)
    return head + ": " + join(lines) + "."


def _r_pending(i):
    prompts = ["what reminders are still open", "what's pending on my reminder list", "which reminders haven't been done yet",
               "show everything i still have to do"]

    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10, 11]))
        dated = sorted([r for r in _open(w) if r.due], key=lambda r: r.due)
        und = [r for r in _open(w) if not r.due]
        head = f"You have {len(dated) + len(und)} open reminders"
        parts = _bullet_or_prose(head, dated, now) if dated else head + "."
        if und:
            parts += f" There's also {join([r.title for r in und])}, with no date."
        prompt = A.style(rng, prompts[i % len(prompts)])
        return mk("reminders_r3", prompt, now, w, [[Call("search_reminders", scope="all")], Final(parts)],
                  "pending = search_reminders (never list_scheduled_sends); open ones only, a finished one never named")
    return make


def _r_overdue(i):
    prompts = ["which reminders did i miss", "anything past due on my list", "what have i let slip on my reminders"]

    def make(rng):
        none = (i == 2)
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10, 11]), overdue=0 if none else rng.choice([1, 2, 3]))
        late = sorted([r for r in _open(w) if r.due and r.due < now], key=lambda r: r.due)
        if not late:
            final = pick(rng, ["Nothing is overdue.", "You're caught up, no reminder is past due."])
        else:
            n = len(late)
            final = _bullet_or_prose(f"{n} overdue" if n > 1 else "One is overdue", late, now) if n > 1 else \
                pick(rng, [f"One is overdue: {rline(late[0], now)}.", f"Only {rline(late[0], now)} is past due."])
        return mk("reminders_r3", A.style(rng, prompts[i % 3]), now, w, [[Call("search_reminders", scope="past_due")], Final(final)],
                  "overdue = scope past_due; list only those, nothing that is still ahead")
    return make


def _r_today(i):
    prompts = ["anything i need to do today", "what reminders go off today", "is there anything on my reminder list for today"]

    def make(rng):
        none = (i == 2)
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]), today=0 if none else rng.choice([1, 2]))
        todays = sorted([r for r in _open(w) if r.due and r.due.date() == now.date()], key=lambda r: r.due)
        if not todays:
            final = pick(rng, ["Nothing is due today.", "No reminders are set for today."])
        else:
            n = len(todays)
            final = (pick(rng, [f"Just one: {rline(todays[0], now)}.", f"You have {rline(todays[0], now)}."]) if n == 1
                     else f"You have {n} today: " + join([f"{r.title} at {clock(r.due)}" for r in todays]) + ".")
        return mk("reminders_r3", A.style(rng, prompts[i % 3]), now, w, [[Call("search_reminders", scope="today")], Final(final)],
                  "due today = scope today")
    return make


def _r_soon(i):
    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]), overdue=1, today=1, upcoming=4)
        rows = sorted([r for r in _open(w) if r.due and r.due >= now and (r.due.date() - now.date()).days <= 2], key=lambda r: r.due)
        prompt = A.style(rng, ["what's coming up in the next couple of days", "anything due soon, like the next two days"][i % 2])
        if not rows:
            final = "Nothing is due in the next two days."
        else:
            final = _bullet_or_prose("Due in the next two days", rows, now)
        return mk("reminders_r3", prompt, now, w, [[Call("search_reminders", scope="upcoming")], Final(final)],
                  "next couple of days: scope upcoming, then only the ones inside the window")
    return make


def _r_lookup(i):
    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]))
        found = (i % 2 == 0)
        pool = [r for r in w.reminders if not r.done and r.due]
        target = rng.choice(pool)
        word = HANDLE[target.title.lower()] if found else rng.choice(["tuition", "recital", "visa"])
        prompt = A.style(rng, ["do i have a reminder about {x}", "is there a reminder with {x} in it"][i % 2].format(x=word))
        if found:
            hits = [r for r in pool if word in r.title.lower()]
            final = (pick(rng, [f"Yes: {rline(hits[0], now)}."]) if len(hits) == 1
                     else "Yes, " + join([rline(r, now) for r in hits]) + ".")
        else:
            final = pick(rng, [f"No open reminder mentions {word}.", f"I don't see a reminder about {word}."])
        return mk("reminders_r3", prompt, now, w, [[Call("search_reminders", query=word)], Final(final)],
                  "a title search: the matching rows, or a plain none")
    return make


def _r_done_check(i):
    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]))
        gone = [r for r in w.reminders if r.done][0]
        word = HANDLE[gone.title.lower()]
        prompt = A.style(rng, ["is the {x} reminder still waiting on me", "did i already clear the {x} reminder"][i % 2].format(x=word))
        final = pick(rng, [f"There's no open reminder about {word}, so it isn't waiting on you.",
                           f"I don't see an open reminder for {word}, so nothing is pending there."])
        return mk("reminders_r3", prompt, now, w, [[Call("search_reminders", query=word)], Final(final)],
                  "a finished reminder does not show as open: say nothing is pending, claim nothing more")
    return make


def _r_complete(i):
    prompts = ["i finished {x}, tick that off", "mark {x} as done", "{x} is taken care of, you can check it off"]

    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]))
        pool = [r for r in w.reminders if not r.done and r.due]
        r = rng.choice(pool)
        key = HANDLE[r.title.lower()]
        prompt = A.style(rng, prompts[i % 3].format(x=key))
        final = pick(rng, [f"Marked “{r.title}” done.", f"“{r.title}” is checked off.", f"Done, “{r.title}” is complete."])
        return mk("reminders_r3", prompt, now, w, [[Call("complete_reminder", title=key)], Final(final)],
                  "finished = complete_reminder (not a delete), matched by a distinctive part of the title", must_see=["Marked"])
    return make


def _r_rename(i):
    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]))
        pool = [r for r in w.reminders if not r.done and r.due]
        r = rng.choice(pool)
        key = HANDLE[r.title.lower()]
        new = rng.choice(["Pick up the dry cleaning and the shoes", "Call the landlord and the super", "Buy bus tickets for the trip",
                          "Send the lab data to the whole group"])
        prompt = A.style(rng, ["rename the {k} reminder to say {n}", "change the {k} one so it reads {n}"][i % 2].format(k=key, n=new.lower()))
        def upd(args, w=w):
            title = (args.get("title") or "").lower()
            hits = [x for x in w.reminders if not x.done and x.due and title in x.title.lower()]
            if not hits:
                return f"Nothing active matches reminder {args.get('title')!r}."
            if len(hits) > 1:
                return "Several reminders match “%s”: %s. Which one?" % (args.get("title"), "; ".join(h.title for h in hits))
            x = hits[0]
            if args.get("when_iso"):
                x.due = dt.datetime.fromisoformat(args["when_iso"])
            elif args.get("day") in ("today", "tomorrow"):
                x.due = dt.datetime.combine(w.now.date() + D(days=1 if args["day"] == "tomorrow" else 0), x.due.time())
            if (args.get("new_title") or "").strip():
                x.title = args["new_title"].strip()
            effect(w, "update_reminder", title=x.title, when_iso=args.get("when_iso"), day=args.get("day"))
            return f"Reminder updated: “{x.title}” — {x.due:%a %b %-d at %-I:%M %p}."
        w.overrides["update_reminder"] = upd
        final = pick(rng, [f"Renamed it to “{new}”, still set for {r.due:%a} at {clock(r.due)}.", f"Done, it now reads “{new}”.",
                           f"Okay, the reminder is “{new}” now."])
        return mk("reminders_r3", prompt, now, w, [[Call("update_reminder", title=key, new_title=new)], Final(final)],
                  "rename only: update_reminder with title + new_title (the time stays)", must_see=["Reminder updated"])
    return make


def _r_move(i):
    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]), upcoming=3)
        pool = [r for r in w.reminders if not r.done and r.due and r.due > now and r.due.date() > now.date()]
        r = pool[0]
        key = HANDLE[r.title.lower()]
        if i % 2 == 0:
            d = A.wd_at(now, "saturday", "at 3pm")
            prompt = A.style(rng, f"push the {key} reminder to saturday at 3pm")
            call = Call("update_reminder", title=key, when_iso=A.iso(d))
            nice = f"Saturday at 3 PM"
        else:
            tom = now.date() + D(days=1)
            prompt = A.style(rng, f"move the {key} reminder to tomorrow, same time")
            call = Call("update_reminder", title=key, day="tomorrow")
            d = dt.datetime.combine(tom, r.due.time())
            nice = f"tomorrow at {clock(d)}"
        def upd(args, w=w):
            title = (args.get("title") or "").lower()
            hits = [x for x in w.reminders if not x.done and x.due and title in x.title.lower()]
            if len(hits) != 1:
                return "Nothing active matches reminder %r." % args.get("title") if not hits else "Several reminders match. Which one?"
            x = hits[0]
            if args.get("when_iso"):
                x.due = dt.datetime.fromisoformat(args["when_iso"])
            else:
                x.due = dt.datetime.combine(w.now.date() + D(days=1), x.due.time())
            effect(w, "update_reminder", title=x.title, when_iso=args.get("when_iso"), day=args.get("day"))
            return f"Reminder updated: “{x.title}” — {x.due:%a %b %-d at %-I:%M %p}."
        w.overrides["update_reminder"] = upd
        final = pick(rng, [f"Moved “{r.title}” to {nice}.", f"“{r.title}” is now set for {nice}."])
        return mk("reminders_r3", prompt, now, w, [[call], Final(final)],
                  "move a reminder: update_reminder with when_iso, or day='tomorrow' to keep its time", must_see=["Reminder updated"])
    return make


def _r_delete_named(i):
    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]))
        pool = [r for r in w.reminders if not r.done and r.due and r.due > now]
        r = rng.choice(pool)
        key = HANDLE[r.title.lower()]
        prompt = A.style(rng, ["get rid of the {k} reminder", "i don't need the {k} reminder anymore, remove it", "delete the one about {k}"][i % 3].format(k=key))
        final = pick(rng, [f"Deleted “{r.title}”.", f"“{r.title}” is gone.", f"Removed the “{r.title}” reminder."])
        return mk("reminders_r3", prompt, now, w, [[Call("cancel_event", title=key)], Final(final)],
                  "delete one reminder by name: cancel_event (never complete, never clear_reminders)",
                  cards=["cancel_event"], approve=[True], must_see=["Cancelled"])
    return make


def _r_clear(scope, i, deny=False):
    phrases = {"today": ["wipe my reminders for today", "clear out the reminders that are due today"],
               "tomorrow": ["clear tomorrow's reminders", "delete the reminders set for tomorrow"],
               "upcoming": ["remove my upcoming reminders", "delete all the future reminders"],
               "all": ["delete every reminder i have", "wipe my whole reminder list"]}

    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]), today=2, upcoming=3)
        # add a tomorrow reminder so the scope is non-empty
        w.reminders.append(Reminder(cap1(TASKS[(i + 9) % len(TASKS)]), dt.datetime.combine(now.date() + D(days=1), dt.time(10, 0))))
        rows = w.reminder_rows(scope, "", include_stale=True)
        n = len(rows)
        prompt = A.style(rng, phrases[scope][i % 2])
        scope_word = {"today": "today", "tomorrow": "tomorrow", "upcoming": "upcoming", "all": "all"}[scope]
        rm = f"{n} reminder{'s' if n != 1 else ''}"
        if deny:
            return mk("reminders_r3", prompt, now, w, [[Call("clear_reminders", scope=scope)]],
                      f"clear_reminders scope {scope}; the card is denied, the turn ends at the call",
                      cards=["clear_reminders"], approve=[False], no_effect=True)
        if scope in ("today", "tomorrow"):
            final = pick(rng, [f"Cleared {rm} for {scope_word}.", f"Done, {rm} deleted for {scope_word}; the rest are untouched.",
                               f"I removed {rm} due {scope_word} and left everything else alone."])
        elif scope == "upcoming":
            final = pick(rng, [f"Cleared the {n} upcoming reminders.", f"Done, {rm} ahead of you deleted; overdue ones are still there."])
        else:
            final = pick(rng, [f"Cleared all {n} reminders.", f"Your reminder list is empty, {n} deleted."])
        return mk("reminders_r3", prompt, now, w, [[Call("clear_reminders", scope=scope)], Final(final)],
                  f"clear only the named scope: clear_reminders scope={scope}, behind a card",
                  cards=["clear_reminders"], approve=[True], must_see=["Cleared"])
    return make


def _r_clear_overdue(i):
    phrases = ["get rid of my overdue reminders", "clear every reminder that's past due", "wipe the old reminders i never did"]

    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]), overdue=3)
        w.past = []
        w.events = [e for e in w.events if e.end > now]      # only reminders are past due, so the count is theirs
        n = len(w.past_due_rows("", 365))
        def handler(args, w=w):
            rows = w.past_due_rows(args.get("query", ""), args.get("days", 365))
            if not rows:
                return "Nothing past-due to clear."
            for r in rows:
                ref = r["_ref"]
                (w.reminders if ref in w.reminders else (w.past if ref in w.past else w.events)).remove(ref)
            effect(w, "clear_past_reminders", query=args.get("query") or None, days=args.get("days"))
            return f"Cleared {len(rows)} past-due item(s)."
        w.overrides["clear_past_reminders"] = handler
        final = pick(rng, [f"Cleared {n} past-due item{'s' if n != 1 else ''}; everything still ahead is untouched.",
                           f"Done, {n} overdue item{'s' if n != 1 else ''} removed."])
        return mk("reminders_r3", A.style(rng, phrases[i % 3]), now, w, [[Call("clear_past_reminders", days=365)], Final(final)],
                  "overdue / old reminders: clear_past_reminders (one call, behind a card), not a loop of cancels",
                  cards=["clear_past_reminders"], approve=[True], must_see=["Cleared"])
    return make


def _r_clear_vague(i):
    prompts = ["clear my reminders", "delete my reminders please", "wipe out my reminder list for me"]

    def make(rng):
        now, w = _rem_world(rng, hour=rng.choice([8, 9, 10]))
        final = pick(rng, ["Which ones: today's, tomorrow's, the overdue, or everything?", "Do you want just today's cleared, the overdue ones, or every reminder?",
                           "How much should go? Today's, overdue, upcoming, or all of them?"])
        return mk("reminders_r3", A.style(rng, prompts[i % 3]), now, w, [Final(final)],
                  "a clear with no scope: one short question, nothing deleted")
    return make


def _r_event_vs(kind, i):
    """kind: 'event' (calendar write, card) or 'reminder' (the contrast)."""
    ev_titles = [("Study group", "study group"), ("Dentist checkup", "dentist checkup"), ("Coffee with Aisha", "coffee with aisha"),
                 ("Lab meeting", "lab meeting"), ("Climbing session with Ben", "climbing with ben"), ("Advisor meeting", "advisor meeting")]

    def make(rng):
        now, w = A.base_world(rng, hour=rng.choice([8, 9, 10, 11]))
        title, spoken = ev_titles[i % len(ev_titles)]
        wd = rng.choice(["wednesday", "thursday", "friday", "saturday"])
        h = rng.choice([13, 14, 15, 16])
        c = A.clk(h, 0, 0)
        d = A.wd_at(now, wd, f"at {A.clk(h, 0, 0)}")
        if kind == "event":
            mins = rng.choice([30, 45, 60, 90])
            prompt = A.style(rng, ["put {s} on my calendar {wd} at {c}", "block {wd} {c} for the {s}, about {m} minutes"][i % 2]
                             .format(s=spoken, wd=wd, c=c, m=mins))
            if i % 2 == 0:
                mins = 60
            final = pick(rng, [f"Added {title} on {d:%A} at {clock(d)}.", f"{title} is on your calendar for {d:%A} at {clock(d)}."])
            return mk("reminders_r3", prompt, now, w,
                      [[Call("add_calendar_event", title=title, when_iso=A.iso(d), duration_min=mins)], Final(final)],
                      "an appointment with a place on the calendar: add_calendar_event (card), not a reminder",
                      cards=["add_calendar_event"], approve=[True], must_see=["Added"])
        task = TASKS[(i * 4 + 1) % len(TASKS)]
        prompt = A.style(rng, f"{wd} at {c} remind me to {task}")
        final = pick(rng, [f"I'll remind you {d:%A} at {clock(d)} to {A.second(task)}.", f"Reminder set for {d:%A} at {clock(d)}."])
        return mk("reminders_r3", prompt, now, w, [[Call("add_reminder", title=cap1(task), when_iso=A.iso(d))], Final(final)],
                  "a nudge to do something: add_reminder, not a calendar event")
    return make


def fam_reminders_r3(rng):
    _SEEN.clear()
    _OPEN.clear()
    out: list = []
    fo = lambda mk_list: _fill_ordered(out, mk_list, rng, "reminders_r3")
    kinds = (["daypart"] * 3 + ["tomorrow_part"] * 2 + ["clock"] * 3 + ["tomorrow_clock"] * 2 + ["rel"] * 2 + ["tonight"] * 1
             + ["today_part"] * 1)
    fo([_r_add(k, i) for i, k in enumerate(kinds)])
    fo([_r_add_dateonly(i) for i in range(3)] + [_r_add_notime(i) for i in range(2)])
    fo([_r_pending(i) for i in range(4)] + [_r_overdue(i) for i in range(3)] + [_r_today(i) for i in range(3)]
       + [_r_soon(i) for i in range(2)] + [_r_lookup(i) for i in range(2)] + [_r_done_check(i) for i in range(2)])
    fo([_r_complete(i) for i in range(4)])
    fo([_r_rename(i) for i in range(2)] + [_r_move(i) for i in range(2)])
    fo([_r_delete_named(i) for i in range(3)])
    fo([_r_clear("today", 0), _r_clear("today", 1), _r_clear("tomorrow", 0), _r_clear("upcoming", 0), _r_clear("all", 0),
        _r_clear("today", 1, deny=True), _r_clear("all", 1, deny=True)])
    fo([_r_clear_overdue(i) for i in range(2)] + [_r_clear_vague(i) for i in range(2)])
    fo([_r_event_vs("event", i) for i in range(4)] + [_r_event_vs("reminder", i) for i in range(3)])
    return out


FAMILIES_R3 = {"device_r3": fam_device_r3, "contacts_r3": fam_contacts_r3, "reminders_r3": fam_reminders_r3}
