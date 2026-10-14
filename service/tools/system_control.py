"""System-control tools: clipboard, plus battery, Bluetooth and Wi-Fi helpers.

All run directly from the Python backend via shell — unlike Mail/Calendar/
Messages, none of these need a per-app TCC grant (pbcopy/pbpaste, ioreg,
system_profiler and networksetup are all available to any process), so
there's no need to route them through the Swift app.
Registered on import.
"""
from __future__ import annotations

import json
import re
import subprocess

from service.tools.registry import register


def _run(argv: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=20, input=input_text)


# Keyboard backlight has no AppleScript "set volume"-style direct setter, and
# no plain shell command either. The classic workaround (simulate the
# hardware illumination keys as NX_SYSDEFINED HID events, or `hidutil
# property --set '{"KeyboardBacklight": ...}'`) was tried and tested live on
# this Mac (Mac17,8 / M5 Pro) and BOTH silently no-op: `ioreg -rn
# AppleHIDKeyboardEventDriverV2` exposes no KeyboardBacklight property at
# all, and `defaults find` turns up nothing backlight/illumination-related
# either. Confirmed why: since the M2-era function-row redesign, F5/F6 are
# Mic Mute/DND (not illumination), and Apple made keyboard backlight fully
# automatic (ambient light + typing activity) with no manual override
# exposed to software on this keyboard generation — this is a hardware/OS
# capability gap, not a missing command to keep guessing at.


@register(
    "clipboard_read",
    "Read the current contents of the macOS clipboard (text only).",
    {"type": "object", "properties": {}},
    category="system_read",
)
def clipboard_read() -> str:
    p = _run(["pbpaste"])
    if p.returncode != 0:
        return f"(could not read clipboard: {p.stderr.strip() or 'unknown error'})"
    text = p.stdout
    if not text.strip():
        return "(clipboard is empty)"
    return text if len(text) <= 4000 else text[:4000] + f"\n…[truncated {len(text)-4000} chars]"


@register(
    "clipboard_write",
    "Replace the macOS clipboard contents with the given text. Requires confirmation.",
    {"type": "object",
     "properties": {"text": {"type": "string"}},
     "required": ["text"]},
    category="system_write",
)
def clipboard_write(text: str) -> str:
    p = _run(["pbcopy"], input_text=text)
    if p.returncode != 0:
        return f"(could not write clipboard: {p.stderr.strip() or 'unknown error'})"
    return f"copied {len(text)} chars to the clipboard"


def battery_status() -> str:
    """Charge, power state, cycle count and design-capacity health (system_status section)."""
    p = _run(["ioreg", "-rn", "AppleSmartBattery"])
    if p.returncode != 0 or not p.stdout.strip():
        return "(no battery data — this Mac may not have a battery)"
    out = p.stdout

    def find_int(pattern: str) -> int | None:
        m = re.search(pattern, out)
        return int(m.group(1)) if m else None

    def find_bool(key: str) -> bool:
        return re.search(rf'"{key}"\s*=\s*Yes', out) is not None

    charge_pct = find_int(r'"CurrentCapacity"\s*=\s*(\d+)')
    if charge_pct is None:
        return "(could not parse battery info)"

    cycle_count = find_int(r'"CycleCount"\s*=\s*(\d+)')
    design_cap = find_int(r'"DesignCapacity"\s*=\s*(\d+)')
    full_charge_cap = find_int(r'"FullChargeCapacity"\s*=\s*(\d+)')
    time_remaining = find_int(r'"TimeRemaining"\s*=\s*(\d+)')
    is_charging = find_bool("IsCharging")
    external = find_bool("ExternalConnected")
    fully_charged = find_bool("FullyCharged")

    parts = [f"charge: {charge_pct}%"]
    if fully_charged:
        parts.append("fully charged")
    elif external:
        parts.append("charging" if is_charging else "plugged in, not charging")
    else:
        parts.append("on battery power")
        if time_remaining and time_remaining < 60000:
            h, m = divmod(time_remaining, 60)
            parts.append(f"~{h}h{m:02d}m remaining")

    if cycle_count is not None:
        parts.append(f"{cycle_count} charge cycles")

    if design_cap and full_charge_cap:
        health_pct = min(round(full_charge_cap / design_cap * 100), 100)
        condition = "Normal" if health_pct >= 80 else "Service Recommended"
        parts.append(f"battery health: {health_pct}% of original design capacity ({condition})")

    return "; ".join(parts)


def bluetooth_devices() -> str:
    """Connected Bluetooth devices and controller state (system_status section)."""
    # MEASURED FAILURE (2026-08-18): asked "what devices are on my bluetooth",
    # with no dedicated tool the model called get_volume and reported it could
    # not answer. `system_profiler SPBluetoothDataType -json` is fast (~50ms
    # measured) and needs nothing installed beyond macOS itself — no `blueutil`
    # dependency — so there was no real obstacle, just no tool.
    p = _run(["system_profiler", "SPBluetoothDataType", "-json"])
    if p.returncode != 0 or not p.stdout.strip():
        return "(could not read Bluetooth status)"
    try:
        data = json.loads(p.stdout)["SPBluetoothDataType"][0]
    except (json.JSONDecodeError, KeyError, IndexError):
        return "(could not parse Bluetooth status)"

    ctrl = data.get("controller_properties", {})
    state = ctrl.get("controller_state", "")
    on = state in ("attrib_on", "on") or state == ""  # some macOS versions omit this key when on
    lines = [f"Bluetooth: {'on' if on else 'off'}"]

    connected = data.get("device_connected") or []
    if not connected:
        lines.append("No devices currently connected.")
        return "\n".join(lines)

    lines.append(f"{len(connected)} device(s) connected:")
    for entry in connected:
        for name, info in entry.items():
            rssi = info.get("device_rssi")
            sig = f", signal {rssi} dBm" if rssi is not None else ""
            lines.append(f"  - {name}{sig}")
    return "\n".join(lines)


def wifi_power(on: bool) -> str:
    """Wi-Fi radio on/off (toggle_setting setting='wifi')."""
    # en0 is the Wi-Fi interface on virtually all Macs; networksetup -listallhardwareports
    # could confirm it, but adds a second subprocess for a case that basically never varies.
    p = _run(["networksetup", "-setairportpower", "en0", "on" if on else "off"])
    if p.returncode != 0:
        return f"(could not change Wi-Fi power: {p.stderr.strip() or 'unknown error'})"
    return f"Wi-Fi turned {'on' if on else 'off'}"


