"""Settings-driven setup of the local Google Workspace MCP server.

Gmail, Calendar and Drive reach Wisp through the existing MCP client
(service/mcp): this module only writes the `google_workspace` entry into
~/.moe/mcp.json, warms the first-run download, restarts the MCP manager, and
fetches the Google sign-in link from the server's own auth tool. The server is
`workspace-mcp`, pinned to one version (PACKAGE), run by uvx as a local stdio
subprocess, so mail and files go Google -> this Mac and nothing is relayed
through a third party. The app bundles its own uvx (find_uvx prefers it) and
runs it with Wisp-owned uv dirs (uv_env), never the user's own uv state; the
first enable downloads workspace-mcp from PyPI.

The OAuth client is the one Google sign-in used (gmail_source.resolve_client:
the user's own Desktop client, else the build's built-in one); the first
connect sets the server up with it (gmail_source._ensure_workspace). It lives
in mcp.json, written 0600 like the rest of ~/.moe's personal files.
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
from service.paths import MOE_DIR

SERVER = "google_workspace"
# Pinned, never "latest": the server is handed the user's OAuth client and the
# refresh tokens of every account switched on, so a new release is taken
# deliberately. 2.2.0 is the newest release uv had resolved locally during
# 0.5.0 dev1 testing; it has --tool-tier, start_google_auth and
# WORKSPACE_MCP_CREDENTIALS_DIR, which this module relies on.
PACKAGE = "workspace-mcp==2.2.0"
# workspace-mcp's own dependencies float, so uvx resolves them as of a fixed
# date (every first connect installs the server). DEPENDENCY_CUTOFF is at least
# two weeks older than the pin was set; PACKAGE itself was published after it
# (2026-10-23), so it alone gets a later cutoff. The Python is fixed as well:
# the tree was resolved for 3.12 (workspace-mcp needs >=3.10).
DEPENDENCY_CUTOFF = "2026-10-12T00:00:00Z"
PACKAGE_CUTOFF = "workspace-mcp=2026-10-24T00:00:00Z"
SERVER_PYTHON = "3.12"
_SERVER_ARGS = ("--exclude-newer", DEPENDENCY_CUTOFF, "--exclude-newer-package", PACKAGE_CUTOFF,
                "--python", SERVER_PYTHON, "--from", PACKAGE, "workspace-mcp")
# uvx options Wisp writes before the server name; _pinned_args replaces them.
_UVX_OPTIONS = ("--exclude-newer", "--exclude-newer-package", "--python", "--from")
# Which client the server entry was set up with (mcp.json key next to "env"):
# "builtin" (the client this build ships) or "user". A built-in id is never
# taken for the user's own, even after a new build ships a different one.
CLIENT_SOURCE_KEY = "wisp_client_source"
TIERS = ("core", "extended", "complete")
# Read-only lookups safe to run unprompted. Honoured only while the server still
# annotates them read-only (see service.mcp._category_for), so a wrong name here
# just means Wisp asks.
TRUSTED_READ_ONLY = [
    "search_gmail_messages", "get_gmail_message_content", "get_gmail_messages_content_batch",
    "get_gmail_thread_content", "list_gmail_labels", "list_calendars", "get_events",
    "search_drive_files", "list_drive_items", "get_drive_file_content",
    "get_doc_content", "read_sheet_values", "get_presentation", "get_form",
    "get_messages", "search_messages", "list_spaces",
    "list_tasks", "get_task", "list_task_lists",
    "list_contacts", "get_contact", "search_contacts",
]
# The server's per-account sign-ins. Wisp owns this folder: every connected
# account (each sign-in switches Workspace on) gets a credential file here,
# written from Wisp's own Google sign-in.
CREDENTIALS_DIR = MOE_DIR / "google_workspace" / "credentials"
TOKEN_URI = "https://oauth2.googleapis.com/token"
# Read-only Workspace scopes, requested at every sign-in together with mail and
# calendar (gmail_source._consent_scopes). Writes (sending, editing files) stay
# with the server's own on-demand sign-in.
WORKSPACE_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
)
FULL_SCOPE = WORKSPACE_SCOPES[0]
_UVX_FALLBACKS = ("~/.local/bin/uvx", "/opt/homebrew/bin/uvx", "/usr/local/bin/uvx")
# The uvx the app bundle ships (build-support/uv_tool.py writes
# Contents/Resources/bin/uv and uvx, signed with the app). The backend runs
# from Contents/Resources/backend, so it is two levels above this package.
_BACKEND = Path(__file__).resolve().parents[1]
BUNDLED_UVX = (_BACKEND.parent / "bin" / "uvx"
               if _BACKEND.name == "backend" and _BACKEND.parent.name == "Resources" else None)
# Wisp's own uv state, so running workspace-mcp never touches the user's uv
# cache, tools or managed Pythons (uvx may fetch a Python for the server).
UV_HOME = MOE_DIR / "uv"


def uv_env() -> dict[str, str]:
    """The only UV_* settings uvx sees: Wisp's dirs, and no uv.toml or
    pyproject config from the user (UV_NO_CONFIG)."""
    return {"UV_CACHE_DIR": str(UV_HOME / "cache"), "UV_TOOL_DIR": str(UV_HOME / "tools"),
            "UV_PYTHON_INSTALL_DIR": str(UV_HOME / "python"), "UV_NO_CONFIG": "1"}


def uv_process_env(base: dict | None = None) -> dict[str, str]:
    """`base` (default: this process's environment) without any inherited
    UV_* variable, plus uv_env()."""
    base = os.environ if base is None else base
    return {**{k: v for k, v in base.items() if not k.startswith("UV_")}, **uv_env()}
_AUTH_URL = re.compile(r"https://accounts\.google\.com/[^\s)>\]\"']+")

_prepare = {"state": "idle", "detail": ""}      # idle | preparing | ready | failed


def _executable(path: Path | None) -> bool:
    return bool(path) and path.is_file() and os.access(path, os.X_OK)


def find_uvx() -> str:
    """Absolute path to uvx: the one bundled with Wisp first, then PATH, then
    the usual install places (a GUI-launched backend's PATH usually lacks
    ~/.local/bin)."""
    if _executable(BUNDLED_UVX):
        return str(BUNDLED_UVX)
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
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, indent=2)
        tmp.replace(CONFIG_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _entry() -> dict:
    servers = _read_file().get("servers")
    entry = servers.get(SERVER) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else {}


async def _warm(uvx: str) -> None:
    """The first run of the pinned server downloads its packages, longer than the
    MCP client's 20s handshake budget, so fetch them before it is started."""
    _prepare.update(state="preparing", detail="Downloading the Google Workspace server…")
    try:
        proc = await asyncio.create_subprocess_exec(
            uvx, *_SERVER_ARGS, "--help", env=uv_process_env(),
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


async def enable(client_id: str, client_secret: str, email: str, tier: str = "core",
                 client_source: str = "user") -> dict:
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
    same_client = previous.get("GOOGLE_OAUTH_CLIENT_ID") == client_id
    secret = client_secret.strip() or (previous.get("GOOGLE_OAUTH_CLIENT_SECRET", "") if same_client else "")
    if not secret:
        return {"ok": False, "error": "Enter the client secret that belongs to this OAuth client ID. Saved settings were kept."}
    env = {"GOOGLE_OAUTH_CLIENT_ID": client_id, "GOOGLE_OAUTH_CLIENT_SECRET": secret,
           "WORKSPACE_MCP_CREDENTIALS_DIR": str(CREDENTIALS_DIR), **uv_env()}
    servers[SERVER] = {"command": uvx, "args": [*_SERVER_ARGS, "--tool-tier", tier],
                       "env": env, "enabled": True,
                       # The MCP client drops inherited UV_* before applying env.
                       "env_clear_prefixes": ["UV_"],
                       CLIENT_SOURCE_KEY: "builtin" if client_source == "builtin" else "user",
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
    try:
        accounts = enabled_accounts()
    except Exception:  # noqa: BLE001
        accounts = []
    client_id = env.get("GOOGLE_OAUTH_CLIENT_ID", "")
    builtin = _entry_is_builtin(entry)
    return {"configured": bool(entry), "email": accounts[0] if accounts else "",
            "accounts": accounts,
            # Only the user's own client id is shown; the built-in one is named
            # by its source. No secret is ever returned.
            "client_id": "" if builtin else client_id,
            "client_source": "builtin" if builtin else "user" if client_id else "none",
            "has_secret": bool(env.get("GOOGLE_OAUTH_CLIENT_SECRET")),
            "uvx": bool(find_uvx()),
            "prepare": dict(_prepare),
            "running": bool(live and live["running"]),
            "error": (live or {}).get("error", ""),
            "tools": len((live or {}).get("tools", []))}


def _entry_is_builtin(entry: dict) -> bool:
    """Set up with the built-in client: by its tag, or (an entry written
    before the tag) by matching the client this build ships."""
    tag = entry.get(CLIENT_SOURCE_KEY)
    if tag in ("builtin", "user"):
        return tag == "builtin"
    client_id = (entry.get("env") or {}).get("GOOGLE_OAUTH_CLIENT_ID", "")
    from service import gmail_source
    return bool(client_id) and client_id == gmail_source.builtin_client()[0]


def saved_credentials() -> tuple[str, str]:
    """(client_id, client_secret) of the user's own client saved by the
    Workspace setup, so the two Google paths share one OAuth client entry
    instead of asking twice. ("", "") when it was set up with the built-in
    client, so a built-in id is never mistaken for the user's own."""
    entry = _entry()
    if _entry_is_builtin(entry):
        return "", ""
    env = entry.get("env") or {}
    return env.get("GOOGLE_OAUTH_CLIENT_ID", ""), env.get("GOOGLE_OAUTH_CLIENT_SECRET", "")


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


# --- accounts: which connected Google accounts the Workspace tools may use ------

def _pinned_args(args) -> list[str]:
    """An entry's arguments with the server pinned to PACKAGE: an unpinned
    `workspace-mcp …` (or another version) keeps only its own flags."""
    args = [str(a) for a in args] if isinstance(args, list) else []
    while len(args) >= 2 and args[0] in _UVX_OPTIONS:
        args = args[2:]
    if args[:1] and args[0].split("@")[0].split("==")[0] == "workspace-mcp":
        args = args[1:]
    return [*_SERVER_ARGS, *(args or ["--tool-tier", "core"])]


def ensure_env() -> bool:
    """Point an existing server entry at Wisp's credentials folder, drop a
    fixed USER_GOOGLE_EMAIL (the account now comes from the user's choice), and
    pin an entry written before PACKAGE was. Wisp's own uvx entry also gets
    Wisp's uv dirs and, when the app bundles uvx, that uvx (an entry written
    from PATH or a moved app keeps working). True when mcp.json changed and
    the server needs a reload."""
    data = _read_file()
    entry = (data.get("servers") or {}).get(SERVER) if isinstance(data.get("servers"), dict) else None
    if not isinstance(entry, dict):
        return False
    own = Path(str(entry.get("command") or "uvx")).name == "uvx"
    env = dict(entry.get("env") or {})
    wanted = {**env, "WORKSPACE_MCP_CREDENTIALS_DIR": str(CREDENTIALS_DIR), **(uv_env() if own else {})}
    wanted.pop("USER_GOOGLE_EMAIL", None)
    listed = entry.get("trusted_read_only")
    trusted = (sorted(set(listed) | set(TRUSTED_READ_ONLY))
               if isinstance(listed, list) and all(isinstance(n, str) for n in listed)
               else list(TRUSTED_READ_ONLY))
    # Only Wisp's own uvx entry is re-pinned; a hand-written one is left alone.
    pinned = _pinned_args(entry.get("args")) if own else entry.get("args")
    command = str(BUNDLED_UVX) if own and _executable(BUNDLED_UVX) else entry.get("command")
    clear = ["UV_"] if own else entry.get("env_clear_prefixes")
    if (wanted == env and trusted == listed and pinned == entry.get("args")
            and command == entry.get("command") and clear == entry.get("env_clear_prefixes")):
        return False
    if clear is not None:
        entry["env_clear_prefixes"] = clear
    entry["env"] = wanted
    entry["trusted_read_only"] = trusted
    entry["args"] = pinned
    if command is not None:
        entry["command"] = command
    _write_file(data)
    return True


def credentials_path(email: str) -> Path:
    return CREDENTIALS_DIR / f"{email.strip().lower()}.json"


def _write_credentials(email: str, item: dict) -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CREDENTIALS_DIR, 0o700)
    path = credentials_path(email)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"token": None, "refresh_token": item.get("refresh_token", ""),
                   "token_uri": TOKEN_URI, "client_id": item.get("client_id", ""),
                   "client_secret": item.get("client_secret", ""),
                   "scopes": list(item.get("scopes") or []), "expiry": None}, handle)
    tmp.replace(path)


def sync_credentials(accounts: dict[str, dict], *, refresh: tuple[str, ...] = ()) -> None:
    """Make the credentials folder match the switched-on connected accounts.

    A file the server already holds is kept (it may carry scopes the server
    added on demand) unless the account just signed in again (`refresh`).
    Accounts that are switched off or gone lose their file."""
    wanted = {email.lower(): item for email, item in accounts.items() if item.get("workspace")}
    for email, item in wanted.items():
        if email in refresh or not credentials_path(email).exists():
            _write_credentials(email, item)
    if CREDENTIALS_DIR.is_dir():
        for path in CREDENTIALS_DIR.glob("*.json"):
            if path.stem.lower() not in wanted:
                path.unlink(missing_ok=True)


def enabled_accounts() -> list[str]:
    from service import gmail_source
    return sorted(email for email, item in gmail_source.connected_accounts().items()
                  if item.get("workspace"))


def has_full_scopes(email: str, item: dict) -> bool:
    try:
        scopes = json.loads(credentials_path(email).read_text()).get("scopes") or []
    except Exception:  # noqa: BLE001 — no file yet: judge by the sign-in itself
        scopes = item.get("scopes") or []
    return FULL_SCOPE in scopes


def route_arguments(schema: dict, args: dict) -> dict | str:
    """Confine a Workspace tool call to an account the user switched on.

    Returns the arguments to send, or a message explaining the refusal. A call
    that names no account (or one that isn't switched on) goes to the only
    switched-on account when there is exactly one."""
    if "user_google_email" not in ((schema or {}).get("properties") or {}):
        return args
    allowed = enabled_accounts()
    if not allowed:
        return ("(no Google account is connected for Google tools. "
                "Connect one in Settings ▸ Google.)")
    asked = str(args.get("user_google_email") or "").strip().lower()
    if asked in allowed:
        return {**args, "user_google_email": asked}
    if len(allowed) == 1:
        return {**args, "user_google_email": allowed[0]}
    return ("(choose which Google account to use: " + ", ".join(allowed)
            + ". Pass it as user_google_email.)")
