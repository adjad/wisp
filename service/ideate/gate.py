"""What Ideate does with each tool call the harness is about to make: run it, ask, or refuse.

Every call reaches here (the gate extension asks before every tool), and the answer uses
Wisp's own policy (service/safety/policy.py), the same one run_shell is judged by:

- a shell command Wisp refuses (its never-run list, its floor, view-only mode) is refused,
  with Wisp's reason;
- a shell command runs without a card only when Wisp's policy allows it AND it is one of
  Wisp's validated read-only commands (policy.read_only_shell_argv: ls, cat, head, ...).
  Wisp's Full access mode does not lift this: the agent, not the person, wrote the command;
- package installs and anything that reaches the network always get a card, and the card
  says the sandbox has no internet, so it will most likely fail even if allowed;
- every other shell command gets a card with Wisp's reason;
- read: runs inside work/; anywhere else it gets a card (Wisp holds Full Disk Access, and the
  sandbox denies the person's mail, messages, keys and other stores besides);
- shell commands that talk to the local model engine (its port, 127.0.0.1, localhost) get their own
  card, which says so; commands that run launchctl, security, screencapture, open, osascript,
  shortcuts, `defaults write` or crontab are refused outright;
- write/edit inside work/: runs (the sandbox is the boundary). Outside work/ (or through a
  link, `..` or `~`): a card that says the sandbox will refuse the write anyway, or a
  refusal when Wisp's own path floor refuses it;
- any other tool: refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from service.ideate import config, store

ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"

NO_NETWORK_NOTE = ("Ideate's sandbox has no internet, so this will most likely fail even if you allow it.")
OUTSIDE_NOTE = ("Ideate's sandbox only lets it write inside the project folder, so this will be refused "
                "even if you allow it.")

_INSTALL = re.compile(
    r"(?ix)(?:^|[\s;&|()`])(?:"
    r"(?:npm|pnpm|yarn|bun)\s+(?:i|install|add|ci|update|upgrade|create|dlx|x)\b"
    r"|npx\b|bunx\b|pnpx\b"
    r"|(?:python3?\s+-m\s+)?pip3?\s+install\b|pipx\s+(?:install|run)\b|uvx?\s+(?:pip\s+install|add|sync|tool|run)\b"
    r"|poetry\s+(?:add|install|update)\b|conda\s+install\b"
    r"|brew\s+(?:install|upgrade|update|tap)\b|port\s+install\b"
    r"|gem\s+install\b|bundle\s+install\b|cargo\s+(?:install|add|fetch)\b"
    r"|go\s+(?:get|install|mod\s+download)\b|composer\s+(?:install|require)\b"
    r"|deno\s+(?:install|add)\b|swift\s+package\s+(?:resolve|update)\b)")
_NETWORK = re.compile(
    r"(?ix)(?:^|[\s;&|()`/])(?:curl|wget|ssh|scp|sftp|rsync|nc|ncat|netcat|telnet|ftp|ping|dig|nslookup|host)\b"
    r"|\bgit\s+(?:clone|fetch|pull|push|ls-remote|submodule)\b"
    r"|https?://")


ENGINE_NOTE = ("This talks to Wisp's local model engine on this Mac, the one running Ideate. "
               "Only allow it if you know what it does.")
READ_NOTE = ("This reads a file outside the project folder. Wisp's own folder, keys and personal "
             "stores stay blocked even if you allow it.")

_LOCAL = re.compile(r"(?i)127\.\d+\.\d+\.\d+|\blocalhost\b|\b0\.0\.0\.0\b|(?<![\w:])::1(?![\w:])|\[::1\]|"
                    r"\b0x7f[0-9a-f]*\b|\b2130706433\b")
_BARRED = re.compile(
    r"(?ix)(?:^|[;&|(`\n]|\$\()\s*(?:(?:sudo|env|exec|command|nohup|time|xargs|builtin)\s+(?:-\S+\s+)*)*"
    r"(?:\S*/)?(?:launchctl|security|screencapture|open|osascript|shortcuts|crontab|defaults\s+write)\b")


@dataclass
class Verdict:
    action: str
    reason: str = ""
    # What the card says it would run or write (a command or a path).
    text: str = ""
    path: str = ""
    tool: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)


def _policy():
    from service.safety import policy
    return policy


def judge(tool: str, args: dict, work: Path, engine_port: int | None = None) -> Verdict:
    tool = str(tool or "")
    args = args if isinstance(args, dict) else {}
    if tool == "read":
        return _judge_read(tool, args, work)
    if tool in ("write", "edit"):
        return _judge_write(tool, args, work)
    if tool == "bash":
        return _judge_shell(args, work, engine_port)
    return Verdict(DENY, "Ideate only uses its file and shell tools.", tool=tool)


def _judge_read(tool: str, args: dict, work: Path) -> Verdict:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return Verdict(DENY, "That read names no file.", tool=tool)
    if store.inside(work, raw) is not None:
        return Verdict(ALLOW, "inside the project", path=raw, tool=tool)
    try:
        hit = _policy().credential_path_hit({"path": raw if raw.startswith(("/", "~")) else str(Path(work) / raw)}, "read_file")
    except Exception:  # noqa: BLE001 - a policy hiccup must not widen anything
        hit = None
    if hit:
        return Verdict(DENY, f"Wisp won't read there ({hit}).", path=raw, tool=tool)
    return Verdict(CONFIRM, "reads outside the project folder", path=raw, tool=tool, note=READ_NOTE)


def _judge_write(tool: str, args: dict, work: Path) -> Verdict:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return Verdict(DENY, "That edit names no file.", tool=tool)
    if store.inside(work, raw) is not None:
        return Verdict(ALLOW, "inside the project", path=raw, tool=tool)
    policy = _policy()
    target = raw if raw.startswith(("/", "~")) else str(Path(work) / raw)
    decision = policy.decide("fs_write", {"path": target}, "write_file")
    if decision.tier is policy.Tier.DENY:
        return Verdict(DENY, f"Wisp won't write there ({decision.reason}).", path=raw, tool=tool)
    return Verdict(CONFIRM, "writes outside the project folder", path=raw, tool=tool, note=OUTSIDE_NOTE)


def _judge_shell(args: dict, work: Path, engine_port: int | None = None) -> Verdict:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return Verdict(DENY, "That shell step has no command.", tool="bash")
    shown = store.cap(command, config.COMMAND_TEXT_CHARS)
    policy = _policy()
    if len(command) > policy.CARD_TEXT_LIMIT:
        return Verdict(DENY, "That command is too long to show on a card, so Wisp won't run it.",
                       text=shown, tool="bash")
    decision = policy.decide("shell", {"cmd": command}, "run_shell")
    if decision.tier is policy.Tier.DENY:
        return Verdict(DENY, decision.reason, text=shown, tool="bash")
    if _BARRED.search(command):
        return Verdict(DENY, "Ideate doesn't run system, scheduling, screen or app-launching commands.",
                       text=shown, tool="bash")
    port_hit = bool(engine_port) and re.search(rf"(?<!\d){int(engine_port)}(?!\d)", command) is not None
    if _LOCAL.search(command) or port_hit:
        return Verdict(CONFIRM, "talks to the local model engine", text=shown, tool="bash", note=ENGINE_NOTE)
    if _INSTALL.search(command) or _NETWORK.search(command) or policy.shell_egress_hit({"cmd": command}):
        return Verdict(CONFIRM, "needs the internet", text=shown, tool="bash", note=NO_NETWORK_NOTE)
    if decision.tier is policy.Tier.ALLOW and policy.read_only_shell_argv(command) is not None:
        return Verdict(ALLOW, "read-only command", text=shown, tool="bash")
    reason = decision.reason if decision.tier is policy.Tier.CONFIRM else "shell command may change files"
    return Verdict(CONFIRM, reason, text=shown, tool="bash")
