"""Settings-driven setup of the local Google Workspace MCP server.

Gmail, Calendar and Drive reach Wisp through the existing MCP client
(service/mcp): this module only writes the `google_workspace` entry into
~/.moe/mcp.json, warms the first-run download, restarts the MCP manager, and
fetches the Google sign-in link from the server's own auth tool. The server is
`workspace-mcp` run by uvx as a local stdio subprocess, so mail and files go
Google -> this Mac and nothing is relayed through a third party.

The OAuth client id/secret are the user's own (Google Cloud "Desktop app").
They live in mcp.json, written 0600 like the rest of ~/.moe's personal files.
Tools keep the MCP client's normal rule: confirm-tier unless the user lists them
in `trusted_read_only`; only plainly read-only lookups are pre-listed here.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

from service.mcp import CONFIG_PATH, manager

SERVER = "google_workspace"
TIERS = ("core", "extended", "complete")
# Read-only lookups safe to run unprompted. Honoured only while the server still
# annotates them read-only (see service.mcp._category_for), so a wrong name here
# just means Wisp asks.
TRUSTED_READ_ONLY = [
    "search_gmail_messages", "get_gmail_message_content", "get_gmail_messages_content_batch",
    "get_gmail_thread_content", "list_gmail_labels", "list_calendars", "get_events",
    "search_drive_files", "list_drive_items", "get_drive_file_content",
]
_UVX_FALLBACKS = ("~/.local/bin/uvx", "/opt/homebrew/bin/uvx", "/usr/local/bin/uvx")
_AUTH_URL = re.compile(r"https://accounts\.google\.com/[^\s)>\]\"']+")

_prepare = {"state": "idle", "detail": ""}      # idle | preparing | ready | failed


def find_uvx() -> str:
    """Absolute path to uvx — a GUI-launched backend's PATH usually lacks ~/.local/bin."""
    found = shutil.which("uvx")
    if found:
        return found
    for candidate in _UVX_FALLBACKS:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return ""


def _read_file() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — absent/malformed starts a fresh file
        return {}


def _write_file(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    tmp.replace(CONFIG_PATH)


def _entry() -> dict:
    servers = _read_file().get("servers")
    entry = servers.get(SERVER) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else {}


async def _warm(uvx: str) -> None:
    """First `uvx workspace-mcp` downloads its packages, longer than the MCP
    client's 20s handshake budget, so fetch them before the server is started."""
    _prepare.update(state="preparing", detail="Downloading the Google Workspace server…")
    try:
        proc = await asyncio.create_subprocess_exec(
            uvx, "workspace-mcp", "--help",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), 300)
        if proc.returncode:
            raise RuntimeError(err.decode(errors="replace").strip().splitlines()[-1][:200]
                               if err.strip() else f"exit {proc.returncode}")
        _prepare.update(state="ready", detail="")
    except Exception as exc:  # noqa: BLE001
        _prepare.update(state="failed", detail=f"Couldn't fetch workspace-mcp: {exc}"[:240])
        return
    await manager.reload()


async def enable(client_id: str, client_secret: str, email: str, tier: str = "core") -> dict:
    uvx = find_uvx()
    if not uvx:
        return {"ok": False, "error": "uv isn't installed (needed to run the Google Workspace "
                                      "server). Install it with: brew install uv"}
    client_id = client_id.strip()
    if not client_id:
        return {"ok": False, "error": "client_id is required"}
    if tier not in TIERS:
        tier = "core"
    data = _read_file()
    servers = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    previous = _entry().get("env") or {}
    secret = client_secret.strip() or previous.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    env = {"GOOGLE_OAUTH_CLIENT_ID": client_id}
    if secret:
        env["GOOGLE_OAUTH_CLIENT_SECRET"] = secret
    if email.strip():
        env["USER_GOOGLE_EMAIL"] = email.strip()
    servers[SERVER] = {"command": uvx, "args": ["workspace-mcp", "--tool-tier", tier],
                       "env": env, "enabled": True,
                       "trusted_read_only": TRUSTED_READ_ONLY}
    data["servers"] = servers
    _write_file(data)
    asyncio.create_task(_warm(uvx))
    return {"ok": True, "state": "preparing"}


async def disable() -> dict:
    data = _read_file()
    if isinstance(data.get("servers"), dict) and SERVER in data["servers"]:
        del data["servers"][SERVER]
        _write_file(data)
    _prepare.update(state="idle", detail="")
    await manager.reload()
    return {"ok": True}


def status() -> dict:
    entry = _entry()
    env = entry.get("env") or {}
    live = next((s for s in manager.status()["servers"] if s["name"] == SERVER), None)
    return {"configured": bool(entry), "email": env.get("USER_GOOGLE_EMAIL", ""),
            "client_id": env.get("GOOGLE_OAUTH_CLIENT_ID", ""),
            "has_secret": bool(env.get("GOOGLE_OAUTH_CLIENT_SECRET")),
            "uvx": bool(find_uvx()),
            "prepare": dict(_prepare),
            "running": bool(live and live["running"]),
            "error": (live or {}).get("error", ""),
            "tools": len((live or {}).get("tools", []))}


def _auth_arguments(schema: dict, email: str) -> dict:
    args: dict = {}
    for prop in (schema or {}).get("properties", {}):
        low = prop.lower()
        if "email" in low:
            args[prop] = email
        elif "service" in low:
            args[prop] = "gmail"
    return args


async def signin(email: str) -> dict:
    """Ask the server for its Google consent link (it also serves the callback)."""
    server = manager.servers.get(SERVER)
    if not server or not server.running:
        return {"ok": False, "error": "The Google Workspace server isn't running yet."}
    spec = next((t for t in server.tools if t.get("name") == "start_google_auth"), None)
    if not spec:
        return {"ok": False, "error": "This server version has no sign-in tool; "
                                      "ask Wisp to check your Gmail and it will prompt you."}
    email = email.strip() or (_entry().get("env") or {}).get("USER_GOOGLE_EMAIL", "")
    if not email:
        return {"ok": False, "error": "Enter your Google email first."}
    try:
        text = await server.call_tool("start_google_auth",
                                      _auth_arguments(spec.get("inputSchema") or {}, email))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    match = _AUTH_URL.search(text)
    if not match:
        return {"ok": False, "error": text[:240] or "No sign-in link returned."}
    return {"ok": True, "auth_url": match.group(0)}
