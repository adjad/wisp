"""Policy engine — decides allow / confirm / deny for every tool action.

Deterministic and rule-based on purpose. Reads optional overrides from
service/config/policy.yaml but ships with safe defaults so it works standalone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml

POLICY_YAML = Path(__file__).parent.parent / "config" / "policy.yaml"


class Tier(str, Enum):
    ALLOW = "allow"      # safe / read-only -> run automatically
    CONFIRM = "confirm"  # mutating / outbound -> ask the user first
    DENY = "deny"        # dangerous / irreversible -> refuse outright


@dataclass
class Decision:
    tier: Tier
    reason: str


# --- built-in defaults (used if policy.yaml is absent) ---------------------

# read-only shell commands that auto-run
_SHELL_ALLOW = [
    r"^\s*(ls|pwd|cat|head|tail|grep|rg|find|echo|which|whoami|date|cal|df|du|ps|"
    r"wc|file|stat|tree|uname|hostname|env|printenv|man|less|more|open\s+-R|"
    r"git\s+(status|log|diff|show|branch|remote|config\s+--get)|"
    r"brew\s+(list|info|--version)|python3?\s+--version|node\s+--version|"
    r"pip3?\s+(list|show|--version)|cat\s)\b",
]
# shell commands that are never allowed
_SHELL_DENY = [
    r"\brm\s+(-[a-zA-Z]*\s+)*(-rf|-fr|-r\s+-f|-f\s+-r)\b.*\s+(/|~|\$HOME)\s*$",
    r"\brm\s+-rf?\s+/",
    r"\bsudo\b", r"\bsu\b\s", r"\bdoas\b",
    r"\bmkfs\b", r"\bdd\b\s+if=", r"\bfdisk\b", r"\bdiskutil\s+(erase|reformat)",
    r"curl[^|]*\|\s*(sudo\s+)?(sh|bash|zsh)", r"wget[^|]*\|\s*(sh|bash|zsh)",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",        # fork bomb
    r">\s*/dev/(disk|sd|rdisk)", r"\bchmod\s+-R\s+777\s+/",
    r"\bkillall\b", r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    r"\blaunchctl\s+(unload|remove|bootout)",
    r"\bdefaults\s+delete\b", r"\bnvram\b",
]
# operators / verbs that mean a shell command could MODIFY the system, even if it
# starts with a read-only command (e.g. `echo hi > file`, `ls; rm x`, `cat a | tee b`).
# Presence of any disqualifies a command from auto-allow (→ confirm, or deny in view-only).
_SHELL_MUTATE = [
    r">>?\s*(?!&|/dev/null\b|/dev/stdout\b|/dev/stderr\b)[\w./~$-]",  # redirect to a file
    r"\b(tee|rm|rmdir|mv|cp|mkdir|touch|ln|chmod|chown|chgrp|truncate|dd|"
    r"kill|pkill|killall|shutdown|reboot|launchctl|crontab|caffeinate|pmset|"
    r"installer|softwareupdate|systemsetup|networksetup|spctl|csrutil|osascript)\b",
    r"\bdefaults\s+write\b",
    r"\bgit\s+(commit|push|reset|checkout|clean|rm|mv|stash|merge|rebase|apply)\b",
    r"\bpip3?\s+(install|uninstall)\b", r"\bbrew\s+(install|uninstall|upgrade|reinstall)\b",
    r"\bnpm\s+(install|uninstall|i|update)\b",
]
# filesystem paths that are never writable/deletable
_PATH_DENY = [
    r"^/System", r"^/usr(?!/local)", r"^/bin", r"^/sbin", r"^/Library/LaunchDaemons",
    r"^/etc", r"^/var/db", r"\.ssh/", r"Keychains?/", r"\.aws/credentials",
]


def _load_yaml() -> dict:
    if POLICY_YAML.exists():
        with POLICY_YAML.open() as f:
            return yaml.safe_load(f) or {}
    return {}


_CFG = _load_yaml()

# View-only mode: the app may READ/inspect anything but never modify the machine.
# Default ON — a local assistant shouldn't be able to change things unless you opt in.
_READ_ONLY = bool(_CFG.get("read_only", True))


def read_only() -> bool:
    return _READ_ONLY


def set_read_only(value: bool) -> None:
    global _READ_ONLY
    _READ_ONLY = bool(value)


# Full-access mode: every action auto-runs with NO confirmation. Overrides
# read_only. The catastrophic-command floor (_SHELL_DENY) is still enforced
# unless `disable_safety_floor: true` is also set — that floor guards against the
# model being prompt-injected into something irreversible, not against you.
_FULL_ACCESS = bool(_CFG.get("full_access", False))
_KEEP_FLOOR = not bool(_CFG.get("disable_safety_floor", False))


def full_access() -> bool:
    return _FULL_ACCESS


def set_full_access(value: bool) -> None:
    global _FULL_ACCESS
    _FULL_ACCESS = bool(value)


def _rules(key: str, default: list[str]) -> list[str]:
    return _CFG.get(key, default)


def _any(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return p
    return None


# Categories that ask for confirmation NO MATTER what access mode is set —
# full_access is meant to skip confirmation for routine reads/writes, but a
# speed test spends real bandwidth/time worth surfacing regardless. (Keyboard
# backlight was meant to join this list too, but turned out to have no real
# effect on this hardware at all — see set_keyboard_backlight — so there's no
# action left to gate.)
_ALWAYS_CONFIRM = {"network_active"}

# Outbound actions performed as the user and seen by other people (mail, texts)
# or that write to a remote service. Confirmed on every call regardless of
# access mode, and not eligible for a standing grant — see decide() and
# grants._NEVER_GRANTABLE.
_ALWAYS_CONFIRM_OUTBOUND = {"email_send", "messages_send", "network_write"}

# Installing a self-authored tool (service/tools/tool_authoring.py). Same
# always-confirm, never-grantable treatment as the outbound-send categories —
# arguably more warranted, since this is a local model's generated code about
# to become something that can execute on this machine going forward, not a
# one-shot message. Drafting the code (category "codegen") stays ALLOW —
# nothing risky happens until create_tool actually writes and registers it.
_ALWAYS_CONFIRM_TOOL_AUTHORING = {"tool_authoring"}


def _hard_deny(category: str, args: dict) -> Decision | None:
    """The unconditional safety floor: rules nothing can override — not
    full_access, not a standing grant, not the user clicking "always allow".

    Split out of decide() so grants have something to be checked *after*. A
    grant that could lift one of these wouldn't be a permission model, it would
    be a bypass.
    """
    if category == "shell":
        cmd = str(args.get("cmd", ""))
        if (hit := _any(_rules("shell_deny", _SHELL_DENY), cmd)):
            return Decision(Tier.DENY, f"matches blocked pattern: {hit}")
    if category in ("fs_write", "fs_delete"):
        path = str(args.get("path", ""))
        if (hit := _any(_rules("path_deny", _PATH_DENY), path)):
            return Decision(Tier.DENY, f"protected path: {hit}")
    return None


def decide(category: str, args: dict, tool: str | None = None) -> Decision:
    """Return the policy decision for a tool action.

    `category` is the tool's safety category (shell, fs_read, fs_write,
    fs_delete, app_control, network, screen). `args` are the tool arguments.
    `tool` is the tool's name, used to look up standing grants the user has
    given ("always allow this") — see service/safety/grants.py. Optional so
    older call sites keep working; without it, only the category rules apply.
    """
    # The floor runs before everything, including full_access and grants.
    # `disable_safety_floor` only ever meant "let full_access mean literally
    # everything" — outside full_access these checks have always been
    # unconditional, and stay that way.
    if (_KEEP_FLOOR or not _FULL_ACCESS) and (floor := _hard_deny(category, args)) is not None:
        return floor

    # A standing grant the user gave for this exact tool (and scope). Checked
    # after the floor and before the mode logic, so it can answer a repeat of a
    # question the user already answered — including lifting view-only for that
    # one tool, which is the narrow alternative to switching full_access on
    # globally just to get one action through.
    if tool:
        from service.safety import grants
        verdict = grants.check(tool, args)
        if verdict == "deny":
            return Decision(Tier.DENY, f"you blocked {tool} for this target")
        if verdict == "allow" and category not in _ALWAYS_CONFIRM:
            return Decision(Tier.ALLOW, f"you allowed {tool} for this target")

    if category in _ALWAYS_CONFIRM:
        if read_only():
            return Decision(Tier.DENY, f"view-only mode: {category} is disabled")
        return Decision(Tier.CONFIRM, f"{category} action (always confirmed, regardless of access mode)")

    # Outbound messages sent as the user, to other people. Irreversible and
    # visible outside this machine, so they ask every single time — full_access
    # speeds up operating YOUR machine, it shouldn't silently authorize speaking
    # in your name. grants.py refuses to store an allow rule for these too.
    if category in _ALWAYS_CONFIRM_OUTBOUND:
        if read_only():
            return Decision(Tier.DENY, f"view-only mode: {category} is disabled")
        return Decision(Tier.CONFIRM, "sends something on your behalf — always confirmed")

    if category in _ALWAYS_CONFIRM_TOOL_AUTHORING:
        if read_only():
            return Decision(Tier.DENY, "view-only mode: installing new tools is disabled")
        return Decision(Tier.CONFIRM,
                        "installs a new tool that can run on this machine going "
                        "forward — review the code shown above before approving")

    if _FULL_ACCESS:
        # Auto-allow everything; the catastrophic floor already ran above.
        return Decision(Tier.ALLOW, "full-access mode")

    ro = read_only()

    if category == "shell":
        cmd = str(args.get("cmd", ""))
        mutating = _any(_rules("shell_mutate", _SHELL_MUTATE), cmd)
        if not mutating and _any(_rules("shell_allow", _SHELL_ALLOW), cmd):
            return Decision(Tier.ALLOW, "read-only command")
        if ro:
            return Decision(Tier.DENY, "view-only: only read-only commands are allowed")
        return Decision(Tier.CONFIRM, "shell command may change system state")

    if category == "fs_read":
        return Decision(Tier.ALLOW, "reading a file")

    if category == "screen":
        return Decision(Tier.ALLOW, "reading the screen")

    if category == "codegen":
        return Decision(Tier.ALLOW, "generating code (no system change)")

    # Assistant commitments live in Wisp's own local database (~/.moe) — reading
    # or adding a reminder never touches user files or system state, so both are
    # safe even in view-only mode.
    if category in ("assistant_read", "assistant_write"):
        return Decision(Tier.ALLOW, "Wisp's own commitment store")

    # Reading the inbox to summarize it is read-only (no send, no delete).
    if category == "email_read":
        return Decision(Tier.ALLOW, "reading email to summarize")

    # Reading iMessage/SMS history to summarize it is likewise read-only.
    if category == "messages_read":
        return Decision(Tier.ALLOW, "reading messages to summarize")

    # Reading Notes.app content is likewise read-only.
    if category == "notes_read":
        return Decision(Tier.ALLOW, "reading notes")

    # web_fetch is a plain GET — no auth, no cookies, no local mutation — same
    # read-only reasoning as email/messages/notes above. Distinct from
    # "network" below, which is reserved for outbound actions with a side
    # effect (e.g. sending something) and stays CONFIRM/DENY-gated.
    if category == "web_read":
        return Decision(Tier.ALLOW, "read-only web fetch (GET)")

    if category in ("fs_write", "fs_delete"):
        path = str(args.get("path", ""))
        if ro:
            return Decision(Tier.DENY, "view-only mode: modifying files is disabled")
        verb = "deleting" if category == "fs_delete" else "writing"
        return Decision(Tier.CONFIRM, f"{verb} {path}")

    if category in ("app_control", "network"):
        if ro:
            return Decision(Tier.DENY, f"view-only mode: {category} is disabled")
        return Decision(Tier.CONFIRM, f"{category} action")

    # Tools from a configured MCP server (service/mcp). A server the user
    # pointed Wisp at is trusted enough to run, not trusted enough to decide
    # its own permissions — see _category_for for why only a read annotation
    # can lower the tier, and never the reverse.
    if category == "mcp_read":
        return Decision(Tier.ALLOW, "read-only MCP tool")
    if category == "mcp_action":
        if ro:
            return Decision(Tier.DENY, "view-only mode: MCP actions are disabled")
        return Decision(Tier.CONFIRM, "running a tool from an MCP server")

    # A tool contributed by an installed skill (service/skills/tools.py). It
    # runs a real command, and the skill file itself is just a folder someone
    # dropped in, so it's confirm-tier no matter what the skill claims about
    # itself. A user who trusts a specific skill tool can grant it standing
    # approval; the skill can't grant that to itself.
    if category == "skill_tool":
        if ro:
            return Decision(Tier.DENY, "view-only mode: skill tools are disabled")
        return Decision(Tier.CONFIRM, "running a tool from an installed skill")

    # Reading system state (volume level, clipboard contents) never changes
    # anything, so it's safe even in view-only mode — same reasoning as fs_read.
    if category == "system_read":
        return Decision(Tier.ALLOW, "reading system state")

    # Changing system state (volume, clipboard, Wi-Fi, screen lock) — same tier
    # as app_control: confirm normally, blocked in view-only mode.
    if category == "system_write":
        if ro:
            return Decision(Tier.DENY, "view-only mode: system changes are disabled")
        return Decision(Tier.CONFIRM, "system state change")

    # unknown category -> be conservative
    return Decision(Tier.DENY if ro else Tier.CONFIRM, f"unclassified action ({category})")
