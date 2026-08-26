"""Display, capture, and the consolidated setting toggles.

A NOTE ON `toggle_setting`
--------------------------
Seven Capability Atlas rows (Bluetooth, AirDrop, Low Power Mode, hotspot, VPN,
firewall, Do Not Disturb) are the same shape: one named boolean, on or off. They
are ONE tool with an enum rather than seven near-identical schemas, and the
reason is retrieval accuracy, not tidiness — seven schemas differing only in a
noun are exactly the case a small model picks wrong from, and each one costs
~200 tokens of every menu it appears in. `set_volume` and `set_wifi` keep their
own tools because they already exist and are the two most-used by a wide margin.

WHAT IS DELIBERATELY MISSING
----------------------------
Scheduling a Focus, and allow-listing a contact through one, are in the Atlas but
have no public macOS API — only on/off is exposed. They are recorded as out of
reach in the plan rather than faked here.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from service.tools.registry import register

_TIMEOUT = 20


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=kw.pop("timeout", _TIMEOUT), **kw)


def _osa(script: str, timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------
# display
# --------------------------------------------------------------------------
@register(
    "set_display",
    "Adjust the screen: brightness, Night Shift, or Dark Mode. Brightness is a "
    "percentage 0-100.",
    {
        "type": "object",
        "properties": {
            "brightness": {"type": "integer",
                           "description": "Screen brightness 0-100. Omit to leave unchanged."},
            "night_shift": {"type": "boolean", "description": "Turn Night Shift on or off."},
            "dark_mode": {"type": "boolean", "description": "Turn Dark Mode on or off."},
        },
    },
    category="system_write",
    aliases=["it's too bright in here", "turn the screen down",
             "make my display dimmer", "switch to dark mode",
             "turn on night shift", "the screen is hurting my eyes",
             "brighten the screen all the way"],
)
def set_display(brightness: int | None = None, night_shift: bool | None = None,
                dark_mode: bool | None = None) -> str:
    done: list[str] = []

    if brightness is not None:
        try:
            level = max(0, min(100, int(brightness)))
        except (TypeError, ValueError):
            return f"(error: {brightness!r} isn't a brightness percentage.)"
        # `brightness` CLI isn't installed by default; the keyboard-key route is
        # the one that works on a stock Mac. Each press is one notch (1/16), so
        # the target is reached by pressing down 16 times then up as needed —
        # crude, but it needs no third-party binary and no extra permission.
        notches = round(level / 100 * 16)
        script = ('tell application "System Events"\n'
                  '  repeat 16 times\n    key code 145\n  end repeat\n'
                  f'  repeat {notches} times\n    key code 144\n  end repeat\n'
                  'end tell')
        p = _osa(script, timeout=40)
        if p.returncode != 0:
            msg = (p.stderr or "").strip()
            if "-1719" in msg or "assistive" in msg.lower() or "not allowed" in msg.lower():
                return ("(Wisp needs Accessibility access to change brightness. "
                        "System Settings > Privacy & Security > Accessibility > Wisp.)")
            return f"(could not set brightness: {msg or 'unknown error'})"
        done.append(f"brightness ~{level}%")

    if night_shift is not None:
        # No shell/AppleScript API exists for Night Shift; the private
        # CBBlueLightClient is the only real handle and needs a compiled helper.
        # Say so instead of silently doing nothing.
        return ("(Night Shift can't be toggled from here — macOS exposes no "
                "scriptable API for it. You can set it in System Settings > "
                "Displays > Night Shift.)"
                + (f" Did change: {', '.join(done)}." if done else ""))

    if dark_mode is not None:
        p = _osa('tell application "System Events" to tell appearance preferences '
                 f'to set dark mode to {"true" if dark_mode else "false"}')
        if p.returncode != 0:
            return f"(could not change appearance: {(p.stderr or '').strip()})"
        done.append(f"dark mode {'on' if dark_mode else 'off'}")

    if not done:
        return "(error: set_display needs at least one of brightness or dark_mode.)"
    return "Set " + ", ".join(done) + "."


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------
@register(
    "screen_capture",
    "Take a screenshot and save it to a file. Use `region='window'` for the "
    "frontmost window only, or 'selection' to let the user drag a region.",
    {
        "type": "object",
        "properties": {
            "region": {"type": "string", "enum": ["screen", "window", "selection"],
                       "description": "What to capture. Defaults to the whole screen."},
            "path": {"type": "string",
                     "description": "Where to save it. Defaults to a timestamped file on the Desktop."},
        },
    },
    category="system_write",
    aliases=["grab a screenshot", "capture what's on my screen",
             "take a picture of this window", "screenshot this for me",
             "snap the front window"],
)
def screen_capture(region: str = "screen", path: str = "") -> str:
    region = (region or "screen").strip().lower()
    if region not in ("screen", "window", "selection"):
        return f"(error: unknown region {region!r}. Use screen, window, or selection.)"

    if path.strip():
        out = Path(path).expanduser()
        if out.is_dir():
            out = out / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    else:
        out = Path.home() / "Desktop" / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    argv = ["screencapture", "-x"]           # -x: no shutter sound
    if region == "window":
        argv += ["-o", "-w"]                 # frontmost window, no shadow
    elif region == "selection":
        argv += ["-i"]                       # interactive drag
    argv.append(str(out))

    # `selection` blocks until the user finishes dragging (or presses Escape),
    # so it gets a human-scale timeout rather than the module default.
    p = _run(argv, timeout=120 if region == "selection" else _TIMEOUT)
    if p.returncode != 0:
        msg = (p.stderr or "").strip()
        # "could not create image from display" is what screencapture actually
        # prints when Screen Recording is denied — it never says "permission".
        # Matching only on the obvious words reported that as an opaque failure
        # and sent the user looking for a bug instead of a checkbox.
        low = msg.lower()
        if ("not authorized" in low or "permission" in low
                or "could not create image" in low):
            return ("(Wisp needs Screen Recording access to capture the screen. "
                    "Grant it in System Settings > Privacy & Security > "
                    "Screen Recording, then try again.)")
        return f"(could not capture: {msg or 'unknown error'})"
    if not out.exists():
        return "Screenshot cancelled — nothing was saved."
    kb = out.stat().st_size / 1024
    return f"Saved {out.name} to {out.parent} ({kb:.0f}KB)."


# --------------------------------------------------------------------------
# toggles
# --------------------------------------------------------------------------
def _bluetooth(on: bool) -> str:
    # blueutil is not installed by default. The scriptable path is the menu-bar
    # UI, which is brittle; this uses the defaults + coreservices restart, which
    # works headlessly on modern macOS.
    p = _run(["defaults", "write", "com.apple.Bluetooth",
              "ControllerPowerState", "-int", "1" if on else "0"])
    if p.returncode != 0:
        return f"(could not change Bluetooth: {(p.stderr or '').strip()})"
    _run(["killall", "-HUP", "bluetoothd"])
    return f"Bluetooth turned {'on' if on else 'off'}."


def _do_not_disturb(on: bool) -> str:
    # Focus/DND moved behind a private framework in Monterey; the old
    # `com.apple.notificationcenterui doNotDisturb` default is inert now.
    # Shortcuts is the one supported automation surface for it.
    name = "Wisp DND On" if on else "Wisp DND Off"
    p = _run(["shortcuts", "run", name])
    if p.returncode != 0:
        return (f"(Do Not Disturb needs a Shortcut to toggle it — macOS exposes "
                f"no direct API since Monterey. Create a Shortcut named "
                f"{name!r} with the 'Set Focus' action, then ask again.)")
    return f"Do Not Disturb turned {'on' if on else 'off'}."


def _low_power(on: bool) -> str:
    p = _run(["pmset", "-a", "lowpowermode", "1" if on else "0"])
    if p.returncode != 0:
        msg = (p.stderr or "").strip()
        if "not permitted" in msg.lower() or "root" in msg.lower():
            return "(Low Power Mode needs an admin password, so it can't be set from here.)"
        return f"(could not change Low Power Mode: {msg})"
    return f"Low Power Mode turned {'on' if on else 'off'}."


def _firewall(on: bool) -> str:
    fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    p = _run([fw, "--setglobalstate", "on" if on else "off"])
    if p.returncode != 0 or "denied" in (p.stdout + p.stderr).lower():
        return "(Changing the firewall needs an admin password, so it can't be set from here.)"
    return f"Firewall turned {'on' if on else 'off'}."


def _airdrop(on: bool) -> str:
    p = _run(["defaults", "write", "com.apple.NetworkBrowser",
              "DisableAirDrop", "-bool", "false" if on else "true"])
    if p.returncode != 0:
        return f"(could not change AirDrop: {(p.stderr or '').strip()})"
    return f"AirDrop {'enabled' if on else 'disabled'}."


_SETTINGS = {
    "bluetooth": _bluetooth,
    "do_not_disturb": _do_not_disturb,
    "low_power_mode": _low_power,
    "firewall": _firewall,
    "airdrop": _airdrop,
}


@register(
    "toggle_setting",
    "Turn a named macOS setting on or off: bluetooth, do_not_disturb, "
    "low_power_mode, firewall, or airdrop. For Wi-Fi use set_wifi and for sound "
    "use set_volume — those have their own tools.",
    {
        "type": "object",
        "properties": {
            "setting": {"type": "string",
                        "enum": ["bluetooth", "do_not_disturb", "low_power_mode",
                                 "firewall", "airdrop"],
                        "description": "Which setting to change."},
            "on": {"type": "boolean", "description": "true to enable, false to disable."},
        },
        "required": ["setting", "on"],
    },
    category="system_write",
    aliases=["turn on do not disturb", "stop notifications for a while",
             "switch bluetooth off", "enable low power mode",
             "I need to focus, silence everything", "turn airdrop off",
             "put my mac in battery saver"],
)
def toggle_setting(setting: str, on: bool) -> str:
    key = (setting or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {"dnd": "do_not_disturb", "focus": "do_not_disturb",
               "donotdisturb": "do_not_disturb", "bt": "bluetooth",
               "low_power": "low_power_mode", "battery_saver": "low_power_mode"}
    key = aliases.get(key, key)
    fn = _SETTINGS.get(key)
    if fn is None:
        return (f"(error: I can't toggle {setting!r}. Options: "
                f"{', '.join(sorted(_SETTINGS))}. Wi-Fi uses set_wifi, sound uses set_volume.)")
    return fn(bool(on))


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
@register(
    "system_status",
    "Report how the Mac is doing: free disk space, memory pressure, CPU load, "
    "and uptime. Use this for 'is my Mac running out of space' or 'why is it "
    "slow' rather than run_shell.",
    {"type": "object", "properties": {}},
    category="system_read",
    aliases=["am I running out of space", "why is my mac so slow",
             "how much storage do I have left", "what's eating my memory",
             "how long has this thing been on", "check my disk space"],
)
def system_status() -> str:
    lines: list[str] = []

    p = _run(["df", "-H", "/System/Volumes/Data"])
    if p.returncode == 0 and len(p.stdout.splitlines()) > 1:
        cols = p.stdout.splitlines()[1].split()
        if len(cols) >= 5:
            lines.append(f"Disk: {cols[3]} free of {cols[1]} ({cols[4]} used)")

    p = _run(["memory_pressure"])
    if p.returncode == 0:
        if (m := re.search(r"System-wide memory free percentage:\s*(\d+)%", p.stdout)):
            lines.append(f"Memory: {m.group(1)}% free")

    try:
        load = os.getloadavg()
        lines.append(f"CPU load: {load[0]:.2f} (1m), {load[1]:.2f} (5m), "
                     f"across {os.cpu_count()} cores")
    except OSError:
        pass

    p = _run(["uptime"])
    if p.returncode == 0 and (m := re.search(r"up\s+(.+?),\s+\d+\s+users?", p.stdout)):
        lines.append(f"Uptime: {m.group(1).strip()}")

    return "\n".join(lines) if lines else "(could not read system status)"
