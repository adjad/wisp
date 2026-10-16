"""Display, capture, and the consolidated setting toggles.

A NOTE ON `toggle_setting`
--------------------------
Seven Capability Atlas rows (Bluetooth, AirDrop, Low Power Mode, hotspot, VPN,
firewall, Do Not Disturb) are the same shape: one named boolean, on or off. They
are ONE tool with an enum rather than seven near-identical schemas, and the
reason is retrieval accuracy, not tidiness — seven schemas differing only in a
noun are exactly the case a small model picks wrong from, and each one costs
~200 tokens of every menu it appears in. Wi-Fi power, VoiceOver and dark/auto
appearance joined the same enum in 2026-10. Wisp has no volume tool (set_volume
and get_volume were retired in 2026-10).

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


def _wifi(on: bool) -> str:
    from service.tools.system_control import wifi_power
    return wifi_power(on)


def _voiceover(on: bool) -> str:
    from service.tools.system_extras2 import voiceover_power
    return voiceover_power("voiceover", on)


def _dark_mode(on: bool) -> str:
    from service.tools.system_extras2 import appearance_mode
    return appearance_mode("dark" if on else "light")


def _auto_appearance(on: bool) -> str:
    from service.tools.system_extras2 import appearance_mode
    if on:
        return appearance_mode("auto")
    # Leave automatic switching while keeping whatever look is showing now.
    p = _run(["defaults", "read", "-g", "AppleInterfaceStyle"])
    return appearance_mode("dark" if p.returncode == 0 and "Dark" in p.stdout else "light")


_SETTINGS = {
    "wifi": _wifi,
    "bluetooth": _bluetooth,
    "do_not_disturb": _do_not_disturb,
    "low_power_mode": _low_power,
    "firewall": _firewall,
    "airdrop": _airdrop,
    "voiceover": _voiceover,
    "dark_mode": _dark_mode,
    "auto_appearance": _auto_appearance,
}


_SETTING_ALIASES = {"dnd": "do_not_disturb", "focus": "do_not_disturb",
                    "donotdisturb": "do_not_disturb", "bt": "bluetooth",
                    "low_power": "low_power_mode", "battery_saver": "low_power_mode",
                    "wi_fi": "wifi", "screen_reader": "voiceover", "dark": "dark_mode",
                    "auto": "auto_appearance"}


def setting_key(setting: str) -> str:
    """Canonical _SETTINGS key for a spoken setting name (also the grant scope)."""
    key = (setting or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _SETTING_ALIASES.get(key, key)


@register(
    "toggle_setting",
    "Turn a named macOS setting on or off: wifi, bluetooth, do_not_disturb, "
    "low_power_mode, firewall, airdrop, voiceover, dark_mode, or auto_appearance "
    "(follow the system light/dark schedule). It cannot change the volume.",
    {
        "type": "object",
        "properties": {
            "setting": {"type": "string",
                        "enum": ["wifi", "bluetooth", "do_not_disturb", "low_power_mode",
                                 "firewall", "airdrop", "voiceover", "dark_mode",
                                 "auto_appearance"],
                        "description": "Which setting to change."},
            "on": {"type": "boolean", "description": "true to enable, false to disable."},
        },
        "required": ["setting", "on"],
    },
    category="system_write",
    aliases=["turn on do not disturb", "stop notifications for a while",
             "switch bluetooth off", "enable low power mode",
             "I need to focus, silence everything", "turn airdrop off",
             "put my mac in battery saver", "turn wifi off", "get me back on wifi",
             "turn on voiceover", "enable screen reader", "switch to dark mode",
             "switch to light mode", "match my mac's theme to the time of day"],
)
def toggle_setting(setting: str, on: bool) -> str:
    key = setting_key(setting)
    fn = _SETTINGS.get(key)
    if fn is None:
        return (f"(error: I can't toggle {setting!r}. Options: "
                f"{', '.join(sorted(_SETTINGS))}.)")
    return fn(bool(on))


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
@register(
    "system_status",
    "Report how the Mac is doing. section='overview' (default): battery plus free "
    "disk, memory pressure, CPU load and uptime. section='battery': charge %, "
    "charging state, time remaining, cycle count and battery HEALTH (max capacity "
    "vs. design capacity) — use it for ANY battery question, not run_shell/pmset "
    "(`pmset -g log` 'BatteryHealth Warning level'/'cap:' lines are low-charge "
    "UI events, NOT a wear metric). section='network': local/public IP, Wi-Fi "
    "network name, Wi-Fi vs Ethernet. section='bluetooth': connected Bluetooth "
    "devices and whether Bluetooth is on (read-only; toggle_setting turns it on/off).",
    {"type": "object", "properties": {
        "section": {"type": "string",
                    "enum": ["overview", "battery", "network", "bluetooth"],
                    "description": "What to report. Defaults to overview."},
    }},
    category="system_read",
    aliases=["am I running out of space", "why is my mac so slow",
             "how much storage do I have left", "what's eating my memory",
             "how long has this thing been on", "check my disk space",
             "what's my ip address", "what wifi network am I on",
             "am I connected to the internet", "what's connected over bluetooth",
             "are my airpods connected", "how's my battery health",
             "how much battery do I have left", "am I plugged in"],
)
def system_status(section: str = "overview") -> str:
    section = (section or "overview").strip().lower()
    if section == "battery":
        from service.tools.system_control import battery_status
        return battery_status()
    if section == "network":
        from service.tools.system_extras2 import network_details
        return network_details()
    if section == "bluetooth":
        from service.tools.system_control import bluetooth_devices
        return bluetooth_devices()
    if section != "overview":
        return f"(error: unknown section {section!r}. Use overview, battery, network, or bluetooth.)"
    from service.tools.system_control import battery_status
    # Battery first: the conditional low-power route reads the first "N%".
    return f"Battery: {battery_status()}\n{_resources()}"


def _resources() -> str:
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
