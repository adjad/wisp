"""Policy engine — decides allow / confirm / deny for every tool action.

Deterministic and rule-based on purpose. Reads optional overrides from
service/config/policy.yaml but ships with safe defaults so it works standalone.
"""
from __future__ import annotations

import re
import shlex
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
    # Present only for implicit read-only shell admission. The executor must
    # run these validated arguments directly, never reinterpret the input.
    shell_argv: tuple[str, ...] | None = None


# --- built-in defaults (used if policy.yaml is absent) ---------------------

# Implicit permission is deliberately limited to simple system utilities.
# Programs with interpreters, plugins, config hooks, pagers, setters, or output
# files (including git/find/rg/env/date) need the normal explicit authority.
# Fixed OS paths avoid PATH lookup, aliases, or a similarly named local script.
_READ_ONLY_SHELL_PROGRAMS = {
    "pwd": "/bin/pwd", "echo": "/bin/echo", "whoami": "/usr/bin/whoami",
    "uname": "/usr/bin/uname", "ls": "/bin/ls", "cat": "/bin/cat",
    "head": "/usr/bin/head", "tail": "/usr/bin/tail", "wc": "/usr/bin/wc",
}
_READ_ONLY_SHELL_FLAGS = {
    "pwd": "LP", "uname": "amnprsv", "ls": "aAhl1dF",
    "cat": "benstuv", "wc": "clmw",
}


def read_only_shell_argv(cmd: str) -> tuple[str, ...] | None:
    """Return a fully validated command, or require explicit shell authority.

    This is not a shell parser: quotes/backslashes group literal arguments,
    while composition, comments, expansions and control characters are refused
    even inside quotes. File operands are anchored to the execution directory;
    in particular a dash-prefixed filename after -- cannot become an option.
    """
    if not isinstance(cmd, str) or re.search(r"[\x00-\x1f\x7f$`|&;<>(){}\[\]*?~#]", cmd):
        return None
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    if not words:
        return None
    name = words[0].rsplit("/", 1)[-1]
    executable = _READ_ONLY_SHELL_PROGRAMS.get(name)
    if executable is None or words[0] not in (name, executable):
        return None
    args = words[1:]
    if name == "echo":
        return (executable, *args)  # Only prints literal arguments to stdout.
    if name == "whoami":
        return (executable,) if not args else None
    if name in ("pwd", "uname"):
        flags = _READ_ONLY_SHELL_FLAGS[name]
        return ((executable, *args) if all(re.fullmatch(f"-[{flags}]+", arg) for arg in args)
                else None)

    validated = [executable]
    options = True
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if options and arg == "--":
            options = False
            continue
        if options and arg.startswith("-") and arg != "-":
            if name in ("head", "tail"):
                if arg in ("-n", "-c") and index < len(args):
                    count = args[index]
                    index += 1
                    if not re.fullmatch(r"[0-9]{1,6}", count):
                        return None
                    validated.extend((arg, count))
                elif re.fullmatch(r"-[nc][0-9]{1,6}", arg):
                    validated.append(arg)
                else:
                    return None
            elif re.fullmatch(f"-[{_READ_ONLY_SHELL_FLAGS[name]}]+", arg):
                validated.append(arg)
            else:
                return None
        else:
            if not arg:
                return None  # An empty filename must not become the home directory.
            if arg == "-" and name in ("head", "tail"):
                return None  # BSD/GNU disagree: use ./- for the literal filename.
            validated.append(arg if arg == "-" and name in ("cat", "wc") else str(Path.home() / arg))
    return tuple(validated)


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

# Shell commands that DESTROY data irreversibly. Confirmed no matter what access
# mode is set, and not liftable by a standing grant — the same treatment
# outbound sends get, for the same reason: full_access is meant to speed up
# operating your machine, not to authorize something you can never undo.
#
# WHY THIS EXISTS (2026-08-16, real data loss). Asked to "reorganize the wisp
# debug logs in my downloads folder", the agent created destination folders
# INSIDE the source folder, then ran
#     rm -rf ~/Downloads/DebugLogs ~/Downloads/Logs_2026-08-08 ~/Downloads/Logs_2026-08-09
# without ever running a single `mv`. Its own reasoning for that step said "then
# I'll move files into organized folders" — the emitted command and the stated
# intent did not match. Three weeks of logs were destroyed. `rm -rf` does not go
# to Trash, and there was no Time Machine destination, so nothing was
# recoverable.
#
# The _SHELL_DENY floor did not catch it: its recursive-rm patterns require the
# target to be `/`, `~` or `$HOME`, and this named ordinary subdirectories —
# which is the far commoner shape, and the one that actually costs you data.
# Deny was rejected over confirm deliberately: recursively deleting a folder you
# asked to have deleted is legitimate, so the fix is that a human sees the exact
# command first, not that Wisp loses the capability.
#
# Scoped to RECURSIVE and BULK deletes on purpose. `rm one-file.txt` still
# auto-runs under full_access; it is undoable-ish, targeted, and gating it would
# make the mode useless. What gets a card is anything that can take out a tree or
# an unbounded set of files in one call.
_DESTRUCTIVE_SHELL = [
    # `rm` with any recursive flag: -r, -R, -rf, -fr, --recursive.
    r"\brm\s+[^|;&]*-[a-zA-Z]*[rR]",
    r"\brm\s+[^|;&]*--recursive\b",
    # `rm` with a glob — an unbounded set the model may not have enumerated.
    r"\brm\s+[^|;&]*[*?]",
    # A recursive delete wearing a different hat.
    r"\bfind\b[^|;&]*-delete\b",
    r"\bfind\b[^|;&]*-exec\s+rm\b",
    # Mirrors a source over a destination, deleting whatever isn't in the source.
    r"\brsync\b[^|;&]*--delete\b",
    # Removes untracked files, including ignored ones with -x. Not recoverable
    # from git precisely because the files were never tracked.
    r"\bgit\s+clean\s+-[a-zA-Z]*[dx]",
]


def is_destructive_shell(cmd: str) -> str | None:
    """The destructive pattern `cmd` matches, or None. Public so the agent
    loop's confirmation card can explain WHICH rule fired."""
    return _any(_rules("destructive_shell", _DESTRUCTIVE_SHELL), cmd)

# Outbound actions performed as the user and seen by other people (mail, texts)
# or that write to a remote service. Confirmed on every call regardless of
# access mode, and not eligible for a standing grant — see decide() and
# grants._NEVER_GRANTABLE.
#
# "scheduled_send" is here for a reason worth stating plainly: it is the ONLY
# category whose confirmation authorizes something that happens later, while
# nobody is watching. The card is shown at scheduling time and covers the
# eventual delivery, because asking again at fire time would defeat the feature
# (the user scheduled 6pm precisely so they need not be present at 6pm) and an
# unanswered card would either hang forever or time out into sending anyway.
# That makes the one prompt the user does see the only gate there will be —
# hence always-confirm and never-grantable, exactly like an immediate send.
_ALWAYS_CONFIRM_OUTBOUND = {"email_send", "messages_send", "network_write",
                            "scheduled_send"}

# add_calendar_event / cancel_event. Same always-confirm, never-grantable
# treatment as the outbound-send categories above, for a comparable reason:
# a wrongly cancelled or wrongly dated calendar event is about as hard to
# undo as a sent message — the user has to notice it's gone/wrong and
# manually recreate or fix it, there is no "unsend".
#
# WHY THIS EXISTS (2026-08-18, real incident — see move_path's docstring for
# the file-system counterpart of the same lesson). Asked to "remove all of my
# reminders and calendar events except the UCSC move-in appointment and the
# ant poison treatment — move it to 5pm today", the model ran 14
# cancel_event/add_calendar_event calls with ZERO confirmation of any kind
# (category was plain "assistant_write", unconditional ALLOW) — cancelled the
# UCSC event it was told to keep, ran a duplicate pass over the same titles,
# and fabricated a wrongly-dated duplicate "UCSC Move-In Appointment" at 5pm
# today (confusing the two named exceptions) with a garbled location field.
# Measured afterwards (dry-run, 5 reps each): this class of compound
# multi-constraint instruction fails at comparable-or-worse rates on oQ6e and
# even on Agents-A1-4B (pre-quant) — never a clean run on any of the three.
# Confirmation isn't a workaround for a model-quality bug that might get
# fixed later; a human reviewing the actual list of changes before they
# happen is the only mitigation that holds regardless of which model is
# proposing them. add_reminder is deliberately NOT here — one low-stakes
# reminder stays on the ordinary assistant_write rules; see the agent loop's
# batched-preview handling for the "one button, not fourteen" side of this.
_ALWAYS_CONFIRM_CALENDAR = {"calendar_write"}

# Composing into Mail/Messages WITHOUT sending (draft_email, draft_message).
# Deliberately NOT in _ALWAYS_CONFIRM_OUTBOUND: nothing leaves the machine, and
# the user's own click in Mail or Messages is a stronger confirmation than a
# card in Wisp. Gating a draft behind the same prompt as a send would make the
# safe path as tedious as the irreversible one, which is how people end up
# reaching for send_email when they meant to review first. Still blocked in
# view-only mode, since it does open windows and fill them in.
_DRAFT_CATEGORIES = {"email_draft", "messages_draft"}

# Mailbox triage on the user's OWN mail (mark_email_read, archive_email).
# Nothing is sent and nothing is destroyed — archive MOVES a message to the
# Archive mailbox, where it stays findable — so these follow the ordinary
# write rules rather than the outbound ones.
_TRIAGE_CATEGORIES = {"email_triage"}

# Installing a self-authored tool (service/tools/tool_authoring.py). Same
# always-confirm, never-grantable treatment as the outbound-send categories —
# arguably more warranted, since this is a local model's generated code about
# to become something that can execute on this machine going forward, not a
# one-shot message. Drafting the code itself (create_tool's own prepare_draft,
# and formerly write_code's dedicated "codegen" category before its removal —
# see codegen.py's removal) stays ALLOW — nothing risky happens until
# create_tool actually writes and registers the result.
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

    if tool == "organize_files":
        if not args.get("confirm", False):
            return Decision(Tier.ALLOW, "file listing preview only; no files move")
        if read_only():
            return Decision(Tier.DENY, "view-only mode: moving files is disabled")
        if not args.get("preview_token"):
            return Decision(Tier.DENY, "a matching file preview is required before moving")
        return Decision(Tier.CONFIRM, "moves the exact previewed file set — always confirmed")

    if (tool in {"send_email", "draft_email"}
            or (tool == "schedule_send" and args.get("channel") == "email")):
        if re.fullmatch(r"\+?[\d().\s-]{7,}", str(args.get("to", "")).strip()):
            return Decision(Tier.DENY, "a phone number is not an email address; ask for the recipient's email")

    # A send whose `to` is literally the USER'S OWN address can never
    # succeed — service.tools.action_tools._own_address_guard refuses it
    # inside the tool itself, with a message that teaches the model to
    # retry via lookup_contact. That guard used to run AFTER the CONFIRM
    # card, so the user had to approve a send that was already guaranteed to
    # bounce before the model got the corrective message and tried again —
    # two confirmations for one real send.
    #
    # MEASURED FAILURE (2026-08-23, user's debug export): asked to text mom
    # the GOOGL/MU prices, the model called send_message with
    # to="AdiJain888@gmail.com" — the user's OWN email, present in the
    # system prompt only for attribution — instead of calling
    # `lookup_contact("Mom")` first. The user confirmed that card, THEN it
    # bounced, THEN the model looked up Mom's real number and the user had
    # to confirm a second time. Checking the guard here, before CONFIRM is
    # even considered, means a doomed call is corrected in the same turn as
    # a DENY (no card shown at all) rather than costing a wasted
    # confirmation — same outcome, one fewer prompt.
    if tool in ("send_message", "send_email"):
        from service.tools.action_tools import _own_address_guard
        if (msg := _own_address_guard(
                str(args.get("to", "")), tool,
                confirmed_self_send=bool(args.get("confirmed_self_send", False)))):
            return Decision(Tier.DENY, msg)

    # An irreversible bulk/recursive delete (see _DESTRUCTIVE_SHELL). Computed
    # BEFORE grants so a standing "always allow run_shell" cannot lift it — a
    # grant is an answer to a question the user already considered, and nobody
    # approving `run_shell` once was agreeing to every future `rm -rf`.
    destructive = (is_destructive_shell(str(args.get("cmd", "")))
                   if category == "shell" else None)

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
        if verdict == "allow" and category not in _ALWAYS_CONFIRM and not destructive:
            return Decision(Tier.ALLOW, f"you allowed {tool} for this target")

    # Irreversible and unbounded: always show the exact command, in every access
    # mode. Placed above the _ALWAYS_CONFIRM block so it also outranks
    # full_access, which is the mode this was destroying data under.
    if destructive:
        if read_only():
            return Decision(Tier.DENY, "view-only mode: deleting is disabled")
        return Decision(Tier.CONFIRM,
                        f"⚠ This permanently deletes files and cannot be undone "
                        f"(matched {destructive}). It does NOT go to the Trash. "
                        f"Check the command before allowing it.")

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

    if category in _ALWAYS_CONFIRM_CALENDAR:
        if read_only():
            return Decision(Tier.DENY, f"view-only mode: {category} is disabled")
        return Decision(Tier.CONFIRM,
                        "changes your calendar — always confirmed")

    # Drafts and mailbox triage: real writes, but nothing irreversible and
    # nothing sent. They fall through to the normal access-mode rules below —
    # allowed outright in full-access, confirmed in the default mode, denied in
    # view-only.
    if category in _DRAFT_CATEGORIES | _TRIAGE_CATEGORIES:
        if read_only():
            return Decision(Tier.DENY, f"view-only mode: {category} is disabled")
        if _FULL_ACCESS:
            return Decision(Tier.ALLOW, "full-access mode")
        return Decision(Tier.CONFIRM,
                        "opens a draft for you to review — nothing is sent"
                        if category in _DRAFT_CATEGORIES
                        else "changes your mailbox (reversible)")

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
        cmd = args.get("cmd", "")
        argv = read_only_shell_argv(cmd)
        # Legacy YAML rules may narrow this contract, but cannot widen it.
        if (argv is not None and not _any(_rules("shell_mutate", _SHELL_MUTATE), cmd)
                and ("shell_allow" not in _CFG or _any(_CFG["shell_allow"], cmd))):
            return Decision(Tier.ALLOW, "read-only command", shell_argv=argv)
        if ro:
            return Decision(Tier.DENY, "view-only: only read-only commands are allowed")
        return Decision(Tier.CONFIRM, "shell command may change system state")

    if category == "fs_read":
        return Decision(Tier.ALLOW, "reading a file")

    if category == "screen":
        return Decision(Tier.ALLOW, "reading the screen")

    # Reads from Wisp's commitment store are safe in every mode. Reminder writes
    # are not: add/update also request an Apple Reminders mirror, and even a
    # local-only commitment changes future notifications.  View-only must
    # therefore block assistant_write just like every other mutation.
    if category == "assistant_read":
        return Decision(Tier.ALLOW, "reading Wisp's commitment store")
    if category == "assistant_write":
        if ro:
            return Decision(Tier.DENY, "view-only mode: reminders changes are disabled")
        return Decision(Tier.ALLOW, "Wisp's commitment store")

    # Reading the inbox to summarize it is read-only (no send, no delete).
    if category == "email_read":
        return Decision(Tier.ALLOW, "reading email to summarize")

    # Reading iMessage/SMS history to summarize it is likewise read-only.
    if category == "messages_read":
        return Decision(Tier.ALLOW, "reading messages to summarize")

    # Reading Notes.app content is likewise read-only.
    if category == "notes_read":
        return Decision(Tier.ALLOW, "reading notes")

    # Reading cached browser history (host/path/title only) is likewise
    # read-only, and only ever populated if the user opted in via Settings.
    if category == "browser_history_read":
        return Decision(Tier.ALLOW, "reading browser history")

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
