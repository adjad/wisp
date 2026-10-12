"""Command-line configuration and secret loading for the brain server."""
from __future__ import annotations

import argparse
import ipaddress
import os
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = "~/.local/share/wisp-brain/brain.db"
DEFAULT_TOKEN_FILE = "~/.config/wisp-brain/token"
DEFAULT_PORT = 8790
DEFAULT_MODEL = "qwen3.6:35b"
MIN_TOKEN_CHARS = 32


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServerConfig:
    db: Path
    token_file: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    ollama: str | None = None
    model: str = DEFAULT_MODEL
    tz: str | None = None
    allow_public: bool = False
    mine_interval_s: float = 900.0
    nudge_interval_s: float = 60.0
    background: bool = True
    phraser_timeout: float = 8.0
    log_level: str = "info"


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, allow_public: bool) -> None:
    """Loopback only unless --allow-public (Tailscale Serve fronts the port)."""
    if not is_loopback_host(host) and not allow_public:
        raise ConfigError(
            f"refusing to bind {host!r}: the brain server listens on loopback only. "
            "Expose it privately with `tailscale serve --bg --https=8443 http://127.0.0.1:8790`, "
            "or pass --allow-public if you really mean it.")


def load_token(path: Path) -> str:
    """Read the bearer token; refuse files readable by group/other."""
    try:
        st = path.stat()
    except FileNotFoundError:
        raise ConfigError(f"token file {path} not found (deploy/gx10/install-brain.sh creates it)") from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"token file {path} is not a regular file")
    if st.st_mode & 0o077:
        raise ConfigError(f"token file {path} must be mode 0600 (run: chmod 600 {path})")
    token = path.read_text("ascii", errors="strict").strip()
    if len(token) < MIN_TOKEN_CHARS or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ConfigError(f"token in {path} must be >= {MIN_TOKEN_CHARS} printable ASCII characters")
    return token


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brain_server",
                                description="Wisp behavior-engine server (bearer auth, loopback by default).")
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    p.add_argument("--token-file", default=DEFAULT_TOKEN_FILE,
                   help=f"bearer token file, mode 0600 (default {DEFAULT_TOKEN_FILE})")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--allow-public", action="store_true",
                   help="allow binding a non-loopback address (not recommended)")
    p.add_argument("--ollama", default=None,
                   help="Ollama origin for the optional phraser, e.g. http://127.0.0.1:11434 "
                        "(uses its OpenAI-compatible /v1). Omit for template-only text.")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"phraser model id (default {DEFAULT_MODEL})")
    p.add_argument("--phraser-timeout", type=float, default=8.0)
    p.add_argument("--tz", default=None, help="IANA timezone of the user, e.g. America/Los_Angeles")
    p.add_argument("--mine-interval", type=float, default=900.0, help="seconds between background mining")
    p.add_argument("--nudge-interval", type=float, default=60.0, help="seconds between nudge generation")
    p.add_argument("--no-background", action="store_true", help="disable the background miner/nudger")
    p.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    return p


def parse_args(argv: list[str] | None = None) -> ServerConfig:
    a = build_parser().parse_args(argv)
    if not 1 <= a.port <= 65535:
        raise ConfigError("--port must be 1..65535")
    check_bind(a.host, a.allow_public)
    if a.mine_interval <= 0 or a.nudge_interval <= 0:
        raise ConfigError("intervals must be positive")
    return ServerConfig(
        db=Path(os.path.expanduser(a.db)), token_file=Path(os.path.expanduser(a.token_file)),
        host=a.host, port=a.port, ollama=a.ollama.rstrip("/") if a.ollama else None,
        model=a.model, tz=a.tz, allow_public=a.allow_public,
        mine_interval_s=a.mine_interval, nudge_interval_s=a.nudge_interval,
        background=not a.no_background, phraser_timeout=a.phraser_timeout, log_level=a.log_level,
    )
