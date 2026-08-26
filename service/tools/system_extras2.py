"""More system control: appearance, Wi-Fi network selection, power, updates,
network diagnostics, login items, uninstalling, screen lock timing.

Split from system_extras.py (Phase 1's display/capture/toggle/status module)
rather than added to it, purely for file size — nothing here shares state with
that module.
"""
from __future__ import annotations

import re
import subprocess

from service.tools.registry import register

_TIMEOUT = 20


def _run(argv: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _osa(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], capture_output=True,
                          text=True, timeout=_TIMEOUT)


@register(
    "set_appearance",
    "Switch macOS appearance between Light, Dark, or follow the system "
    "schedule (auto). For a one-off toggle use system_extras.set_display's "
    "dark_mode instead — this is for setting the actual MODE, including auto.",
    {"type": "object",
     "properties": {"mode": {"type": "string", "enum": ["light", "dark", "auto"]}},
     "required": ["mode"]},
    category="system_write",
    aliases=["switch to light mode", "put me on auto appearance",
             "match my mac's theme to the time of day"],
)
def set_appearance(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m not in ("light", "dark", "auto"):
        return f"(error: unknown mode {m!r}. Use light, dark, or auto.)"
    if m == "auto":
        script = ('tell application "System Events" to tell appearance preferences '
                  'to set dark mode to true\n'
                  'do shell script "defaults write NSGlobalDomain '
                  'AppleInterfaceStyleSwitchesAutomatically -bool true"')
    else:
        script = (
            f'do shell script "defaults delete NSGlobalDomain '
            f'AppleInterfaceStyleSwitchesAutomatically" 2>/dev/null\n'
            f'tell application "System Events" to tell appearance preferences '
            f'to set dark mode to {"true" if m == "dark" else "false"}')
    p = _osa(script)
    if p.returncode != 0:
        return f"(could not change appearance: {(p.stderr or '').strip()})"
    return f"Appearance set to {m}."


@register(
    "connect_wifi",
    "Join a specific Wi-Fi network by name. For just turning Wi-Fi on/off use "
    "set_wifi instead.",
    {"type": "object",
     "properties": {
         "network": {"type": "string", "description": "The Wi-Fi network (SSID) name."},
         "password": {"type": "string", "description": "The network password, if it needs one."},
     },
     "required": ["network"]},
    category="system_write",
    aliases=["connect to my home wifi", "join the network called office",
             "switch to a different wifi network"],
)
def connect_wifi(network: str, password: str = "") -> str:
    ssid = (network or "").strip()
    if not ssid:
        return "(error: connect_wifi needs a `network` name.)"
    dev = _run(["networksetup", "-listallhardwareports"])
    iface = "en0"
    lines = dev.stdout.splitlines()
    for i, line in enumerate(lines):
        if "Wi-Fi" in line and i + 1 < len(lines) and "Device:" in lines[i + 1]:
            iface = lines[i + 1].split(":")[-1].strip()
            break
    argv = ["networksetup", "-setairportnetwork", iface, ssid]
    if password:
        argv.append(password)
    p = _run(argv)
    if p.returncode != 0 or "not found" in (p.stdout + p.stderr).lower():
        return (f"(could not join {ssid!r}: {(p.stderr or p.stdout).strip() or 'network not found'}. "
                f"Check the name and password.)")
    return f"Joined {ssid}."


@register(
    "power_control",
    "Restart, shut down, sleep, or log out of this Mac. ALWAYS confirmed "
    "before running (the policy engine gates this) — closes unsaved work, so "
    "the user should be certain.",
    {"type": "object",
     "properties": {"action": {"type": "string",
                                "enum": ["restart", "shutdown", "sleep", "logout"]}},
     "required": ["action"]},
    category="system_write",
    aliases=["restart my mac", "shut down the computer", "put my mac to sleep",
             "log me out"],
)
def power_control(action: str) -> str:
    verbs = {"restart": "restart", "shutdown": "shut down",
             "sleep": "sleep", "logout": "log out"}
    a = (action or "").strip().lower()
    if a not in verbs:
        return f"(error: unknown action {a!r}. Use restart, shutdown, sleep, or logout.)"
    script = f'tell application "System Events" to {verbs[a]}'
    p = _osa(script)
    if p.returncode != 0:
        return f"(could not {verbs[a]}: {(p.stderr or '').strip()})"
    return f"{verbs[a].capitalize()} initiated."


@register(
    "software_update",
    "Check for macOS software updates, or install pending ones. Checking is "
    "read-only; installing needs `confirm=true` and may restart the Mac.",
    {"type": "object",
     "properties": {
         "action": {"type": "string", "enum": ["check", "install"], "description": "Default check."},
         "confirm": {"type": "boolean", "description": "Required true to actually install."},
     }},
    category="system_write",
    aliases=["check for software updates", "is my mac up to date",
             "install the pending update", "update macos"],
)
def software_update(action: str = "check", confirm: bool = False) -> str:
    a = (action or "check").strip().lower()
    if a == "check":
        p = _run(["softwareupdate", "-l"], timeout=60)
        out = (p.stdout + p.stderr).strip()
        if "no new software" in out.lower():
            return "No updates available — you're up to date."
        return out or "(couldn't check for updates.)"
    if a == "install":
        if not confirm:
            return ("(error: installing updates needs confirm=true — it may "
                    "restart the Mac. Check first with action='check'.)")
        p = _run(["softwareupdate", "-i", "-a"], timeout=1800)
        return (p.stdout + p.stderr).strip() or "(update process finished with no output.)"
    return f"(error: unknown action {a!r}. Use check or install.)"


@register(
    "network_info",
    "Report current network details: local IP, public IP, Wi-Fi network "
    "name, and whether the connection is Wi-Fi or Ethernet.",
    {"type": "object", "properties": {}},
    category="system_read",
    aliases=["what's my ip address", "what wifi network am I on",
             "am I connected to the internet", "what's my network setup"],
)
def network_info() -> str:
    lines = []
    p = _run(["ipconfig", "getifaddr", "en0"])
    if p.returncode == 0 and p.stdout.strip():
        lines.append(f"Local IP (Wi-Fi): {p.stdout.strip()}")
    ssid_p = _run(["networksetup", "-getairportnetwork", "en0"])
    if ":" in ssid_p.stdout:
        lines.append(f"Wi-Fi network: {ssid_p.stdout.split(':', 1)[-1].strip()}")
    try:
        import httpx
        r = httpx.get("https://api.ipify.org", timeout=6)
        if r.status_code == 200:
            lines.append(f"Public IP: {r.text.strip()}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines) if lines else "(couldn't determine network info.)"


@register(
    "manage_login_items",
    "List, add, or remove apps that launch automatically at login.",
    {"type": "object",
     "properties": {
         "action": {"type": "string", "enum": ["list", "add", "remove"]},
         "app_path": {"type": "string", "description": "Full path to the app, for add/remove."},
     },
     "required": ["action"]},
    category="system_write",
    aliases=["what opens when I log in", "add this app to startup",
             "stop this app from opening automatically", "what's in my login items"],
)
def manage_login_items(action: str, app_path: str = "") -> str:
    a = (action or "").strip().lower()
    if a == "list":
        p = _osa('tell application "System Events" to get the name of every login item')
        if p.returncode != 0:
            return f"(could not list login items: {(p.stderr or '').strip()})"
        items = [n.strip() for n in p.stdout.split(",") if n.strip()]
        return "Login items: " + (", ".join(items) if items else "(none)")
    if a in ("add", "remove"):
        if not app_path.strip():
            return f"(error: {a} needs `app_path`.)"
        if a == "add":
            script = f'tell application "System Events" to make login item at end with properties {{path:"{app_path}", hidden:false}}'
        else:
            script = f'tell application "System Events" to delete login item "{app_path.split("/")[-1].replace(".app", "")}"'
        p = _osa(script)
        if p.returncode != 0:
            return f"(could not {a} login item: {(p.stderr or '').strip()})"
        return f"{'Added' if a == 'add' else 'Removed'} login item."
    return f"(error: unknown action {a!r}. Use list, add, or remove.)"


@register(
    "uninstall_app",
    "Move an application to the Trash (uninstall). Only removes the .app "
    "bundle from /Applications — does not clean up preference files or "
    "support data (macOS has no built-in clean-uninstall for that).",
    {"type": "object",
     "properties": {"name": {"type": "string", "description": "The app's name, e.g. 'OldApp'."}},
     "required": ["name"]},
    category="fs_write",
    aliases=["uninstall this app", "get rid of this application",
             "remove this app from my mac"],
)
def uninstall_app(name: str) -> str:
    from pathlib import Path

    app_name = name.strip()
    if not app_name.endswith(".app"):
        app_name += ".app"
    for base in ("/Applications", str(Path.home() / "Applications")):
        p = Path(base) / app_name
        if p.exists():
            script = f'tell application "Finder" to delete POSIX file "{p}"'
            result = _osa(script)
            if result.returncode != 0:
                return f"(could not remove {app_name}: {(result.stderr or '').strip()})"
            return (f"Moved {app_name} to Trash. Preference files and support "
                    f"data (if any) were left behind — macOS has no built-in "
                    f"way to clean those up.")
    return f"(couldn't find {app_name} in /Applications.)"


@register(
    "set_screen_lock_timeout",
    "Set how long before the screen locks after going idle, in minutes. "
    "0 means never lock.",
    {"type": "object",
     "properties": {"minutes": {"type": "integer", "description": "Idle minutes before lock. 0 = never."}},
     "required": ["minutes"]},
    category="system_write",
    aliases=["lock my screen after 5 minutes", "never lock my screen automatically",
             "how long before my mac locks"],
)
def set_screen_lock_timeout(minutes: int) -> str:
    try:
        mins = max(0, int(minutes))
    except (TypeError, ValueError):
        return f"(error: {minutes!r} isn't a number of minutes.)"
    secs = mins * 60
    p = _run(["sysadminctl", "-screenLock", "off" if mins == 0 else str(secs)])
    if p.returncode != 0:
        return (f"(could not change screen lock timing — this usually needs an "
                f"admin password, so it can't be set from here: "
                f"{(p.stderr or '').strip()})")
    return (f"Screen lock set to never." if mins == 0
            else f"Screen lock set to {mins} minute(s) after idle.")


@register(
    "accessibility_toggle",
    "Turn VoiceOver on or off — Wisp's only reliable accessibility toggle. "
    "Other accessibility features (Zoom, Reduce Motion) have no stable "
    "scriptable control on modern macOS and need System Settings directly; "
    "this says so rather than risking a `defaults write` that leaves the "
    "system in a broken visual state.",
    {"type": "object",
     "properties": {"feature": {"type": "string", "enum": ["voiceover"]},
                     "on": {"type": "boolean"}},
     "required": ["feature", "on"]},
    category="system_write",
    aliases=["turn on voiceover", "turn off voiceover",
             "enable screen reader", "disable voiceover"],
)
def accessibility_toggle(feature: str, on: bool) -> str:
    f = (feature or "").strip().lower()
    if f != "voiceover":
        return (f"(error: only 'voiceover' can be toggled reliably here. Zoom, "
                f"Reduce Motion, and similar have no stable scriptable control "
                f"on modern macOS — use System Settings > Accessibility "
                f"directly for those.)")
    if on:
        p = _run(["open", "-a", "VoiceOver"])
        if p.returncode != 0:
            return f"(could not start VoiceOver: {p.stderr.strip()})"
        return "VoiceOver turned on."
    p = _run(["killall", "VoiceOver"])
    if p.returncode not in (0, 1):
        return f"(could not stop VoiceOver: {p.stderr.strip()})"
    return "VoiceOver turned off." if p.returncode == 0 else "VoiceOver wasn't running."
