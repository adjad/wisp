"""Helpers behind system_extras' merged tools: appearance and VoiceOver
(toggle_setting) and network details (system_status). Registers no tools.
"""
from __future__ import annotations

import re
import subprocess

_TIMEOUT = 20


def _run(argv: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _osa(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], capture_output=True,
                          text=True, timeout=_TIMEOUT)


def appearance_mode(mode: str) -> str:
    """Light, dark, or automatic appearance (toggle_setting dark_mode/auto_appearance)."""
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


def network_details() -> str:
    """Local/public IP and connection type (system_status section; public IP is a network call)."""
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


def voiceover_power(feature: str, on: bool) -> str:
    """VoiceOver on/off (toggle_setting setting='voiceover')."""
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
