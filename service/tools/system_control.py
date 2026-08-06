"""System-control tools: volume, clipboard, Wi-Fi, screen lock.

All run directly from the Python backend via osascript/shell — unlike Mail/
Calendar/Messages, none of these need a per-app TCC grant (System Events'
volume control, pbcopy/pbpaste, networksetup, and CGSession are all available
to any process), so there's no need to route them through the Swift app.
Registered on import.
"""
from __future__ import annotations

import json
import re
import subprocess

from service.tools.registry import register


def _osascript(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=20)


def _run(argv: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=20, input=input_text)


def _osascript_js(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-l", "JavaScript", "-e", script],
                          capture_output=True, text=True, timeout=20)


# Keyboard backlight has no AppleScript "set volume"-style direct setter, and
# no plain shell command either. The classic workaround (simulate the
# hardware illumination keys as NX_SYSDEFINED HID events, or `hidutil
# property --set '{"KeyboardBacklight": ...}'`) was tried and tested live on
# an Apple Silicon laptop and BOTH silently no-op: `ioreg -rn
# AppleHIDKeyboardEventDriverV2` exposes no KeyboardBacklight property at
# all, and `defaults find` turns up nothing backlight/illumination-related
# either. Confirmed why: since the M2-era function-row redesign, F5/F6 are
# Mic Mute/DND (not illumination), and Apple made keyboard backlight fully
# automatic (ambient light + typing activity) with no manual override
# exposed to software on this keyboard generation — this is a hardware/OS
# capability gap, not a missing command to keep guessing at.


@register(
    "get_volume",
    "Get the current system output volume (0-100) and whether audio is muted.",
    {"type": "object", "properties": {}},
    category="system_read",
)
def get_volume() -> str:
    # Extracting into variables first, then coercing `as string`, matters:
    # concatenating `output volume of (get volume settings)` directly with `&`
    # (no intermediate variable) doesn't cleanly coerce the integer — it comes
    # out as the record's own textual form ("6,  , false") instead of "6".
    p = _osascript('set vs to get volume settings\n'
                   'set v to output volume of vs\n'
                   'set m to output muted of vs\n'
                   'return (v as string) & " " & (m as string)')
    if p.returncode != 0:
        return f"(could not read volume: {p.stderr.strip() or 'unknown error'})"
    parts = p.stdout.strip().split()
    level = parts[0] if parts else "?"
    muted = parts[1] == "true" if len(parts) > 1 else False
    return f"volume {level}/100" + (" (muted)" if muted else "")


@register(
    "set_volume",
    "Set the system output volume. Pass 0 to effectively mute.",
    {"type": "object",
     "properties": {"level": {"type": "integer", "description": "0-100"}},
     "required": ["level"]},
    category="system_write",
)
def set_volume(level: int) -> str:
    level = max(0, min(100, int(level)))
    p = _osascript(f"set volume output volume {level}")
    if p.returncode != 0:
        return f"(could not set volume: {p.stderr.strip() or 'unknown error'})"
    return f"volume set to {level}/100"


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


@register(
    "get_battery_status",
    "Get real battery info: charge %, charging/plugged-in state, time remaining, "
    "charge cycle count, and battery HEALTH (current max capacity vs. original "
    "design capacity, as a percentage). Use this for ANY question about battery "
    "charge, degradation, wear, or whether the battery needs replacing. Do NOT "
    "use run_shell/pmset for this: `pmset -g log`'s 'BatteryHealth Warning "
    "level'/'cap:' lines are just low-charge UI-warning events tied to the "
    "charge percent at that moment (10 = 10% charge remaining, not 10% health) "
    "— they are not a wear/capacity metric, and reading them as one produces "
    "confidently wrong health/degradation claims.",
    {"type": "object", "properties": {}},
    category="system_read",
)
def get_battery_status() -> str:
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


@register(
    "set_keyboard_backlight",
    "Call this for ANY request to change keyboard lighting/backlight — do NOT "
    "use run_shell for this (there's no shell command, `defaults` key, or "
    "`hidutil property` that actually works; both were tried and confirmed "
    "to silently no-op on this hardware, not just guessed). This tool does "
    "NOT change the backlight — on this Mac (M2-and-later keyboard layout), "
    "keyboard backlight is fully automatic (ambient light + typing activity) "
    "with no manual software override, confirmed via ioreg and defaults. "
    "Tell the user this directly instead of claiming to have set it.",
    {"type": "object", "properties": {}},
    category="system_read",
)
def set_keyboard_backlight() -> str:
    return ("Keyboard backlight can't be set from here — on this Mac's "
            "keyboard (the M2-and-later layout, where F5/F6 are Mic Mute/DND "
            "instead of illumination), the backlight is fully automatic "
            "based on ambient light and typing activity, with no manual "
            "override exposed to software. This was confirmed, not assumed: "
            "no KeyboardBacklight service in ioreg, no related `defaults` "
            "key, and simulating the old illumination hotkeys has no effect.")


@register(
    "run_speed_test",
    "Run a real internet (Wi-Fi/Ethernet) speed test using macOS's built-in "
    "`networkQuality` tool — no install needed. Returns download/upload Mbps, "
    "latency, and responsiveness. Takes ~15-20 seconds and uses real bandwidth "
    "(several hundred MB), so it asks for confirmation first rather than "
    "running silently. Use this for ANY 'run a speed test' / 'check my "
    "internet speed' request — do NOT use run_shell/`speedtest` for this, "
    "that command isn't installed on this Mac and there is no built-in "
    "`speedtest` binary; `networkQuality` is the real, working equivalent.",
    {"type": "object", "properties": {}},
    category="network_active",
)
def run_speed_test() -> str:
    try:
        p = subprocess.run(["networkQuality", "-c", "-M", "30"],
                           capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return "(speed test timed out after 45s)"
    except FileNotFoundError:
        return "(networkQuality is not available on this Mac — needs macOS 12 Monterey or later)"
    if p.returncode != 0 or not p.stdout.strip():
        return f"(speed test failed: {p.stderr.strip() or 'no output'})"
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return "(could not parse speed test output)"

    dl_mbps = data.get("dl_throughput", 0) / 1_000_000
    ul_mbps = data.get("ul_throughput", 0) / 1_000_000
    rtt_ms = data.get("base_rtt")
    interface = next(iter(data.get("other", {}).get("interface-type", {})), None)

    parts = [f"download: {dl_mbps:.1f} Mbps", f"upload: {ul_mbps:.1f} Mbps"]
    if rtt_ms is not None:
        parts.append(f"latency: {rtt_ms:.0f} ms")
    if interface:
        parts.append(f"via {interface}")
    return "; ".join(parts)


@register(
    "set_wifi",
    "Turn the Mac's Wi-Fi on or off.",
    {"type": "object",
     "properties": {"on": {"type": "boolean", "description": "true to enable, false to disable"}},
     "required": ["on"]},
    category="system_write",
)
def set_wifi(on: bool) -> str:
    # en0 is the Wi-Fi interface on virtually all Macs; networksetup -listallhardwareports
    # could confirm it, but adds a second subprocess for a case that basically never varies.
    p = _run(["networksetup", "-setairportpower", "en0", "on" if on else "off"])
    if p.returncode != 0:
        return f"(could not change Wi-Fi power: {p.stderr.strip() or 'unknown error'})"
    return f"Wi-Fi turned {'on' if on else 'off'}"


@register(
    "lock_screen",
    "Lock the Mac's screen immediately.",
    {"type": "object", "properties": {}},
    category="system_write",
)
def lock_screen() -> str:
    p = _run(["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession",
              "-suspend"])
    if p.returncode != 0:
        return f"(could not lock screen: {p.stderr.strip() or 'unknown error'})"
    return "screen locked"
