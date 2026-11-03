"""Round-4 family ``info_answers_r4``: device status, unit and time-zone conversion, and Wisp capability questions.

Why. r3 answered these from a glance at the result or from memory. The answer here states the
exact numbers the result shows (the same form: "212G free of 494G", "~2h05m remaining"), so the
round-4 thought can list them and the final can carry every one:

  device status   system_status (overview / battery / network / bluetooth / focus / sound): battery, disk,
                  memory, Wi-Fi, volume, Bluetooth, Focus, macOS. Result formats are the real tool's
                  (scen_r3_device.Dev copies them from service/tools/system_extras.py).
  conversion      convert_units (the real function's own output, so any unit it knows works) and world_time
                  (the real function's logic, run against the scenario's frozen clock): the converted value
                  exactly as the tool prints it, and for time zones the weekday and whether the day changes.
  capability      wisp_capabilities / wisp_status: "can you read my texts", "can you order food", "does it work
                  offline". Every claim is a row of the tool's text; a thing the rows do not list is said
                  plainly to be something Wisp cannot do, and what Wisp can do is named from the rows.

Reads only (WRITE_ALLOW_R4 is empty). Every candidate is dry-run through the host (``_attempt``); one the
host would not run as scripted is dropped.

Host facts found while writing this file (not trained against):
  - a prompt shaped "should i ..." / "do i ..." / "... my <thing>?" with NO tool call is re-prompted by the loop
    (``asks_personal_data``), so the chat family avoids those wordings; here a tool is called, so they run.
  - "how long ..." / "how much time ..." questions leave the turn with no tools (the time-question guard), so
    none is worded that way.
"""
from __future__ import annotations

import datetime as dt
import re

import scen_r2_act as A
from harness import Call, Final
from scen_r3_device import _attempt, dev_world, mk as _mk, pick

FAMILY = "info_answers_r4"
WRITE_ALLOW_R4 = {FAMILY: frozenset()}


def mk(prompt, now, w, steps, notes, **kw):
    return _mk(FAMILY, prompt, now, w, steps, notes, **kw)


def hm(minutes: int) -> str:
    """'125' -> '2h05m', the tool's own form."""
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}m"


# =============================================================================
# DEVICE STATUS
# =============================================================================
SSIDS = ["Linden-Student", "Hollis-Library", "Cafe Verde Guest", "Rivera-5G", "Northline-Guest", "Linden-Secure"]


def _battery(i):
    prompts = ["battery check?", "how's the battery looking", "about to run out of juice on this thing?",
               "what percent is the laptop at", "is the mac charging right now", "battery level, and is it plugged in"]
    p = prompts[i % len(prompts)]

    def make(rng):
        now, w, dev = dev_world(rng, charging=rng.random() < 0.4)
        dev.remaining = int(dev.battery * rng.uniform(3.0, 4.2))
        b = dev.battery
        asks_charge = i % 6 in (4, 5)      # "is it charging" / "is it plugged in": a yes or no first
        left = hm(dev.remaining)
        if asks_charge and dev.charging:
            final = pick(rng, [f"Yes, it's charging, at {b}% right now.", f"Yes, it's plugged in and charging. The battery is at {b}%."])
        elif asks_charge:
            final = pick(rng, [f"No, it's on battery, at {b}% with about {left} remaining.",
                               f"No, it isn't plugged in. The battery is at {b}% and the Mac estimates ~{left} remaining."])
        elif dev.charging:
            final = pick(rng, [f"Your battery is at {b}% and charging.", f"It's plugged in and charging, currently at {b}%."])
        else:
            final = pick(rng, [f"Your battery is at {b}% and not charging, with about {left} remaining.",
                               f"You're on battery power at {b}%, and the Mac estimates ~{left} remaining.",
                               f"{b}% charge, running on battery with about {left} of use remaining."])
        return mk(A.style(rng, p), now, w, [[Call("system_status", section="battery")], Final(final)],
                  "battery: system_status battery; the percent, the charging state and the remaining time exactly as printed")
    return make


def _health(i):
    prompts = ["how worn out is the battery after all this time", "battery wear and cycle count?"]

    def make(rng):
        now, w, dev = dev_world(rng, charging=rng.random() < 0.3)
        final = pick(rng, [f"The battery holds {dev.health}% of its original design capacity, rated Normal, after {dev.cycles} charge cycles.",
                           f"Battery health is {dev.health}% of the original design capacity (Normal), and it has been through {dev.cycles} charge cycles.",
                           f"It's at {dev.health}% of its original design capacity with {dev.cycles} charge cycles, which counts as Normal."])
        return mk(A.style(rng, prompts[i % 2]), now, w, [[Call("system_status", section="battery")], Final(final)],
                  "battery health and cycle count read from the battery section")
    return make


def _disk(i):
    prompts = ["how much space is left on the disk", "storage left?", "am i low on storage", "how full is the drive",
               "whats my free disk space", "is there room for a {n} GB download"]

    def make(rng):
        now, w, dev = dev_world(rng)
        free, total, used = dev.free
        p = prompts[i % len(prompts)]
        if "{n}" in p:
            gb = int(float(free[:-1]) * (1000 if free.endswith("T") else 1))
            size = rng.choice([20, 40, 60, 150, 300])
            p = p.format(n=size)
            if gb > size * 1.5:
                final = pick(rng, [f"Yes, {free} is free of {total}, which is plenty for a {size} GB download.",
                                   f"Yes. You have {free} free of {total} ({used}% used), so {size} GB fits comfortably."])
            elif gb > size:
                final = f"It would fit, but not by much: {free} free of {total}, and the download takes {size} GB of that."
            else:
                final = pick(rng, [f"Not as things stand. You only have {free} free of {total} ({used}% used), and a {size} GB download needs more than that.",
                                   f"No: {free} is free of {total}, so {size} GB would not fit until you clear something."])
        else:
            note = ("That is getting full." if int(used) >= 80 else "That leaves a comfortable amount of room." if int(used) < 60 else "That is a reasonable amount of room for now.")
            final = pick(rng, [f"You have {free} free of {total}, so the disk is {used}% used. {note}",
                               f"The disk shows {free} free of {total} ({used}% used). {note}",
                               f"{free} free of {total}, which puts the disk at {used}% used. {note}"])
        return mk(A.style(rng, p), now, w, [[Call("system_status")], Final(final)],
                  "disk: system_status overview; free of total and the percent exactly as printed")
    return make


def _memory(i):
    prompts = ["is the ram okay right now", "how much memory is free", "am i out of memory"]

    def make(rng):
        now, w, dev = dev_world(rng)
        note = "That is on the low side." if dev.mem_free < 30 else "That is a healthy amount."
        final = pick(rng, [f"This Mac has 24 GB of memory installed, and {dev.mem_free}% of it is free right now. {note}",
                           f"Memory is {dev.mem_free}% free out of 24 GB installed. {note}",
                           f"{dev.mem_free}% of the 24 GB is free at the moment. {note}"])
        return mk(A.style(rng, prompts[i % 3]), now, w, [[Call("system_status")], Final(final)],
                  "memory: system_status overview; installed memory and the percent free as printed")
    return make


def _wifi(i):
    prompts = ["whats the wifi called that im on", "which network is this mac connected to", "name of the network and the local ip?",
               "am i on the campus wifi"]

    def make(rng):
        ssid = rng.choice(SSIDS)
        now, w, dev = dev_world(rng, ssid=ssid)
        p = prompts[i % 4]
        if i % 4 == 2:
            final = pick(rng, [f"You're connected to {ssid}, and the local IP address is 10.24.8.113.",
                               f"The network is {ssid} and this Mac's local IP is 10.24.8.113."])
        elif i % 4 == 3:
            if ssid.startswith("Linden"):
                final = f"Yes, you're on {ssid}, which is a campus network."
            else:
                final = f"No, you're on {ssid} right now, not a campus network."
        else:
            final = pick(rng, [f"You're connected to {ssid}.", f"This Mac is on the {ssid} network."])
        return mk(A.style(rng, p), now, w, [[Call("system_status", section="network")], Final(final)],
                  "Wi-Fi: system_status network; the network name (and local IP when asked) as printed")
    return make


def _volume(i):
    prompts = ["is the sound up too loud", "what's the output volume and the mic level", "is the mac muted", "volume check"]

    def make(rng):
        muted = rng.random() < 0.3
        now, w, dev = dev_world(rng, muted=muted)
        p = prompts[i % 4]
        mute_word = "muted" if muted else "not muted"
        if i % 4 == 1:
            final = f"Output volume is {dev.volume}% and it is {mute_word}. The microphone input volume is 72%."
        elif muted:
            final = pick(rng, [f"The Mac is muted. The output volume is set to {dev.volume}%, so it will come back at that level when you unmute.",
                               f"It's muted, with the output volume at {dev.volume}%."])
        else:
            final = pick(rng, [f"The output volume is {dev.volume}% and it isn't muted.",
                               f"Sound is on at {dev.volume}%, not muted, so it will play at that level."])
        return mk(A.style(rng, p), now, w, [[Call("system_status", section="sound")], Final(final)],
                  "volume: system_status sound; the output volume and mute state as printed")
    return make


def _bluetooth(i):
    prompts = ["what bluetooth stuff is connected right now", "is bluetooth on", "anything connected over bluetooth?"]

    def make(rng):
        on = (i != 1) or rng.random() < 0.5
        now, w, dev = dev_world(rng)
        dev.settings["bluetooth"] = on
        if on and i % 3 == 1:     # "is bluetooth on": the yes, and no more than the device that makes it useful
            final = pick(rng, ["Yes, Bluetooth is on, and AirPods Pro are connected.", "It is on, with AirPods Pro connected."])
        elif on:
            final = pick(rng, ["Bluetooth is on, with 1 device connected: AirPods Pro, at a signal of -52 dBm.",
                               "Bluetooth is on and AirPods Pro are connected, with a signal of -52 dBm."])
        else:
            final = pick(rng, ["Bluetooth is off, so no devices are connected.", "It's off right now, which means nothing is connected over Bluetooth."])
        return mk(A.style(rng, prompts[i % 3]), now, w, [[Call("system_status", section="bluetooth")], Final(final)],
                  "Bluetooth: system_status bluetooth; on or off and the connected device as printed")
    return make


def _focus(i):
    prompts = ["am i in a focus mode right now", "is dnd active at the moment", "any focus turned on?"]

    def make(rng):
        name = rng.choice(["Work", "Sleep", "Do Not Disturb", "Reading"]) if rng.random() < 0.6 else None
        now, w, dev = dev_world(rng, focus=name)
        if name:
            final = pick(rng, [f"Yes, Focus is on: {name}.", f"{name} is the Focus that's on right now."])
        else:
            final = pick(rng, ["No Focus is turned on by hand right now. A scheduled or automatic Focus isn't recorded where I can read it, so I can't rule that one out.",
                               "None that was turned on by hand. If a Focus starts on a schedule or by app, that isn't something I can see."])
        return mk(A.style(rng, prompts[i % 3]), now, w, [[Call("system_status", section="focus")], Final(final)],
                  "Focus: system_status focus; the Focus named, or the honest limit of what can be read")
    return make


def _macos(i):
    prompts = ["what os build is this laptop on", "which macos am i running, version and build"]

    def make(rng):
        now, w, dev = dev_world(rng)
        final = pick(rng, [f"You're on macOS {dev.version[0]}, build {dev.version[1]}.",
                           f"This Mac runs macOS {dev.version[0]} ({dev.version[1]})."])
        return mk(A.style(rng, prompts[i % 2]), now, w, [[Call("system_status")], Final(final)],
                  "macOS version and build exactly as the overview prints them")
    return make


def _overview(kind, i):
    prompts = {"check": ["give me a quick health check of the laptop", "status report on this mac please"],
               "pair": ["battery and storage, quick", "how's the charge and how much disk is left"],
               "slow": ["the mac feels slow, is something wrong with it", "is the cpu busy or is it just me"]}
    busy_q = kind == "slow" and i % 2 == 1

    def make(rng):
        now, w, dev = dev_world(rng, charging=rng.random() < 0.4)
        dev.remaining = int(dev.battery * rng.uniform(3.0, 4.2))
        free, total, used = dev.free
        b = dev.battery
        bat = (f"the battery is at {b}% and charging" if dev.charging
               else f"the battery is at {b}% and not charging, with ~{hm(dev.remaining)} remaining")
        load = f"{dev.load[0]:.2f}"
        if kind == "check":
            final = pick(rng, [f"{bat[0].upper() + bat[1:]}. The disk has {free} free of {total} ({used}% used), memory is {dev.mem_free}% free, and the CPU load is {load} across {dev.cores} cores.",
                               f"Here's the picture: {bat}, {free} free of {total} on the disk, {dev.mem_free}% of memory free, and a CPU load of {load} over the last minute."])
        elif kind == "pair":
            final = pick(rng, [f"{bat[0].upper() + bat[1:]}, and the disk has {free} free of {total} ({used}% used).",
                               f"Battery: {b}%, {'charging' if dev.charging else 'not charging'}. Disk: {free} free of {total}, {used}% used."])
        else:
            # a verdict first (load against the cores), then the figures behind it; memory is only called low when it is
            ratio = dev.load[0] / dev.cores
            busy = ratio >= 0.9
            fig = f"load is {load} (1m) across {dev.cores} cores, memory is {dev.mem_free}% free, and the disk is {used}% used"
            if busy_q:
                fig = f"load is {load} (1m) across {dev.cores} cores"
                verdict = ("Yes, the CPU is busy" if busy else "No, the CPU isn't busy" if ratio < 0.5 else "The CPU is moderately loaded")
                final = f"{verdict}. CPU {fig}. " + ("Something on this Mac is working hard." if busy else
                                                     "The processor has plenty of headroom." if ratio < 0.5 else "That isn't extreme.")
            else:
                tail = ("Memory is getting low, which could explain it." if dev.mem_free < 15 else
                        "The disk is getting full, which can slow things down." if int(used) >= 80 else "Nothing there looks strained.")
                final = f"Nothing alarming turns up. CPU {fig}. {tail}"
        return mk(A.style(rng, prompts[kind][i % 2]), now, w, [[Call("system_status")], Final(final)],
                  f"overview ({kind}): one system_status call; every figure asked for, as printed")
    return make


# =============================================================================
# CONVERSIONS: the real convert_units and world_time against the scenario clock
# =============================================================================
def install_conversions(w):
    from zoneinfo import ZoneInfo

    from service.tools import conversions
    from service.tools.reference_tools import _fmt, _label, _resolve_zone

    la = ZoneInfo("America/Los_Angeles")
    real_convert = conversions.convert_units

    def convert(args):
        return real_convert(args["value"], args["from_unit"], args["to_unit"])

    def world_time(args):
        place, compare = (args.get("place") or ""), (args.get("compare_to") or "")
        local = w.now.replace(tzinfo=la)
        if not place and not compare:
            return f"It's {_fmt(local)} ({local.tzname()})."
        if place and compare:
            a, b = _resolve_zone(place), _resolve_zone(compare)
            if a is None or b is None:
                return f"(error: I don't know a place called {place if a is None else compare!r}.)"
            now_a, now_b = local.astimezone(a), local.astimezone(b)
            hours = ((now_a.utcoffset() or dt.timedelta()) - (now_b.utcoffset() or dt.timedelta())).total_seconds() / 3600
            if hours == 0:
                rel = "the same time as"
            else:
                mag = f"{abs(hours):.0f}" if abs(hours) == int(abs(hours)) else f"{abs(hours):.1f}"
                rel = f"{mag} hour{'s' if abs(hours) != 1 else ''} {'ahead of' if hours > 0 else 'behind'}"
            la_, lb = _label(place, a), _label(compare, b)
            return f"{la_} is {rel} {lb}.\n  {la_}: {_fmt(now_a)}\n  {lb}: {_fmt(now_b)}"
        target = place or compare
        zone = _resolve_zone(target)
        if zone is None:
            return f"(error: I don't know a place called {target!r}. Try a major city or an IANA zone like 'Europe/London'.)"
        there = local.astimezone(zone)
        delta = ((there.utcoffset() or dt.timedelta()) - (local.utcoffset() or dt.timedelta())).total_seconds() / 3600
        if delta == 0:
            rel = "same as here"
        else:
            mag = f"{abs(delta):.0f}" if abs(delta) == int(abs(delta)) else f"{abs(delta):.1f}"
            rel = f"{mag}h {'ahead of' if delta > 0 else 'behind'} you"
        return f"In {_label(target, zone)} it's {_fmt(there)} ({rel})."

    w.overrides["convert_units"] = convert
    w.overrides["world_time"] = world_time
    return world_time


# (prompt, value, from_unit, to_unit, how the final names the units)
UNITS = [
    ("150 pounds in kg", 150, "lb", "kg", ("pounds", "kg")),
    ("what is 180 pounds in kilos", 180, "lb", "kg", ("pounds", "kg")),
    ("26.2 miles to kilometers", 26.2, "mi", "km", ("miles", "km")),
    ("6 feet in centimeters", 6, "ft", "cm", ("feet", "cm")),
    ("convert 12 inches to centimeters", 12, "in", "cm", ("inches", "cm")),
    ("100 yards in meters", 100, "yd", "m", ("yards", "m")),
    ("8 miles in kilometers", 8, "mi", "km", ("miles", "km")),
    ("30 mph to km/h", 30, "mph", "km/h", ("mph", "km/h")),
    ("what's 68 fahrenheit in celsius", 68, "F", "C", None),
    ("350 f to c", 350, "F", "C", None),
    ("20 celsius to fahrenheit", 20, "C", "F", None),
    ("98.6 degrees f in celsius", 98.6, "F", "C", None),
    ("1.5 liters in milliliters", 1.5, "L", "ml", ("liters", "ml")),
    ("2 pounds in ounces", 2, "lb", "oz", ("pounds", "ounces")),
    ("4.5 hours expressed in minutes", 4.5, "h", "min", ("hours", "min")),
    ("3 days in hours", 3, "days", "hours", ("days", "hours")),
    ("1.5 gb to mb", 1.5, "GB", "MB", ("GB", "MB")),
    ("500 mb in gb", 500, "MB", "GB", ("MB", "GB")),
    ("a 2 tb drive is how many gb", 2, "TB", "GB", ("TB", "GB")),
    ("10 inches to cm", 10, "in", "cm", ("inches", "cm")),
]


def _unit(i):
    prompt, val, fu, tu, names = UNITS[i % len(UNITS)]

    def make(rng):
        now, w, dev = dev_world(rng)
        install_conversions(w)
        res = w.overrides["convert_units"]({"value": val, "from_unit": fu, "to_unit": tu})
        m = re.fullmatch(r"(.+?) = (.+)", res)
        assert m, res
        lhs, rhs = m.group(1), m.group(2)             # "70 kg", "154.3235835 lb" / "68°F", "20°C"
        # a long decimal is said rounded to two places with "about" (the thought rounds the same way)
        rnd = lambda t: re.sub(r"\d+\.\d{3,}", lambda mm: f"{float(mm.group(0)):.2f}", t)
        about = "about " if rnd(rhs) != rhs else ""
        echo = not about or (names is not None and "." not in lhs and fu != "mph")     # a rounded answer drops an awkward input echo
        if names is None:
            final = (pick(rng, [f"{lhs} is {about}{rnd(rhs)}.", f"{lhs} comes to {about}{rnd(rhs)}."]) if echo else
                     pick(rng, [f"That comes to {about}{rnd(rhs)}.", f"It works out to {about}{rnd(rhs)}."]))
        else:
            num_from, num_to = lhs.split(" ", 1)[0], rnd(rhs.split(" ", 1)[0])
            final = (pick(rng, [f"{num_from} {names[0]} is {about}{num_to} {names[1]}.",
                                f"{num_from} {names[0]} comes to {about}{num_to} {names[1]}.",
                                f"{num_from} {names[0]} works out to {about}{num_to} {names[1]}."]) if echo else
                     pick(rng, [f"That comes to {about}{num_to} {names[1]}.", f"It works out to {about}{num_to} {names[1]}."]))
        return mk(A.style(rng, prompt), now, w,
                  [[Call("convert_units", value=val, from_unit=fu, to_unit=tu)], Final(final)],
                  "unit conversion: convert_units, the converted value exactly as the tool prints it")
    return make


# cities whose label in world_time equals how they are typed
CITIES = ["Tokyo", "London", "Sydney", "Paris", "Dubai", "Singapore", "Seoul", "Auckland", "Berlin", "Cairo",
          "Honolulu", "New York", "Chicago", "Toronto", "Madrid", "Hong Kong", "Mexico City", "Sao Paulo"]


def _split_when(s):
    """'6:12 AM on Thursday, November 5, 2026' -> ('6:12 AM', 'Thursday, November 5')."""
    m = re.fullmatch(r"(\d{1,2}:\d\d [AP]M) on (\w+, \w+ \d{1,2}), \d{4}", s)
    assert m, s
    return m.group(1), m.group(2)


def _tz_one(i, intent):
    city = CITIES[i % len(CITIES)]
    prompts = {
        "now": ["whats the clock say in {c} at this moment", "current time in {c}?", "time in {c} at the moment, i want to call someone there"],
        "day": ["is it already tomorrow in {c}", "is {c} on the next day yet"],
        "weekday": ["what day is it in {c} at the moment", "which weekday is it over in {c}"],
        "dark": ["is it day or night in {c} right now"],
    }[intent]

    def make(rng):
        now, w, dev = dev_world(rng)
        wt = install_conversions(w)
        res = wt({"place": city})
        m = re.fullmatch(r"In (.+?) it's (.+?) \((.+)\)\.", res)
        label, when, rel = m.group(1), m.group(2), m.group(3)
        time_, day = _split_when(when)
        mag = re.match(r"([\d.]+)h (ahead of|behind) you", rel)
        gap = f"{mag.group(1)} hours {mag.group(2)} you" if mag else "the same time as you"
        if mag and mag.group(1) == "1":
            gap = f"1 hour {mag.group(2)} you"
        # day relation from the real zone arithmetic
        from zoneinfo import ZoneInfo
        from service.tools.reference_tools import _resolve_zone
        local = w.now.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        there = local.astimezone(_resolve_zone(city))
        diff = (there.date() - local.date()).days
        rel_day = {0: "the same as yours", 1: "one day ahead of yours", -1: "one day behind yours"}[diff]
        hour = there.hour
        part = ("late at night" if hour >= 23 or hour < 4 else "early morning" if hour < 7 else "morning" if hour < 12
                else "afternoon" if hour < 17 else "evening" if hour < 21 else "night")
        if intent == "now":
            final = pick(rng, [f"It's {time_} on {day} in {label}. That is {gap}, and the date there is {rel_day}.",
                               f"In {label} it's {time_} on {day}, {gap}, so the date there is {rel_day}."])
        elif intent == "weekday":
            final = pick(rng, [f"It's {day} in {label} right now, at {time_}. The date there is {rel_day}.",
                               f"In {label} it's {day}, and the time there is {time_}, so the date is {rel_day}."])
        elif intent == "day":
            if diff == 1:
                final = f"Yes, it's already tomorrow there: {day} in {label}, at {time_}. That is {gap}."
            elif diff == -1:
                final = f"No, {label} is still on the day before yours: it's {time_} on {day} there, which is {gap}."
            else:
                final = f"Not yet, it's the same day there: {day} in {label}, at {time_}, which is {gap}."
        else:
            night = hour >= 21 or hour < 6
            final = (f"It's night there: {time_} on {day} in {label}." if night else
                     f"It's daytime there, {time_} on {day} in {label}, which is {part} for them.")
        return mk(A.style(rng, prompts[i % len(prompts)].format(c=city.lower() if rng.random() < 0.5 else city)),
                  now, w, [[Call("world_time", place=city)], Final(final)],
                  f"time zone ({intent}): world_time with place; the time and weekday as printed, and whether the day differs")
    return make


def _tz_compare(i):
    pairs = [("Tokyo", "New York"), ("London", "Chicago"), ("Sydney", "London"), ("Paris", "Toronto"),
             ("Dubai", "Berlin"), ("Seoul", "Los Angeles")]
    a, b = pairs[i % len(pairs)]
    prompts = ["{a} compared with {b}: which one is ahead and by how much", "{a} vs {b}, how far apart are they in time",
               "is {a} ahead of {b}, and what time is it in each"]

    def make(rng):
        now, w, dev = dev_world(rng)
        wt = install_conversions(w)
        res = wt({"place": a, "compare_to": b})
        head, la_, lb = res.split("\n")
        ta, da = _split_when(la_.split(": ", 1)[1])
        tb, db = _split_when(lb.split(": ", 1)[1])
        final = pick(rng, [f"{head} Right now it's {ta} on {da} in {a} and {tb} on {db} in {b}.",
                           f"{head} It's {ta} on {da} in {a}, while {b} is at {tb} on {db}."])
        return mk(A.style(rng, prompts[i % 3].format(a=a.lower(), b=b.lower())), now, w,
                  [[Call("world_time", place=a, compare_to=b)], Final(final)],
                  "two places: world_time with place and compare_to; the offset sentence and both times as printed")
    return make


# =============================================================================
# CAPABILITY QUESTIONS: every claim is a row of the tool's text
# =============================================================================
REPORT = {
    "msg": "Yes — Read Messages", "send_msg": "Yes — Send text messages (confirmation required)",
    "draft_msg": "Yes — Draft text messages", "mail": "Yes — Read Mail", "send_mail": "Yes — Send email (confirmation required)",
    "reply": "Yes — Reply in an existing email thread (confirmation required)",
    "sched": "Yes — Schedule a new email or text for later (confirmation required)",
    "remind": "Yes — Create reminders", "move": "Yes — Move calendar events to a new time (confirmation required)",
    "cal": "Yes — Read calendar/reminders", "hist": "Yes — Read browser history", "notes": "Yes — Search Notes",
    "files": "Yes — Read and organize local files", "sys": "Yes — Change system settings (confirmation required)",
    "no_sched_reply": "No — schedule a reply inside an existing email thread (only new standalone emails and texts can be scheduled)",
    "no_bank": "No — access a bank balance unless a dedicated connected banking tool is installed",
}

# (prompt, mode, final, needed rows or phrases of the tool text). mode: report / howto / overview / privacy / models.
CAPS = [
    ("can you read my texts", "report",
     "Yes, I can read your Messages. Sending a text needs your confirmation first, so nothing goes out until you approve it.",
     ["msg", "send_msg"]),
    ("can you order me some food", "report",
     "Ordering food isn't something I can do. I can read your Mail and Messages, set reminders, and send emails or texts once you confirm them.",
     ["mail", "msg", "remind", "send_mail"]),
    ("can you book a flight for me", "report",
     "Booking flights isn't something I can do. I can set a reminder for the trip and read your Mail to find a booking confirmation.",
     ["remind", "mail"]),
    ("can you pay a bill for me", "report",
     "I can't pay bills, and I can't see a bank balance unless a dedicated banking tool is connected. What I can do is create a reminder so the due date doesn't slip.",
     ["no_bank", "remind"]),
    ("can you ring my mom", "report",
     "Phone calls aren't something I can place. I can draft a text to her and send it once you confirm, or do the same with an email.",
     ["draft_msg", "send_msg", "send_mail"]),
    ("can you answer an email thread that's already open", "report",
     "Yes, I can reply inside an existing email thread. I show it to you and wait for your confirmation before it goes out.",
     ["reply"]),
    ("can you schedule a text to go out tomorrow morning", "report",
     "Yes, I can schedule a new text for later, and I ask for your confirmation first.",
     ["sched"]),
    ("can you set a reply to an email thread to go out later", "report",
     "Not quite: only new emails and texts can be scheduled, so a reply inside an existing thread can't wait for a set time. I can reply right away once you confirm it.",
     ["no_sched_reply", "reply"]),
    ("can you go through my browsing history", "report",
     "Yes, I can read your browser history.",
     ["hist"]),
    ("can you tidy up the downloads folder", "report",
     "Yes, I can read and organize local files, so tidying a folder is something I can help with.",
     ["files"]),
    ("can you change settings on the mac", "report",
     "Yes, I can change system settings, and I ask for your confirmation before I do.",
     ["sys"]),
    ("can you reschedule a meeting on the calendar", "report",
     "Yes, I can move a calendar event to a new time once you confirm it. I can also read your calendar and reminders first to find a better slot.",
     ["move", "cal"]),
    ("can you tell me my bank balance", "report",
     "No, I can't access a bank balance unless a dedicated banking tool is connected to me.",
     ["no_bank"]),
    ("can you dig through my notes", "report",
     "Yes, I can search your Notes.",
     ["notes"]),
    ("can you take a photo", "report",
     "Taking photos isn't something I can do. I can read your Mail, Messages, Notes, calendar and reminders, and I can create reminders.",
     ["mail", "msg", "notes", "cal", "remind"]),
    ("can you switch on the lights at home", "report",
     "Controlling lights isn't something I can do. What I can change are the Mac's own system settings, and I ask for your confirmation first.",
     ["sys"]),
    ("can you prepare a text draft and leave it unsent", "report",
     "Yes, I can draft a text message without sending it. A text only goes out after you confirm.",
     ["draft_msg", "send_msg"]),
    ("do you have access to mail as well as messages", "report",
     "Yes, I can read your Mail and your Messages.",
     ["mail", "msg"]),
    ("does it work if the wifi is down", "privacy",
     "Partly. The AI model runs on your Mac, and your mail, messages, calendar and notes are kept here, but web searches, weather, stocks and research have to go online.",
     ["My AI model runs here, on your Mac", "Some things have to go online: web searches, weather, stocks and research"]),
    ("is anything i say uploaded somewhere", "privacy",
     "Not by default. Your mail, messages, calendar, notes and what you tell me are read and kept on this Mac, and no cloud AI is used unless you turn one on in Settings.",
     ["read and kept on this Mac, not on a server", "No cloud AI is used unless you turn one on in"]),
    ("does a cloud model ever get used", "privacy",
     "Only if you turn one on in Settings. By default the AI model runs on your Mac, and just web searches, weather, stocks and research go online.",
     ["No cloud AI is used unless you turn one on in", "web searches, weather, stocks and research"]),
    ("what can you actually do for me", "overview",
     "I can read and sum up your calendar, reminders, mail, messages and notes, and draft and send emails and texts. I can add reminders and calendar events, search the web, check the weather, do quick math, and open apps. Sends and calendar changes wait for your OK.",
     ["Read and sum up your calendar, reminders, mail, messages and notes", "Search the web, check the weather and do quick math",
      "Open apps and change Mac settings like Do Not Disturb or dark mode", "I show it to you and wait for your OK"]),
    ("what kinds of things can i ask you", "overview",
     "Plenty: what's on your calendar, which emails need a reply, or a reminder at a set time, for example. You can also ask me to search the web, check the weather, do quick math or remember something you tell me.",
     ["\"What's on my calendar tomorrow?\"", "\"Any emails I need to reply to?\"", "Remember what you tell me"]),
    ("can you open apps for me", "overview",
     "Yes, I can open apps, and I can change Mac settings like Do Not Disturb or dark mode.",
     ["Open apps and change Mac settings like Do Not Disturb or dark mode"]),
    ("can you do the weather and quick sums too", "overview",
     "Yes, I can check the weather and do quick math, and I can search the web as well.",
     ["Search the web, check the weather and do quick math"]),
    ("is there a way to just talk to you instead of typing", "howto",
     "Yes. Press ⌥⇧Space from any app, or click the mic in Ask. I send your question when you pause and read the answer aloud.",
     ["press **⌥⇧Space** from any app, or click the mic in Ask", "Wisp sends your question when you pause and reads the answer"]),
    ("is there a page that shows my whole day", "howto",
     "Yes: Brief and Day are the tabs next to Ask. Brief is your day at a glance, and Day is today's timeline of events and reminders.",
     ["Brief is your day at a glance", "Day is today's timeline of events and reminders"]),
    ("how would i get my gmail into this", "howto",
     "Open Settings → Google and sign in. Then I'll read that account's Gmail and Calendar too.",
     ["open **Settings → Google**, then sign in", "I'll read that account's Gmail and Calendar too"]),
    ("what ai is behind you", "models",
     "I'm running gemma-4-E4B-it-oQ4e-mtp, locally on your Mac in Wisp's bundled engine, and cloud is off.",
     ["gemma-4-E4B-it-oQ4e-mtp, running locally in Wisp's bundled engine", "Cloud: off"]),
]


def _cap(i):
    prompt, mode, final, needs = CAPS[i]

    def make(rng):
        now, w, dev = dev_world(rng)
        if mode == "models":
            call, text = Call("wisp_status", topic="models"), w.t_wisp_status(topic="models")
        elif mode == "report":
            call, text = Call("wisp_capabilities", query=prompt), w.t_wisp_capabilities(query=prompt)
        elif mode == "howto":
            call, text = Call("wisp_capabilities", topic="howto", query=prompt), w.t_wisp_capabilities(query=prompt, topic="howto")
        else:
            call, text = Call("wisp_capabilities", topic=mode), w.t_wisp_capabilities(topic=mode)
        need_text = [REPORT.get(n, n) for n in needs]
        missing = [n for n in need_text if n not in text]
        assert not missing, (prompt, missing)       # the final may only state what this text says
        return mk(A.style(rng, prompt) if i % 3 == 0 else prompt, now, w, [[call], Final(final)],
                  "Wisp capability: honest and short; every claim is a row of the tool's text")
    return make


# =============================================================================
def fam_info_answers_r4(rng):
    out: list = []

    def fill(makers):
        for mkr in makers:
            sc = _attempt(mkr, rng, FAMILY)
            if sc is not None:
                out.append(sc)

    fill([_battery(i) for i in range(6)] + [_health(i) for i in range(2)])
    fill([_disk(i) for i in range(6)] + [_memory(i) for i in range(3)])
    fill([_wifi(i) for i in range(4)] + [_volume(i) for i in range(4)] + [_bluetooth(i) for i in range(3)]
         + [_focus(i) for i in range(3)] + [_macos(i) for i in range(2)])
    fill([_overview("check", i) for i in range(2)] + [_overview("pair", i) for i in range(2)]
         + [_overview("slow", i) for i in range(2)])
    fill([_unit(i) for i in range(len(UNITS))])
    fill([_tz_one(i, "now") for i in range(6)] + [_tz_one(i + 6, "day") for i in range(4)] + [_tz_one(i + 12, "weekday") for i in range(3)] + [_tz_one(i + 10, "dark") for i in range(2)]
         + [_tz_compare(i) for i in range(6)])
    fill([_cap(i) for i in range(len(CAPS))])
    return out


FAMILIES_R4 = {FAMILY: fam_info_answers_r4}
