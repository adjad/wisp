"""Policy engine — decides allow / confirm / deny for every tool action.

Deterministic and rule-based on purpose. Reads optional overrides from
service/config/policy.yaml but ships with safe defaults so it works standalone.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import stat
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import chain
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import unquote

import yaml

from service.paths import MOE_DIR
from service.publication_authority import Authority, Witness, revocation

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
    # Present only for a plain delete's card (plain_delete_targets): the
    # (path, resolved path) pairs it was judged on, compared again by
    # run_shell immediately before the delete runs.
    delete_targets: tuple[tuple[str, str], ...] | None = None


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


def _read_only_shell_path(arg: str) -> str | None:
    """Anchor and validate one implicitly readable filesystem operand."""
    operand = Path(arg)
    if ".." in operand.parts:
        return None

    try:
        lexical_home = Path.home()
        home = lexical_home.resolve()
        anchored = operand if operand.is_absolute() else lexical_home / operand
        resolved = anchored.resolve()
        lexical_relative = anchored.relative_to(lexical_home)
        resolved_relative = resolved.relative_to(home)
    except (OSError, RuntimeError, ValueError):
        return None

    # Hidden paths and macOS's Library contain credentials, app databases, and
    # other private state that must require explicit shell authority. Check the
    # spelling supplied by the caller and the canonical target so a visible
    # symlink cannot bypass the boundary.
    for relative in (lexical_relative, resolved_relative):
        if any(part.startswith(".") for part in relative.parts):
            return None
        if relative.parts and relative.parts[0].casefold() == "library":
            return None
    return str(anchored)


# Shell or AppleScript text longer than this is not parsed or pattern-matched
# at all (shlex and the patterns below are slow on huge input): it is treated
# as needing a card, and a card cannot show it (CARD_TEXT_LIMIT), so it is
# refused. No real command is this long.
SHELL_TEXT_LIMIT = 16_000
# The most a confirmation card shows of a command or script. A longer one is
# refused rather than approved with a tail the user never saw.
CARD_TEXT_LIMIT = 4_000


def read_only_shell_argv(cmd: str) -> tuple[str, ...] | None:
    """Return a fully validated command, or require explicit shell authority.

    This is not a shell parser: quotes/backslashes group literal arguments,
    while composition, comments, expansions and control characters are refused
    even inside quotes. File operands are anchored to the user's home and must
    canonically remain in its non-private paths; in particular a dash-prefixed
    filename after -- cannot become an option.
    """
    if (not isinstance(cmd, str) or len(cmd) > SHELL_TEXT_LIMIT
            or re.search(r"[\x00-\x1f\x7f$`|&;<>(){}\[\]*?~#]", cmd)):
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
            if arg == "-" and name in ("cat", "wc"):
                validated.append(arg)
                continue
            path = _read_only_shell_path(arg)
            if path is None:
                return None
            validated.append(path)
    return tuple(validated)


# The broad recursive-delete entry. plain_delete_paths() names the one command
# shape it is skipped for.
_SHELL_BROAD_RM = r"\brm\s+-rf?\s+/"
# shell commands that are never allowed
_SHELL_DENY = [
    r"\brm\s+(-[a-zA-Z]*\s+)*(-rf|-fr|-r\s+-f|-f\s+-r)\b.*\s+(/|~|\$HOME)\s*$",
    _SHELL_BROAD_RM,
    r"\bsudo\b", r"\bsu\b\s", r"\bdoas\b",
    r"\bmkfs\b", r"\bdd\b\s+if=", r"\bfdisk\b", r"\bdiskutil\s+(erase|reformat)",
    r"curl[^|]*\|\s*(sudo\s+)?(sh|bash|zsh)", r"wget[^|]*\|\s*(sh|bash|zsh)",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",        # fork bomb
    r">\s*/dev/(disk|sd|rdisk)", r"\bchmod\s+-R\s+777\s+/",
    r"\bkillall\b", r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    r"\blaunchctl\s+(unload|remove|bootout)",
    r"\bdefaults\s+delete\b", r"\bnvram\b",
]
# Wisp's own human-only send endpoints. POST /assistant/email_reply/* and
# /assistant/send_message_draft treat the request as the user's tap on Send
# (see email_reply_api.py), so the agent must never reach them from run_shell
# or run_applescript (`do shell script "curl …"`). Checked unconditionally —
# not configurable, and not lifted by full_access or disable_safety_floor —
# because no legitimate shell command calls them.
_LOCAL_SEND_DENY = [
    r"assistant/+(email_reply|send_message_draft)\b",
    r"assistant/+proactive/+cards/+[^/\s'\"]+/+fire\b",
    r"assistant/+needs_you/+[^/\s'\"]+/+(?:done|clear)\b",
    r"assistant/+proactive/+(?:todos|threads)/+[^/\s'\"]+/+(?:done|clear)\b",
    # /resetwisp's erase: only the app, after the person's Reset Wisp click.
    r"assistant/+reset_wisp\b",
    # Bounded: unbounded lazy runs here were quadratic on a long command.
    # The route patterns above already match the path at any length.
    r"(127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)[^\s'\"]{0,256}?:8765\b[^\s'\"]{0,256}?(email_reply|send_message_draft)",
]
# Wisp's own service port on a loopback host. A route built at run time
# (`u=email_reply; curl …/assistant/$u/send`) never shows in the text, but the
# command usually still names the port, so shell/AppleScript text aimed at it
# is refused, read routes included.
#
# This is a best-effort tripwire, not a boundary. It reads text, and a shell
# can build any host or port at run time (`p=$((8000+765))`, a hostname that
# resolves to 127.0.0.1, a script file written first and run second), so it
# will never be complete and is not meant to be. The cheap spellings are
# covered: leading zeros on the port, `/dev/tcp/<host>/<port>`, `/` or `:`
# before the host, and dotted hex/octal forms of 127.0.0.1. A command that
# names a loopback host or this port at all is also confirmed under full
# access (see _SHELL_EGRESS), so a missed spelling still gets a card. The real
# boundary is the per-launch app key every route of an app-launched backend
# requires (service/local_http.AppKeyGuard); a command never holds it.
_LOOPBACK_V4 = r"(?:127|0+177|0x0*7f)(?:\.(?:0x[0-9a-f]+|\d+)){1,3}"
_LOOPBACK_NAME = (r"(?:[\w.-]*localhost\.?|" + _LOOPBACK_V4 + r"|0+(?:\.0+){1,3}"
                  r"|\[[0-9a-z:.%]*\])")     # any bracketed IPv6 literal, zone ids included
_LOOPBACK_NUMBER = r"(?:0+|0x0+|2130706433|0x0*7f[0-9a-f]{6}|0+17700000001)"
_PORT = r"0*8765(?!\d)"
_LOCAL_SERVICE_DENY = [
    # host:8765, `nc host 8765`, ("host", 8765), "host:" & "8765", /dev/tcp/host/8765
    r"(?<![\w.-])" + _LOOPBACK_NAME + r"[:\s,&+/]+" + _PORT,
    # a bare-number host only with its colon or slash (`seq 0 8765` is not a URL)
    r"(?<![\w.-])" + _LOOPBACK_NUMBER + r"[:/]" + _PORT,
    # curl --resolve name:8765:127.0.0.1 and --connect-to ::127.0.0.1:8765
    r"(?<![\w.])0*8765:(?:" + _LOOPBACK_NAME + "|" + _LOOPBACK_NUMBER + r")(?![\w.])",
]


def _command_forms(text: str) -> tuple[str, ...]:
    """What the deny patterns read: the text percent-decoded until stable
    (uvicorn decodes the path, so `/assistant/%65mail_reply/send` reaches
    email_reply) and lowercased, and that again with shell quoting removed
    (`local''host:87\\65` is localhost:8765 to the shell)."""
    out = str(text)
    for _ in range(10):
        decoded = unquote(out)
        if decoded == out:
            break
        out = decoded
    out = out.lower()
    bare = re.sub(r"[\"'\\`]", "", out)
    return (out, bare) if bare != out else (out,)


def local_send_endpoint_hit(text: str) -> str | None:
    """The pattern a shell/AppleScript text matches if it would call a human-only send route."""
    return next((hit for form in _command_forms(text) if (hit := _any(_LOCAL_SEND_DENY, form))), None)


def local_service_hit(text: str) -> str | None:
    """The pattern a shell/AppleScript text matches if it names Wisp's own service port."""
    return next((hit for form in _command_forms(text) if (hit := _any(_LOCAL_SERVICE_DENY, form))), None)


def shell_text(args: dict) -> str:
    """Everything a shell-category call will run: run_shell's `cmd` and
    run_applescript's `script` (whose `do shell script` is a shell too)."""
    return " ".join(str(args.get(k) or "") for k in ("cmd", "script")).strip()


def _quote_forms(text: str) -> tuple[str, ...]:
    """The text, and the text with shell quoting removed (`~/.s"sh"` is ~/.ssh)."""
    bare = re.sub(r"[\"'\\]", "", text)
    return (text, bare) if bare != text else (text,)


_DO_SHELL_SCRIPT = re.compile(r'do\s+shell\s+script\s+"((?:[^"\\]|\\.)*)"', re.IGNORECASE)


def shell_forms(args: dict) -> tuple[str, ...]:
    """Every spelling of a shell-category call the floor and the checks judge:
    the text, its quote-stripped form, and for AppleScript each
    `do shell script "…"` body as the shell will see it."""
    text = shell_text(args)
    forms = list(_quote_forms(text))
    for body in _DO_SHELL_SCRIPT.findall(str(args.get("script") or "")):
        forms.extend(_quote_forms(re.sub(r"\\(.)", r"\1", body)))
    return tuple(dict.fromkeys(forms))


# A plain grep over files. With no shell operators or expansions it can only
# read and print, so searching docs that mention localhost:8765 (or a send
# route) is not a way to reach the service. Fixed program names only; ripgrep
# is left out because `rg --pre` runs a command.
_PLAIN_SEARCH_PROGRAMS = {"grep", "egrep", "fgrep"}


def _cannot_reach_network(args: dict) -> bool:
    """True only for a run_shell command that provably opens no connection:
    a validated read-only argv, or a plain grep with no shell syntax."""
    cmd = args.get("cmd")
    if (args.get("script") or not isinstance(cmd, str) or not cmd.strip()
            or len(cmd) > SHELL_TEXT_LIMIT):
        return False
    if read_only_shell_argv(cmd) is not None:
        return True
    if re.search(r"[\x00-\x1f\x7f$`|&;<>(){}\\]", cmd):
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        return False
    name = words[0].rsplit("/", 1)[-1] if words else ""
    return name in _PLAIN_SEARCH_PROGRAMS and words[0] in (name, f"/usr/bin/{name}")


# Shell text that sends something off this Mac, or drives another app that can.
# Under full access these still get a card (see _decide); a standing grant
# cannot lift them. Like the deny above this reads text, so it is a net for the
# common shapes, not a guarantee: a script file, an alias or a name built at
# run time gets past it. run_applescript itself is always confirmed.
#
# Matched case-insensitively (macOS paths are case-insensitive, so CURL runs
# curl) against the text and its quote-stripped form.
_CMD_POS = (r"(?:^|[;&|({\n`]|\$\(|\b(?:then|do|else|exec|env|command|nohup|time|xargs|nice)\b)"
            r"\s*(?:-\S+\s+)*(?:[\w./~-]*/)?")
# A loopback host named anywhere: Wisp, oMLX and every other local service.
_LOOPBACK_ANYWHERE = [
    r"(?<![\w.-])(?:[\w.-]*localhost|" + _LOOPBACK_V4 + r"|0+(?:\.0+){3}|\[[0-9a-z:.%]*\])(?![\w-])",
    r"(?<![\w.-])" + _LOOPBACK_NUMBER + r"[:/]\d",
]
_SHELL_EGRESS = [
    # Apple Events: Mail, Messages and any other app, from the shell.
    r"(?<![\w.-])(?:osascript|osacompile|automator)(?![\w.-])",
    _CMD_POS + r"shortcuts\s+run\b",
    # Raw sockets, remote shells, file transfer, mail. Only where a command
    # starts, so ~/Library/Mail or ~/.ssh in a path is not a hit.
    _CMD_POS + r"(?:nc|ncat|netcat|socat|telnet|ssh|scp|sftp|ftp|tftp|sendmail|mailx?)(?=\s|$|[;&|)`])",
    r"/dev/(?:tcp|udp)/",
    # Protocols that write: mail submission, raw TCP through curl.
    r"\b(?:smtps?|gopher|dict|telnet|mqtts?)://",
    # wget uploads.
    r"(?<![\w.-])wget(?![\w.-])[\s\S]*(?:--(?:post-data|post-file|method|body-data|body-file|execute)\b|\s-e\s)",
    *_LOOPBACK_ANYWHERE,
    # Script interpreters that open sockets, send Apple Events or start other programs.
    r"(?<![\w.-])(?:python[\d.]*|node|ruby|perl|php|swift|deno|bun|lua|tclsh)(?![\w-])[\s\S]*"
    r"(?:socket|urllib|requests|httpx|aiohttp|http\.client|http\.server|smtplib|ftplib|telnetlib|xmlrpc"
    r"|fetch\s*\(|xmlhttprequest|net::|net/http|lwp|nsapplescript|scriptingbridge|sbapplication"
    r"|appscript|osascript|urlsession|subprocess|os\.system|child_process|`|(?<!\d)0*8765(?!\d)"
    r"|require\s*\(\s*[\"'`]?(?:node:)?(?:https?2?|net|tls|dgram)\b|\bnode:(?:https?2?|net|tls|dgram)\b"
    r"|open-uri|open_uri|uri\.open|\bopenuri\b)",
    # Uploads and pushes with tools that are otherwise ordinary.
    _CMD_POS + r"rsync\b[^;&|\n]*(?:\s[\w.@-]+:|rsync://)",
    r"\bgit\b[^;&|\n]*\spush\b",
    r"\bgh\s+(?:gist\s+create|(?:issue|pr)\s+(?:create|comment|edit)|release\s+(?:create|upload)"
    r"|api\b[^;&|\n]*\s(?:-f|-F|--field|--raw-field|--input|-X|--method)\b)",
    r"\baws\s+s3\s+(?:cp|mv|sync)\b", r"\baws\s+s3api\s+put-", r"\bgsutil\b[^;&|\n]*\s(?:cp|mv|rsync)\b",
    r"\brclone\s+(?:copy|copyto|move|moveto|sync)\b",
    # Anything piped into a shell or interpreter runs text the command fetched or built.
    r"\|\s*(?:sudo\s+)?(?:[\w./~-]*/)?(?:sh|bash|zsh|dash|ksh|fish|python[\d.]*|node|ruby|perl|osascript)(?![\w.-])",
]
# curl/wget are ordinary for reading a page. They need a card when they upload
# (data, form, file or a method other than GET/HEAD), read their flags from a
# file, mention Wisp's port, or splice command output into the request.
# Flags are case-sensitive (-d uploads, -D dumps headers), so these run on the
# text as written, quote-stripped.
_HTTP_CLIENT = re.compile(r"(?<![\w.-])(?:curl|wget)(?![\w.-])", re.IGNORECASE)
_CURL_UPLOAD = re.compile(
    r"(?:^|\s)-[a-zA-Z]*[dFTK]"                                  # -d, -F, -T, -K, clustered too
    r"|(?:^|\s)-[a-zA-Z]*X\s*(?!(?:GET|HEAD)\b)\S"                # -X POST, -XPUT
    r"|(?:^|\s)--(?:data[\w-]*|form[\w-]*|json|upload-file|request|config|mail-rcpt|mail-from)\b"
    r"|\$\(|`|(?<!\d)0*8765(?!\d)")


# `open` hands a URL to whatever app owns its scheme. Web pages and files are
# ordinary; every other scheme (shortcuts://, imessage:, sms:, tel:,
# facetime:, mailto:...) can start a call, a message or a Shortcut, so it asks.
_OPEN_COMMAND = re.compile(_CMD_POS + r"open(?![\w.-])([^;&|\n]*)", re.IGNORECASE)
_URL_SCHEME = re.compile(r"(?:^|[\s\"'=])([a-z][a-z0-9+.-]*):", re.IGNORECASE)
_PLAIN_SCHEMES = frozenset({"http", "https", "file"})
# Files that do something when opened: a .command or .terminal runs in
# Terminal, a .webloc/.inetloc opens its URL, an .app or installer launches,
# a .shortcut or .workflow imports into Shortcuts or Automator.
_ACTIVE_FILE = re.compile(r"\.(?:command|tool|terminal|webloc|inetloc|fileloc|app|pkg|mpkg|shortcut"
                          r"|workflow)/?[\"']?$", re.IGNORECASE)
# Apps that act on what they are handed: Terminal runs it, Shortcuts runs it.
# Matched against the app's own name however the call spells it (see
# _app_base_name), and by bundle id. Other terminal apps run a file the same way.
_ACTING_APPS = frozenset({
    "shortcuts", "terminal", "iterm", "iterm2",
    "com.apple.shortcuts", "com.apple.terminal", "com.googlecode.iterm2",
    "warp", "ghostty", "kitty", "alacritty", "wezterm", "hyper",
})


def _app_base_name(name: str) -> str:
    """The app an `open -a` name points at, for comparing: `open -a` takes a
    bundle path as readily as a name, so "/System/Applications/Utilities/
    Terminal.app", "Terminal.app/" and "TERMINAL" are all "terminal"."""
    name = name.strip().strip("\"'").strip()
    if "/" in name:                      # also folds "Terminal.app/." and "x/../Terminal.app"
        name = os.path.normpath(name).rsplit("/", 1)[-1]
    name = name.strip().casefold()
    return name[:-4].rstrip() if name.endswith(".app") else name


def _open_hit(text: str) -> str | None:
    for match in _OPEN_COMMAND.finditer(text):
        rest = match.group(1)
        if re.search(r"(?:^|\s)-(?:a\s*[\"']?[^\s\"']*(?:shortcuts|terminal|iterm)"
                     r"|b\s*[\"']?com\.apple\.(?:shortcuts|terminal))", rest, re.IGNORECASE):
            return "open Shortcuts or Terminal"
        for scheme in _URL_SCHEME.findall(rest):
            if scheme.lower() not in _PLAIN_SCHEMES:
                return f"open {scheme}:"
        if any(_ACTIVE_FILE.search(word) for word in rest.split()):
            return "open a file that runs or links somewhere"
    return None


def app_open_hit(args: dict) -> str | None:
    """Why an open_app call needs a card in every mode, or None: the same rule
    as shell `open`. Shortcuts or Terminal, a scheme other than http(s) or
    file (shortcuts://, imessage:, sms:, tel:, facetime:, mailto:...), or a
    file that runs or links somewhere when opened."""
    name = str(args.get("name") or "").strip()
    target = str(args.get("target") or "").strip()
    if _app_base_name(name) in _ACTING_APPS:
        return f"opens {name}, which runs what it is given"
    if (m := re.match(r"\s*([a-z][a-z0-9+.-]*):", target, re.IGNORECASE)) and len(m.group(1)) > 1:
        if m.group(1).lower() not in _PLAIN_SCHEMES:
            return f"opens a {m.group(1)}: link"
    if target and _ACTIVE_FILE.search(target):
        return "opens a file that runs or links somewhere"
    return None


def shell_egress_hit(args: dict) -> str | None:
    """Why a shell-category call could send something off this Mac, or None.

    Read-only commands and plain greps are never hits: they cannot open a
    connection whatever their text mentions."""
    text = shell_text(args)
    if len(text) > SHELL_TEXT_LIMIT:
        return "a command too long to check"
    if not text or _cannot_reach_network(args):
        return None
    forms = dict.fromkeys((text, re.sub(r"[\"'\\]", "", text)))
    for form in forms:
        if (hit := _first_match(_SHELL_EGRESS, form) or _open_hit(form)):
            return hit
        if _HTTP_CLIENT.search(form) and (m := _CURL_UPLOAD.search(form)):
            return f"curl/wget {m.group(0).strip()}"
    for form in _command_forms(text):     # percent-encoded loopback hosts
        if (hit := _first_match(_LOOPBACK_ANYWHERE, form)):
            return hit
    return None


def _first_match(patterns: list[str], text: str) -> str | None:
    """The text the first matching pattern found (short, for a card), or None."""
    for p in patterns:
        if (m := re.search(p, text, re.IGNORECASE)):
            return " ".join(m.group(0).split())[:60] or p
    return None


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
    # The directory ITSELF counts, not only what is inside it: `~/.ssh` has no
    # trailing slash, and moving or archiving the directory moves every key in it.
    r"^/etc", r"^/var/db", r"\.ssh(?:/|$)", r"Keychains?(?:/|$)", r"\.aws/credentials",
]

# Every argument name a tool uses to receive a filesystem location. The floor used
# to read only `path`, so tools whose operands are `source`/`destination`
# (move_path, backup_folder), `folder` (organize_files) or `paths`/`archive_path`
# (archive_files) were never checked at all. Matching by argument NAME rather than
# by tool means a future tool that takes the same arguments is covered too.
_PATH_ARG_KEYS = ("path", "paths", "source", "src", "destination", "dest", "archive_path",
                  "folder", "directory", "dir", "target", "output", "output_path",
                  "file", "files")


def _path_operands(args: dict, tool: str | None = None) -> list[tuple[str, str]]:
    # `tool` is unused since archive_files was retired; kept so both call
    # sites (hard-deny and preview) need not change.
    """(argument name, value) for every string location in `args`, list items included."""
    found: list[tuple[str, str]] = []
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            found.append((key, value))
        elif isinstance(value, (list, tuple)):
            found += [(key, item) for item in value if isinstance(item, str)]
    return found


def _path_forms(raw: str) -> list[str]:
    """Every spelling of a location the floor must judge, not just the one typed.

    The caller's own spelling, the `~`-expanded form, and the CANONICAL target
    (symlinks resolved, relative paths anchored both at home and at the working
    directory because tools resolve them against the latter). macOS's /etc, /var
    and /tmp are links into /private, so a resolved path also gets its /private
    prefix removed, which is the spelling the rules are written in.
    """
    forms = {raw}
    expanded = os.path.expanduser(raw)
    forms.add(expanded)
    anchors = [Path(expanded)] if os.path.isabs(expanded) else [Path.home() / expanded, Path.cwd() / expanded]
    for anchor in anchors:
        forms.add(str(anchor))
        # Admission catches resolution failures and denies; raw spelling alone
        # cannot certify an unresolved symlink or inaccessible derived target.
        resolved = str(anchor.resolve())
        forms.add(resolved)
        if resolved.startswith("/private/"):
            forms.add(resolved[len("/private"):])
    return sorted(forms)


def _derived_path_operands(tool: str | None, args: dict):
    """Mirror deterministic output naming, without importing/executing tools.

    Only organize_files enumerates immediate matching source files, just as
    its preview does. This is admission-time checking, not recursive scanning
    or a binding of paths against changes between confirmation and execution.
    """
    # archive_files, convert_file and encrypt_file had branches here too; all
    # three were retired in the 182 -> 100 tool consolidation, so nothing could
    # reach them (test_every_tool_name_policy_names_is_registered keeps it so).
    if tool in {"write_document", "spreadsheet_ops"}:
        if not isinstance(args.get("path"), str):
            return
        target = Path(args["path"]).expanduser()
        suffix = ".docx" if tool == "write_document" else ".xlsx"
        if target.suffix.lower() != suffix:
            target = target.with_suffix(suffix)
        yield "derived output", str(target)
    elif tool == "move_path":
        if not isinstance(args.get("source"), str) or not isinstance(args.get("destination"), str):
            return
        source = Path(args["source"]).expanduser()
        target = Path(args["destination"]).expanduser()
        if args["destination"].endswith(("/", os.sep)) or target.is_dir():
            yield "destination child", str(target / source.name)
    elif tool == "organize_files":
        if not all(isinstance(args.get(key), str) for key in ("folder", "destination", "pattern")):
            return
        source = Path(args["folder"]).expanduser().resolve()
        if not source.is_dir():
            return
        target = Path(args["destination"]).expanduser()
        if not target.is_absolute():
            target = source / target
        target = target.resolve()
        group_by = args.get("group_by", "")
        if source == target and not group_by:
            return
        from service.file_organization import group_for_filename
        pattern = args["pattern"]
        if "/" in pattern:
            glob = Path(pattern).expanduser()
            if glob.parent.resolve() != source:
                return
            pattern = glob.name
        for item in source.iterdir():
            if item.is_file() and fnmatch.fnmatch(item.name, pattern):
                # Retain the conservative source floor even for a matched
                # entry excluded from grouping or execution.
                yield "source child", str(item)
                group = group_for_filename(item.name, group_by)
                if group is None:
                    continue
                # Preserve the existing floor for protected symlink sources,
                # even though execution excludes them from the move plan.
                if not item.is_symlink():
                    yield "destination child", str(target / group / item.name) if group else str(target / item.name)


def _known_config(data) -> bool:
    if type(data) is not dict:
        return False
    flags = {"read_only", "full_access", "disable_safety_floor"}
    rules = {"shell_deny", "shell_mutate", "shell_allow", "path_deny", "destructive_shell"}
    for key, value in data.items():
        if key in flags:
            if type(value) is not bool:
                return False
        elif key in rules:
            if type(value) is not list or any(type(item) is not str for item in value):
                return False
            try:
                for item in value:
                    re.compile(item, re.IGNORECASE)
            except re.error:
                return False
        else:
            return False
    return True


def _load_snapshot() -> tuple[dict, bool]:
    if POLICY_YAML.exists():
        with POLICY_YAML.open() as f:
            raw = yaml.safe_load(f)
            return raw or {}, raw is None or _known_config(raw)
    return {}, True


def _load_yaml() -> dict:
    """Read a snapshot only; this does not replace live policy or grant proof."""
    return _load_snapshot()[0]


_CFG, _proof_known = _load_snapshot()
_publication = Authority("action-policy")


def publication_witness() -> Witness | None:
    """Issue the current memory epoch; never load policy or rearm authority.

    Capture before full action checks and validate these SAME tokens afterwards.
    Direct file/environment/global edits outside the setters are not attested.
    """
    token = _publication.token() if _proof_known else None
    return Witness((token,)) if token is not None else None


# The user's own access choice (POST /mode -> choose_mode), kept across restarts
# so "Ask before acting" stays chosen. policy.yaml is only the fresh-install
# default: a key saved here wins over it at startup. The bare setters below stay
# in-memory only (tests and scripts flip them freely); only choose_mode saves.
MODE_PATH = MOE_DIR / "access_mode.json"
_MODE_KEYS = frozenset({"read_only", "full_access"})


class _BadModeFile(Exception):
    """access_mode.json exists but isn't a valid saved choice."""


def _read_mode() -> dict[str, bool]:
    """The saved choice; {} when there is none. A file that is there but can't be
    trusted (a symlink, a FIFO or directory, another owner, an extra hard link,
    unreadable, malformed or with unknown keys) raises _BadModeFile."""
    try:
        # O_NONBLOCK: a FIFO planted here must not hang the service's start.
        fd = os.open(MODE_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except OSError as e:
        raise _BadModeFile(str(e)) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise _BadModeFile("not a regular file of this user's")
        with os.fdopen(fd, encoding="utf-8") as f:
            fd = -1
            data = json.load(f)
    except _BadModeFile:
        raise
    except Exception as e:  # noqa: BLE001 — unreadable or not JSON
        raise _BadModeFile(str(e)) from None
    finally:
        if fd >= 0:
            os.close(fd)
    if (type(data) is not dict or not data.keys() <= _MODE_KEYS
            or any(type(value) is not bool for value in data.values())):
        raise _BadModeFile("not a saved access choice")
    return data


def _saved_mode() -> dict[str, bool]:
    """The saved choice, or {} when it is missing or can't be trusted."""
    try:
        return _read_mode()
    except _BadModeFile:
        return {}


def _startup_mode() -> dict[str, bool]:
    """The saved choice laid over policy.yaml at import. A file that is there but
    bad fails closed: Full access off, whatever policy.yaml says, since it may
    have been the user's "off" that got damaged or replaced (0.5.0 #409 audit)."""
    try:
        return _read_mode()
    except _BadModeFile as e:
        import logging
        logging.getLogger("wisp.policy").warning(
            "access_mode.json can't be read (%s); Full access stays off until it is chosen again", e)
        return {"full_access": False}


def _save_mode(data: dict[str, bool]) -> None:
    MODE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = MODE_PATH.with_name(MODE_PATH.name + ".tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        with os.fdopen(os.open(tmp, flags, 0o600), "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)   # O_CREAT's mode skips a leftover tmp
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MODE_PATH)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


_STARTUP_MODE = _startup_mode()

# View-only mode: the app may READ/inspect anything but never modify the machine.
# Default ON — a local assistant shouldn't be able to change things unless you opt in.
_READ_ONLY = _STARTUP_MODE.get("read_only", bool(_CFG.get("read_only", True)))


def read_only() -> bool:
    return _READ_ONLY


def set_read_only(value: bool) -> None:
    global _READ_ONLY
    with revocation():
        _publication.withdraw()
        _READ_ONLY = bool(value)
        if _proof_known:
            _publication.publish()


# Full-access mode: every action auto-runs with NO confirmation. Overrides
# read_only. The catastrophic-command floor (_SHELL_DENY) is still enforced
# unless `disable_safety_floor: true` is also set — that floor guards against the
# model being prompt-injected into something irreversible, not against you.
_FULL_ACCESS = _STARTUP_MODE.get("full_access", bool(_CFG.get("full_access", False)))
_KEEP_FLOOR = not bool(_CFG.get("disable_safety_floor", False))
if _proof_known:
    _publication.publish()


def full_access() -> bool:
    return _FULL_ACCESS


def set_full_access(value: bool) -> None:
    global _FULL_ACCESS
    with revocation():
        _publication.withdraw()
        _FULL_ACCESS = bool(value)
        if _proof_known:
            _publication.publish()


def choose_mode(chosen: dict[str, bool]) -> None:
    """The user's choice (POST /mode): apply it now and save it for the next start.

    Each key goes through set_read_only / set_full_access, so it revokes and
    republishes exactly as they do; the save happens under the same lease, so
    two choices can't leave memory and the file disagreeing. The change stands
    even if saving fails (turning Full access off must take effect now) and the
    OSError is raised, so the caller can say it won't survive a restart.
    """
    if not chosen.keys() <= _MODE_KEYS:
        raise ValueError(f"unknown access mode keys: {sorted(chosen.keys() - _MODE_KEYS)}")
    chosen = {key: bool(value) for key, value in chosen.items()}
    if not chosen:
        return
    with revocation():
        if "read_only" in chosen:
            set_read_only(chosen["read_only"])
        if "full_access" in chosen:
            set_full_access(chosen["full_access"])
        # Both keys, as they are in force now, every time. Saving only the key that was chosen
        # left the other to policy.yaml's default at the next start: after a damaged file had
        # forced Full access off, choosing view-only alone would have brought Full access back.
        _save_mode({"read_only": bool(_READ_ONLY), "full_access": bool(_FULL_ACCESS)})


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
    rules = _rules("destructive_shell", _DESTRUCTIVE_SHELL)
    if (hit := _any(rules, cmd)):
        return hit
    # The patterns stop at a `;`. One that is quoted or escaped is a word, not
    # the end of the command (`find ';' build -delete`), so it is read through.
    plain = _QUOTED_SEMICOLON.sub(" _ ", cmd) if isinstance(cmd, str) else cmd
    return _any(rules, plain) if plain != cmd else None


_QUOTED_SEMICOLON = re.compile(r"\\;|';'|\";\"")

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

# Tools from an installed skill or a configured MCP server: what they do is
# decided by text Wisp cannot inspect (OPAQUE_ACTION_CATEGORIES), so neither
# full access nor a standing grant covers them, on either route. An MCP tool
# the user listed in its server's `trusted_read_only` is category mcp_read and
# is not covered here (service/mcp).
_ALWAYS_CONFIRM_EXTENSION = {"mcp_action", "skill_tool"}

# manage_contacts (create, add_phone, add_email). Always confirmed, in every
# access mode, and never grantable (grants._NEVER_GRANTABLE). A contact card is
# not a private note: it is the address book every later send resolves
# through. lookup_contact turns "text Mom" into whatever number Mom's card
# holds, and the ContactsReader sync turns an incoming number back into a
# name. So one wrong add_phone (the 1B #33 "this is my mom btw" shape, aimed at
# the wrong number) silently redirects every future message to "Mom" to a
# stranger, and labels the stranger's texts as hers, with nothing at send time
# to suggest anything changed. Same lesson as _ALWAYS_CONFIRM_CALENDAR's
# 2026-08-18 incident: a write whose damage only shows up later, somewhere
# else, needs a human to see the exact change before it happens, whatever mode
# Wisp is in. Before this it had no rule of its own and fell to "unclassified
# action": confirmed by default, but run unprompted under full access, and an
# "always allow" for it could be stored. The tool has no read actions to
# exempt (lookup_contact and list_contacts are the reads, category
# messages_read), and an unknown action is a no-op error, so every call asks.
_ALWAYS_CONFIRM_CONTACTS = {"messages_write"}


# EVERY category that must be confirmed on every call. One definition, read by
# both decide() (a standing grant cannot lift these) and grants (an allow rule
# cannot be stored for, or honoured on, a tool in one of these). Before this
# existed grantability was a hand-kept list of tool names that missed
# update_event, clear_reminders, clear_past_reminders, forward_email and
# http_request (audit H-1), and decide() consulted grants against a different,
# smaller set. A new tool in any of these categories is covered automatically.
ALWAYS_CONFIRM_CATEGORIES = frozenset(
    _ALWAYS_CONFIRM | _ALWAYS_CONFIRM_OUTBOUND | _ALWAYS_CONFIRM_CALENDAR
    | _ALWAYS_CONFIRM_TOOL_AUTHORING | _ALWAYS_CONFIRM_EXTENSION | _ALWAYS_CONFIRM_CONTACTS)

# toggle_setting calls that switch something OFF the user may be relying on:
# the network, wireless devices, a protection or an accessibility aid. Confirmed
# in every mode, and a standing grant cannot lift them (grants.is_grantable).
# Keyed on the call, not the category: turning the same setting ON stays on the
# ordinary system_write rules.
#
# WHY THIS EXISTS (2026-11-01 toolbench gate). Under Full access the model read
# an ambiguous "turn everything off" as toggle_setting(wifi, on=false) and ran
# it with no card. Wi-Fi off can drop the very connection the user is talking
# to Wisp over (the iPhone app, a remote session); Bluetooth off can take the
# keyboard and mouse with it. Fails closed: any setting not listed as harmless
# to switch off, and any `on` that is not exactly true, asks.
_TOGGLE_OFF_CUTS = {"wifi": "Wi-Fi", "bluetooth": "Bluetooth", "firewall": "the firewall",
                    "airdrop": "AirDrop", "voiceover": "VoiceOver"}
# Switching these off only changes the look, or brings back notifications and
# full performance: nothing is disconnected or left unprotected.
_TOGGLE_OFF_HARMLESS = frozenset({"dark_mode", "auto_appearance", "low_power_mode",
                                  "do_not_disturb"})


def toggle_off_hit(tool: str | None, args: dict | None) -> str | None:
    """What this toggle_setting call would cut off (for the card), or None."""
    if tool != "toggle_setting" or not isinstance(args, dict):
        return None
    if args.get("on") is True:
        return None
    from service.tools.system_extras import setting_key
    key = setting_key(str(args.get("setting", "")))
    if key in _TOGGLE_OFF_HARMLESS:
        return None
    return _TOGGLE_OFF_CUTS.get(key, f"the {key or 'unnamed'} setting")


# Tools confirmed on every call, in every mode, whatever their category says.
# run_applescript is category "shell", but its text drives other apps: one line
# sends an iMessage or a Mail message as the user, and `do shell script` is a
# full shell. Full access and standing grants do not cover it (grants.py
# refuses to store an allow for it).
ALWAYS_CONFIRM_TOOLS = frozenset({"run_applescript"})
# Writes onto this Mac a file somebody else sent. Its category is fs_write (so the
# path floor, the credential check and view-only apply), but fs_write runs
# unasked under full access; this file is confirmed on a card naming it in every
# mode, and never grantable (grants._NEVER_GRANTABLE).
ALWAYS_CONFIRM_FILE_TOOLS = frozenset({"save_email_attachment"})
# Deletes a note. Its category is notes_write (view-only and the ordinary note
# rules apply), but notes_write runs unasked under full access; a delete is
# confirmed on a card naming the note and its folder in every mode, and never
# grantable (grants._NEVER_GRANTABLE).
ALWAYS_CONFIRM_DELETE_TOOLS = frozenset({"delete_note"})


# Where keys, tokens and Wisp's own grants live. Reading one puts a secret in
# the model's context; writing one can rewrite who Wisp trusts (access_mode.json
# would turn Full access on at the next start). Either asks first in every mode
# (view-only refuses), and a grant cannot lift it.
def _credential_roots() -> list[Path]:
    from service.paths import MOE_DIR
    home = Path.home()
    roots = [home / ".ssh", home / ".aws", home / ".gnupg", home / ".netrc",
             home / ".config" / "gh", home / ".config" / "gcloud", home / "Library" / "Keychains"]
    for moe in dict.fromkeys((MOE_DIR, home / ".moe")):
        roots += [moe / "gmail.json", moe / "mcp.json", moe / "google_workspace",
                  moe / "grants.json", moe / "access_mode.json",
                  # "Let Wisp act for me" (service/proactive/acting.py): writing it would give, or
                  # backdate, the person's consent for Wisp to act without asking.
                  moe / "act_for_me.json",
                  # "Not now" for the same question: it can turn nothing on, only stop the
                  # question being shown, and is kept with the gate for one rule.
                  moe / "act_for_me_asked.json",
                  # /resetwisp's commit marker: its presence erases Wisp's data at the next start.
                  moe / ".reset-pending"]
    return roots


_SHELL_SECRET = [
    r"(?<![\w-])\.(?:ssh|aws|gnupg|netrc)(?![\w-])",
    r"\.config/(?:gh|gcloud)\b", r"\bkeychains?\b", r"\bsecurity\s+(?:find|dump|export)-",
    r"\b(?:gmail|mcp|grants|access_mode|act_for_me(?:_asked)?)\.json\b", r"\bgoogle_workspace\b", r"\.reset-pending\b",
]
# The ~/.moe folder itself (not a file in it): moving or replacing it swaps
# grants.json and access_mode.json without naming them (#409 audit). Any run
# of `/` and `/.` after it still names the folder: `~/.moe/`, `~/.moe/.`,
# `~/.moe//`, `~/.moe/./` (#415 audit).
_SHELL_MOE_FOLDER = r"(?<![\w-])\.moe(?:/\.?)*(?=$|[\s'\";|&)])"  # one `/` per pass: no backtracking blowup
# A dot-name glob (`~/.m*`, `~/.mo?`, `~/.[m]oe`) that the shell may expand to it.
_SHELL_GLOB_WORD = re.compile(r"(?<![\w-])\.[^\s/'\";|&()<>]*[*?\[][^\s/'\";|&()<>]*")
# Listing a folder or counting what is in it reads names and sizes, never a
# file's contents, so `cd ~/.moe && ls` and `ls ~/.moe | wc -l` don't ask.
# Not sort or uniq: `sort -o FILE` and `uniq IN OUT` write a file.
_LISTING_ONLY = frozenset({"cd", "ls", "wc", "du", "stat", "pwd"})


def _only_lists(cmd: str) -> bool:
    """Every command in `cmd` lists or counts (_LISTING_ONLY): no substitution,
    redirect, background `&` or subshell, and each `&&`/`||`/`;`/`|` part
    starts with one of those programs. Quoting is not parsed: a quoted
    separator just makes a part fail, which asks."""
    if re.search(r"[`$<>(){}\n\\]|(?<!&)&(?!&)", cmd):
        return False
    for part in re.split(r"&&|\|\||[;|]", cmd):
        words = part.split()
        if not words or words[0] not in _LISTING_ONLY:
            return False
    return True


def _moe_folder_hit(text: str) -> str | None:
    """Where `text` names Wisp's data folder itself, spelled out or as a glob."""
    if (m := re.search(_SHELL_MOE_FOLDER, text, re.IGNORECASE)):
        return " ".join(m.group(0).split())[:60]
    for m in _SHELL_GLOB_WORD.finditer(text):
        if fnmatch.fnmatchcase(".moe", m.group(0).casefold()):
            return m.group(0)[:60]
    return None


def _within(path: str, roots: list[str]) -> bool:
    p = path.rstrip("/").casefold()       # APFS is case-insensitive by default
    return any(p == r or p.startswith(r + "/") for r in roots)


def _wisp_folders() -> list[str]:
    """Wisp's data folder itself, in _within's spelling. (The folders above it
    are home and /Users: a move into ~ is ordinary, and moving those is not.)"""
    from service.paths import MOE_DIR
    found: list[str] = []
    for folder in dict.fromkeys((MOE_DIR, Path.home() / ".moe")):
        found.append(str(folder).rstrip("/").casefold())
        try:
            found.append(str(folder.resolve()).rstrip("/").casefold())
        except (OSError, RuntimeError):
            pass
    return found


def credential_path_hit(args: dict, tool: str | None = None, *, mutating: bool = False) -> str | None:
    """The credential location a file tool's arguments name, or None. A change
    (`mutating`) aimed at Wisp's data folder itself counts too:
    moving ~/.moe aside and another folder into its place replaces grants.json
    and access_mode.json without naming either (#409 audit)."""
    operands = _path_operands(args, tool)
    if not operands:
        return None
    if mutating:
        folders = set(_wisp_folders())
        for _, operand in operands:
            try:
                forms = _path_forms(operand)
            except (ValueError, OSError, RuntimeError):
                return operand
            if any(f.rstrip("/").casefold() in folders for f in forms):
                return operand
    roots: list[str] = []
    for root in _credential_roots():
        roots.append(str(root).rstrip("/").casefold())
        try:
            roots.append(str(root.resolve()).rstrip("/").casefold())
        except (OSError, RuntimeError):
            pass
    for _, operand in operands:
        try:
            forms = _path_forms(operand)
        except (ValueError, OSError, RuntimeError):
            return operand            # cannot tell where it points: ask
        if any(_within(form, roots) for form in forms):
            return operand
    return None


def _grep_file_operands(cmd: str) -> str:
    """A plain grep's file operands, without its search pattern: searching a
    README for the words "mcp.json" reads the README, not mcp.json."""
    words, files, pattern_given, skip = shlex.split(cmd)[1:], [], False, False
    for word in words:
        if skip:
            skip = False
        elif word in ("-e", "-f", "--regexp", "--file"):
            pattern_given = skip = True
            if word in ("-f", "--file"):
                skip = False          # -f names a file grep reads: judge it
        elif word.startswith(("-e", "--regexp=")):
            pattern_given = True
        elif word.startswith("-") and word != "-":
            continue
        elif not pattern_given:
            pattern_given = True      # the first operand is the pattern
        else:
            files.append(word)
    return " ".join(files)


# The protected files inside Wisp's own folder, by name. A wildcard that the shell could
# expand to one of them names it just as surely as spelling it out.
_MOE_PROTECTED_NAMES = ("gmail.json", "mcp.json", "grants.json", "access_mode.json", "act_for_me.json",
                        "act_for_me_asked.json",
                        ".reset-pending", "google_workspace")
_SHELL_WORD_BREAK = re.compile(r"[\s'\";|&()<>`]+")
# Shell settings under which `*`, `?` and `[...]` also match a leading dot: bash's dotglob
# (and GLOBIGNORE, which turns dotglob on), zsh's GLOB_DOTS (any case, any underscores)
# and its per-pattern `(D)` qualifier, as in `*(D)` or `*(.D)`.
_SHELL_DOTGLOB = re.compile(r"(?i:dotglob|glob_*dots|globignore)|[*?\]]\((?:#q)?[^()\s]*D")
_GLOB_BRACE = re.compile(r"\{([^{}]*)\}")
_GLOB_SEQUENCE = re.compile(r"^(-?\d+|[A-Za-z])\.\.(-?\d+|[A-Za-z])(?:\.\.-?\d+)?$")
_GLOB_EXPANSIONS = 256          # brace alternatives looked at per word
_GLOB_BUDGET = 100_000          # characters of brace expansion worked through per command
_GLOB_WORD_CHARS = 4096         # a longer word is not expanded: judged by what it could name
_GLOB_SEGMENT_CHARS = 1024      # a longer pattern segment is assumed to match
_GLOB_BARE_WORDS = 1024         # distinct bare wildcards judged per command; more asks


def _brace_options(word: str, budget: int) -> tuple[list[str], int] | None:
    """`word` with `{a,b}` and `{a..c}` brace expansion applied over the whole word, as
    the shell does before globbing, and the characters of work that took. None when it
    grows past _GLOB_EXPANSIONS alternatives or `budget` characters (the caller then
    judges the word by what it could name). A `{x}` with no comma is literal to the
    shell; here it is opened too, which can only find more."""
    options, done, spent = [word], [], 0
    while options:
        option = options.pop()
        spent += len(option)
        if spent > budget:
            return None
        found = _GLOB_BRACE.search(option)          # innermost group: no nested braces inside
        if not found:
            done.append(option)
        else:
            inner, parts = found.group(1), None
            if (seq := _GLOB_SEQUENCE.match(inner)):
                lo, hi = seq.group(1), seq.group(2)
                if lo.lstrip("-").isdigit() and hi.lstrip("-").isdigit():
                    a, b = int(lo), int(hi)
                    if abs(b - a) >= _GLOB_EXPANSIONS:
                        return None
                    parts = [str(n) for n in range(min(a, b), max(a, b) + 1)]
                elif lo.isalpha() and hi.isalpha():
                    a, b = sorted((ord(lo), ord(hi)))
                    parts = [chr(n) for n in range(a, b + 1)]       # A..z also covers [\]^_`
            if parts is None:
                parts = inner.split(",")
            options += [option[:found.start()] + part + option[found.end():] for part in parts]
        if len(options) + len(done) > _GLOB_EXPANSIONS:
            return None
    return done, spent


_GLOB_ANY = "\x00"             # stands for text this check could not read: it may be anything
_GLOB_GROUP = re.compile(r"\([^()]*\)")        # innermost first; spaces and quotes inside included


def _glob_brackets(segment: str) -> str:
    """`segment` with every bracket expression replaced by `?`. fnmatch and the shell read
    brackets differently (`[^x]` negates in bash and is literal in fnmatch; fnmatch knows no
    `[[:alpha:]]`, `[[.x.]]` or `[[=x=]]`), so a bracket is taken to match any one character,
    which can only find more. A `[` that never closes is literal, as in the shell."""
    out, i, n = [], 0, len(segment)
    while i < n:
        if segment[i] != "[":
            out.append(segment[i])
            i += 1
            continue
        j = i + 1
        if j < n and segment[j] in "!^":
            j += 1
        if j < n and segment[j] == "]":
            j += 1                                   # a `]` first is a member, not the end
        while j < n and segment[j] != "]":
            if segment[j] == "[" and j + 1 < n and segment[j + 1] in ":.=":
                close = segment.find(segment[j + 1] + "]", j + 2)
                if close < 0:
                    break
                j = close + 2
            else:
                j += 1
        if j < n and segment[j] == "]":
            out.append("?")
            i = j + 1
        else:
            out.append("[[]")                        # literal `[`, spelled so fnmatch agrees
            i += 1
    return "".join(out)


def _glob_segment_matches(name: str, segment: str, dotglob: bool) -> bool:
    """Whether one path segment of a glob (already casefolded) can match the file or folder
    called `name` (casefolded). Like the shell, a name that starts with `.` is matched only
    by a segment that starts with `.` too, unless dotglob is on or the segment's start is
    text this check could not read (_GLOB_ANY: a brace group cut by quoting)."""
    import fnmatch
    if len(segment) > _GLOB_SEGMENT_CHARS:
        return True
    loose = segment.startswith(_GLOB_ANY)
    if name.startswith(".") and not dotglob and not loose and not segment.startswith("."):
        return False
    return fnmatch.fnmatchcase(name, _glob_brackets(segment).replace(_GLOB_ANY, "*"))


def _glob_unbalanced(word: str) -> str:
    """A word whose braces do not pair up is a piece of a brace group that quoting or an
    escaped separator split (`x.{json,"a b"}` arrives as `x.{json,` then `a`, `b}`). What
    the missing part held is unknown, so it becomes _GLOB_ANY: everything from the first
    unpaired `{` on, and everything up to the last unpaired `}`."""
    opens, last_close = [], -1
    for index, char in enumerate(word):
        if char == "{":
            opens.append(index)
        elif char == "}":
            if opens:
                opens.pop()
            else:
                last_close = index
    if opens:
        word = word[:opens[0]] + _GLOB_ANY
    if last_close >= 0:
        word = _GLOB_ANY + word[last_close + 1:]
    return word


def _moe_glob_hit(form: str) -> str | None:
    """A wildcard that can expand to a protected file in Wisp's folder.

    - a wildcard in the file name: `~/.moe/act_for*.json`, `~/.moe/*.json`, `~/.moe/a[c]t*`;
    - a wildcard in the FOLDER name too: `~/.m?e/act*`, `~/.*/gra*`;
    - braces, anywhere in the word: `~/.moe/{grants,act_for_me}.json`, `~/.{moe,x}/act*`,
      also when quoting cut the group in pieces (`act_for_me.{json,"a b"}`);
    - through `..`: `~/.moe/cache/../act_for_me.js?n` (each spelling is normalized);
    - a relative wildcard after the folder was named elsewhere in the command, on ANY of its
      segments: `cd ~/.moe && cat act*`, `cd ~/.moe/cache && cp f ../act_for_m?.json`,
      `cd ~/.moe/cache && cp f ../goog*/token.json`. `~+/…` and `~-/…` (the current and the
      previous folder) are relative too;
    - a parenthesised group glued into a word, which zsh reads as alternatives with no option
      set: `~/.moe/(act_for_me|x).json`, `act_for_me.js(o)n`. What it holds is not read: it
      counts as any text. (bash's extglob `@(a|b)` and zsh's extendedglob operators need an
      option turned on first; they are covered only as far as this same rule reaches.)
    Case does not matter (APFS is case-insensitive by default: `~/.MOE/act*` is the same
    folder). Every `[...]` is read as "any one character". As in the shell, `*` and `?` do
    not match a leading dot unless the command turns on dotglob/GLOB_DOTS, so `cat */*.json`
    and `rm -rf ~/.cache/*/*` cannot reach ~/.moe and are untouched; a custom Wisp folder
    (WISP_HOME) whose name has no dot can be matched by any wildcard. A word too large to
    expand asks whenever the folder is named. Errs toward a hit: a hit means "ask first",
    never "refuse".

    This reads the command's TEXT. It cannot see a name built at run time (a variable, an
    interpreter, `$'\\x2e'`, find's own matching): it is not what keeps the files safe by
    itself, only what makes the plain spellings ask."""
    dotglob = bool(_SHELL_DOTGLOB.search(form))           # before casefolding: zsh's (D) is upper case
    form = form.casefold()
    # Two readings, and either one asks. First the text as it stands, where parentheses break
    # words: a subshell or `$(…)` is then read through, `(cat>~/.moe/act*)`. Then with every
    # group in parentheses taken as "any text" glued into its word, which is how zsh reads
    # `~/.moe/(act_for_me| x).json` (alternatives; spaces and quotes allowed inside). The
    # second reading alone would hide what is inside a subshell, so it never replaces the first.
    if (hit := _moe_glob_pass(form, dotglob)) is not None:
        return hit
    grouped = form
    for _ in range(8):                                    # nested groups, innermost first
        grouped, found = _GLOB_GROUP.subn(_GLOB_ANY, grouped)
        if not found:
            break
    return _moe_glob_pass(grouped, dotglob) if grouped != form else None


def _moe_glob_pass(form: str, dotglob: bool) -> str | None:
    """One reading of a casefolded command for _moe_glob_hit (see there)."""
    import posixpath
    from service.paths import MOE_DIR
    hit = "a protected Wisp file (wildcard)"
    folders = tuple(dict.fromkeys((".moe", str(MOE_DIR).rstrip("/").rsplit("/", 1)[-1].casefold())))
    names = tuple(name.casefold() for name in _MOE_PROTECTED_NAMES)

    def is_folder(segment: str) -> bool:
        return any(_glob_segment_matches(folder, segment, dotglob) for folder in folders)

    def is_protected(segment: str) -> bool:
        return any(_glob_segment_matches(name, segment, dotglob) for name in names)

    budget, names_folder, skipped, tails = _GLOB_BUDGET, False, False, set()
    for word in _SHELL_WORD_BREAK.split(form):
        if not any(mark in word for mark in "*?[{}" + _GLOB_ANY):
            # A word with no wildcard can still name the folder for a later relative wildcard.
            names_folder = names_folder or any(part in folders for part in word.split("/"))
            continue
        word = _glob_unbalanced(word)
        expanded = _brace_options(word, budget) if budget > 0 and len(word) <= _GLOB_WORD_CHARS else None
        if expanded is None:
            # Too big to expand. It asks if it could itself be the folder, or (at the end) if
            # the folder is named anywhere in the command.
            # (A `..` is a brace range, `{0..300}`, which can never produce a dot.)
            if dotglob or "." in word.replace("..", "") or _GLOB_ANY in word or any(not f.startswith(".") for f in folders):
                return hit
            skipped = True
            budget -= len(word)
            continue
        options, spent = expanded
        budget -= spent
        for option in options:
            # As written, and with `..` resolved: either spelling may be the one that lands.
            for spelling in dict.fromkeys((option, posixpath.normpath(option))):
                parts = [part for part in spelling.split("/") if part not in ("", ".")]
                for index, part in enumerate(parts):
                    if part != ".." and is_folder(part):
                        # A segment that is ALL unread text (`{print` in an awk program) does not
                        # by itself name the folder; right before a protected name it still asks.
                        names_folder = names_folder or part.strip(_GLOB_ANY) != ""
                        if index + 1 < len(parts) and is_protected(parts[index + 1]):
                            return hit
                # A relative path lands wherever the command stands, which may be the folder or
                # a folder inside it: any of its segments may be the protected name. `~/…` and
                # `/…` are absolute; `~+/…`, `~-/…` and anything else are not.
                if parts and not spelling.startswith(("/", "~/")) and spelling != "~":
                    for part in parts:
                        if part == "..":
                            continue
                        if len(tails) < _GLOB_BARE_WORDS:
                            tails.add(part)
                        else:
                            skipped = True
    if names_folder and (skipped or any(is_protected(part) for part in tails)):
        return hit                                         # a relative wildcard, the folder named elsewhere
    return None


def shell_secret_hit(args: dict) -> str | None:
    """The credential location a shell-category call names, or None."""
    cmd = str(args.get("cmd") or "")
    folder = not (not args.get("script") and _only_lists(cmd))

    def hit_in(form: str) -> str | None:
        return (_first_match(_SHELL_SECRET, form) or _moe_glob_hit(form)
                or (_moe_folder_hit(form) if folder else None))

    if not args.get("script") and len(cmd) <= SHELL_TEXT_LIMIT:
        if read_only_shell_argv(cmd) is not None:
            return None               # validated reads never reach hidden paths or ~/Library
        if _cannot_reach_network(args):
            cmd = _grep_file_operands(cmd)
            return next((hit for form in _quote_forms(cmd) if (hit := hit_in(form))), None)
    return next((hit for form in shell_forms(args) if (hit := hit_in(form))), None)


# First words that make a scoped "always allow" a blanket one: an interpreter
# or launcher runs whatever follows it, so a grant on `python3` would cover
# every future python3 program.
_UNGRANTABLE_SHELL_PROGRAMS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "osascript", "osacompile",
    "node", "deno", "bun", "npx", "ruby", "perl", "php", "lua", "tclsh", "swift",
    "uv", "uvx", "pipx", "curl", "wget", "open", "env", "xargs", "exec", "command",
    "nohup", "time", "nice", "eval", "source", ".", "find", "awk", "make", "shortcuts",
    "automator", "sudo", "doas", "su",
})


def shell_grant_problem(args: dict) -> str | None:
    """Why a standing "always allow" may not cover this shell-category call.

    A run_shell grant is scoped to the command's first word, so it is only
    honest for a single plain program: not a pipeline or compound command
    (`ls && anything`), not an interpreter or launcher, and never anything
    that is confirmed on every call anyway."""
    if args.get("script"):
        return "AppleScript is confirmed every time"
    cmd = str(args.get("cmd") or "")
    words = cmd.split()
    if not words:
        return "empty command"
    if re.search(r"[\x00-\x1f\x7f$`|&;<>(){}\\]", cmd):
        return "compound commands are confirmed every time"
    first = words[0].rsplit("/", 1)[-1].casefold()
    if "=" in words[0]:
        return "commands that set variables are confirmed every time"
    if first in _UNGRANTABLE_SHELL_PROGRAMS or re.fullmatch(r"python[\d.]*|pip[\d.]*", first):
        return f"{first} runs other programs, so it is confirmed every time"
    if is_destructive_shell(cmd):
        return "permanent deletes are confirmed every time"
    if (hit := shell_egress_hit(args) or shell_secret_hit(args)):
        return f"commands that can send data or read secrets are confirmed every time ({hit})"
    return None


# MCP actions under full access: an allowlist, not a list of write verbs.
# Write verbs never run out (patch, fork, rerun, dismiss, close, exercise...),
# so an mcp_action runs without a card only when its own tool name, without
# the mcp_<server>_ prefix, STARTS with a read verb and holds no effect word
# anywhere. Everything else asks. The name is the server's own claim, so this
# only ever decides between asking and not; trusted_read_only and the
# readOnlyHint/destructiveHint handling (service.mcp._category_for) decide
# mcp_read separately and are unchanged. Names are judged in their
# registered form: lowercased, with every non-alphanumeric made `_`.
# Not "check": check_task and check_item mean "mark done".
_MCP_READ_VERBS = frozenset({"get", "list", "search", "read", "fetch", "query", "find", "view",
                             "inspect", "describe", "lookup", "count", "show", "preview",
                             "render", "retrieve"})
# Backstop for names that start with a read verb: any of these, as a whole
# word, means it asks (search_and_post, query_post, get_and_delete...).
_MCP_EFFECT_WORDS = frozenset({
    "send", "delete", "remove", "trash", "create", "update", "modify", "share", "publish",
    "upload", "forward", "reply", "invite", "write", "transfer", "purchase", "cancel", "archive",
    "comment", "merge", "submit", "execute", "insert", "notify", "manage", "append", "replace",
    "resize", "rename", "restore", "export", "draft", "post", "push", "run", "edit", "save", "add",
    "set", "put", "book", "pay", "order", "move", "copy", "import", "clear", "format", "batch",
    "label", "start", "apply", "complete", "respond", "mark", "patch", "fork", "star", "rerun",
    "assign", "dismiss", "close", "duplicate", "request", "exercise", "trade", "buy", "sell",
    "approve", "subscribe", "unsubscribe", "enable", "disable", "toggle", "trigger",
    "drop", "purge", "revoke", "install", "uninstall",
    # A name that joins two actions (get_and_purge, fetch_then_install):
    # the read verb in front says nothing about the second one.
    "and", "then", "or",
})
# And as a substring of a squashed word (getandsend, readthensubmit).
_MCP_EFFECT_STEMS = ("send", "delete", "remove", "create", "update", "modify", "upload",
                     "forward", "transfer", "purchase", "submit", "execute", "insert", "publish",
                     "write", "archive", "share", "reply", "cancel", "merge", "export", "draft",
                     "replace", "rename")


def _mcp_tool_part(tool: str) -> str | None:
    """The tool's own name, without its mcp_<server>_ prefix, as registered.
    None for an mcp_ name whose server boundary is unknown: it then asks."""
    name = str(tool or "").lower()
    try:
        from service.mcp import TOOL_PARTS
        if (part := TOOL_PARTS.get(name)) is not None:
            return part
    except ImportError:
        pass
    return None if name.startswith("mcp_") else name


def mcp_effect_word(tool: str | None) -> str | None:
    """Why an mcp_action needs a card even under full access, or None when its
    name plainly reads (see above)."""
    part = _mcp_tool_part(str(tool or ""))
    if part is None:
        return "change things (its name does not say it only reads)"
    words = [w for w in re.split(r"[_\-.]+", part) if w]
    if not words or words[0] not in _MCP_READ_VERBS:
        return "change things (its name does not start with get, list, search or read)"
    if words == ["query"]:
        return "run any query it is given (a bare query tool can write)"
    if (word := next((w for w in words[1:] if w in _MCP_EFFECT_WORDS), None)):
        return word
    if (stem := next((s for s in _MCP_EFFECT_STEMS if any(s in w for w in words)), None)):
        return stem
    return None


_SAVE_BUNDLE_SUFFIXES = (".app", ".bundle", ".framework", ".plugin", ".kext", ".xpc", ".appex",
                         ".photoslibrary")


def _save_folder_problem(folder) -> str | None:
    """Why a folder for a saved attachment is refused, or None. Mirrors the save
    engine: an existing folder in the user's home, outside ~/Library and hidden
    folders. Empty means Downloads. Judged on the spelling and on where it
    really points, so a link cannot lead out."""
    if folder in (None, ""):
        return None
    if not isinstance(folder, str) or "\x00" in folder:
        return "invalid folder for a saved file"
    try:
        home = Path(os.path.realpath(Path.home()))
        typed = Path(os.path.expanduser(folder))
        if not typed.is_absolute():
            typed = Path.home() / typed
        for candidate in (Path(os.path.normpath(typed)), Path(os.path.realpath(typed))):
            for base in (Path.home(), home):
                try:
                    parts = candidate.relative_to(base).parts
                    break
                except ValueError:
                    continue
            else:
                return "a saved file goes in a folder inside your home folder"
            # Compared the way the save engine does: case-folded and NFC, since
            # on the usual case-insensitive volume ~/library IS ~/Library.
            parts = tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)
            if parts and parts[0] in ("library", "applications"):
                return "a saved file may not go into ~/Library or ~/Applications"
            if any(part.startswith(".") for part in parts):
                return "a saved file may not go into a hidden folder"
            if any(part.endswith(_SAVE_BUNDLE_SUFFIXES) for part in parts):
                return "a saved file may not go into an application bundle"
    except (ValueError, OSError, RuntimeError):
        return "that folder could not be safely resolved"
    return None


# --- text the floor does not read (rc2 fix) -------------------------------------
# _SHELL_DENY reads raw text, so a commit message or a note that only MENTIONS
# a refused command (`git commit -m "stop the reboot loop"`) was refused too.
# Rather than teach the floor shell quoting, a few WHOLE-command shapes whose
# quoted text is only ever data have that text blanked before the floor reads
# the rest:
#   git commit [flags] -m '<text>' [-m '<text>' ...]
#   echo [-n|-e] '<text>' >|>> <notes file>
#   cat >|>> <notes file> <<'EOF' ... EOF        (a quoted delimiter only)
#   git commit [flags] -F - <<'EOF' ... EOF
# each optionally after `&&` steps from a short list that writes no file,
# makes no link and stays in the working folder (git add/status/diff/log/show
# with plain words and no `=`, `mkdir [-p] <plain relative folder>`; before a
# git commit also `cd <plain folder>`, since a commit writes no file the text
# names). The floor still reads those steps. Any other step (ln, cp, mv, cd
# before a notes file, touch, ...) and the whole line is read as before.
# Double-quoted text may hold nothing the shell expands ($ ` \ !). A notes
# file is a relative path ending .txt/.md/.log/.csv/..., with no dot-folder
# and no `..`: never a script, a startup file or a launch agent. It is also
# looked up on disk (run_shell runs in home) when the call is judged: no part
# of it may be a link, an existing file must be one ordinary file with one
# name (no hard link), and it must stay inside home and out of LaunchAgents,
# LaunchDaemons, StartupItems, the keychains and Wisp's own data. A file not
# yet there, in real folders, is fine. Anything else, and every way of running
# text (sh -c, eval, xargs, a pipe, a heredoc into a shell, $(...)), is read
# as before. The never-run check reads the original text in every case.
_INERT_WORD = re.compile(r"[A-Za-z0-9._+@/=:,%~-]+")
_INERT_DQ = re.compile(r'[^"$`\\!]*')
_INERT_TARGET = re.compile(r"[A-Za-z0-9_+@][A-Za-z0-9._+@/-]*\.(?:txt|md|markdown|log|csv|tsv|rst|org)",
                           re.IGNORECASE)
_INERT_HEREDOC = re.compile(r"<<(?:'([A-Za-z_][A-Za-z0-9_]*)'|\"([A-Za-z_][A-Za-z0-9_]*)\")")
_INERT_LIT = "'_'"


def _inert_tokens(line: str) -> list[tuple[str, str]] | None:
    """('word'|'lit'|'op'|'here', text) tokens, or None for anything else."""
    tokens, i, n = [], 0, len(line)
    while i < n:
        c = line[i]
        if c in " \t":
            i += 1
            continue
        if c == "'":
            end = line.find("'", i + 1)
            if end < 0:
                return None
            tokens.append(("lit", line[i:end + 1]))
            i = end + 1
        elif c == '"':
            end = line.find('"', i + 1)
            if end < 0 or not _INERT_DQ.fullmatch(line[i + 1:end]):
                return None
            tokens.append(("lit", line[i:end + 1]))
            i = end + 1
        elif line.startswith("&&", i):
            tokens.append(("op", "&&"))
            i += 2
        elif line.startswith(">>", i):
            tokens.append(("op", ">>"))
            i += 2
        elif line.startswith("<<", i):
            m = _INERT_HEREDOC.match(line, i)
            if m is None:
                return None
            tokens.append(("here", m.group(1) or m.group(2)))
            i = m.end()
        elif c == ">":
            tokens.append(("op", ">"))
            i += 1
        else:
            m = _INERT_WORD.match(line, i)
            if m is None:
                return None
            tokens.append(("word", m.group()))
            i = m.end()
        if i < n and tokens[-1][0] in ("lit", "here") and line[i] not in " \t":
            return None                     # nothing glued onto quoted text
    return tokens


def _inert_target(word: str) -> bool:
    parts = word.split("/")
    return (bool(_INERT_TARGET.fullmatch(word))
            and not any(p in ("", "..") or p.startswith(".") for p in parts))


_INERT_GIT_READS = frozenset({"add", "status", "diff", "log", "show"})
_INERT_FOLDER = re.compile(r"[A-Za-z0-9_+@][A-Za-z0-9._+@-]*(?:/[A-Za-z0-9_+@][A-Za-z0-9._+@-]*)*/?")
_INERT_NEVER_INTO = frozenset({"launchagents", "launchdaemons", "startupitems", "keychains"})


def _inert_plain_folder(word: str) -> bool:
    """A relative folder of plain names: no `..`, no dot-folder, no `~`, no `-`."""
    return bool(_INERT_FOLDER.fullmatch(word)) and ".." not in word.split("/")


def _inert_lead_ok(step: list[tuple[str, str]], commit: bool) -> bool:
    """Whether a step before a data shape is one that cannot write a file,
    make a link or move the working folder (see above)."""
    words = [text for _, text in step]
    if words[0] == "git":
        return (len(words) > 1 and words[1] in _INERT_GIT_READS
                and not any("=" in w or w.startswith("--output") for w in words[2:]))
    if words[0] == "mkdir":
        rest = words[1:]
        if rest[:1] == ["-p"]:
            rest = rest[1:]
        return bool(rest) and all(_inert_plain_folder(w) for w in rest)
    if words[0] == "cd" and commit:
        dest = words[1][2:] if len(words) == 2 and words[1].startswith("~/") else words[-1]
        return len(words) == 2 and _inert_plain_folder(dest)
    return False


def _inert_target_safe(rel: str) -> bool:
    """Whether the notes file `rel` (relative to home, where run_shell runs) is
    an ordinary file or not there yet, reached through real folders only, and
    stays inside home away from the folders _INERT_NEVER_INTO names."""
    home = _nr_home()
    parts = rel.split("/")
    if any(p.casefold() in _INERT_NEVER_INTO for p in parts):
        return False
    try:
        path = home
        for k, part in enumerate(parts):
            path = os.path.join(path, part)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                break                           # the rest is not there yet
            if stat.S_ISLNK(st.st_mode):
                return False
            if k < len(parts) - 1:
                if not stat.S_ISDIR(st.st_mode):
                    return False
            elif not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                return False                    # a hard link writes another file too
        real_home = os.path.realpath(home)
        real = os.path.realpath(os.path.join(home, rel))
    except (OSError, ValueError, RuntimeError):
        return False
    if not real.startswith(real_home.rstrip("/") + "/"):
        return False
    return not (_nr_protected_hit(os.path.join(home, rel)) or _nr_protected_hit(real))


def _inert_git_commit(words: list[tuple[str, str]], heredoc: bool) -> bool:
    if [t for _, t in words[:2]] != ["git", "commit"] or any(k == "word" and t == "--"
                                                             for k, t in words):
        return False
    rest, saw_message = words[2:], False
    for k, (kind, text) in enumerate(rest):
        if kind == "lit":
            before = rest[k - 1][1] if k else ""
            if heredoc or not (before in ("-m", "--message") or re.fullmatch(r"-[A-Za-z]*m", before)):
                return False
            saw_message = True
        elif kind != "word" or not text.startswith("-") or "=" in text:
            return False
    if heredoc:
        return [t for _, t in rest[-2:]] == ["-F", "-"]
    return saw_message


def _inert_shape(tokens: list[tuple[str, str]], heredoc: bool) -> bool:
    """Whether the last step of a line is one of the shapes above (with a
    'here' token when `heredoc`), every earlier step is one _inert_lead_ok
    allows, and a notes file it writes passes _inert_target_safe."""
    steps, current = [], []
    for tok in tokens:
        if tok == ("op", "&&"):
            steps.append(current)
            current = []
        else:
            current.append(tok)
    steps.append(current)
    if any(not step or any(kind != "word" for kind, _ in step) for step in steps[:-1]):
        return False
    last = steps[-1]
    here = [tok for tok in last if tok[0] == "here"]
    if len(here) != (1 if heredoc else 0):
        return False
    body = [tok for tok in last if tok[0] != "here"]
    commit = bool(body) and body[0] == ("word", "git")
    if not all(_inert_lead_ok(step, commit) for step in steps[:-1]):
        return False
    if commit:
        return _inert_git_commit(body, heredoc)
    if heredoc:
        if not (len(body) == 3 and body[0] == ("word", "cat") and body[1][0] == "op"
                and body[1][1] in (">", ">>") and body[2][0] == "word" and _inert_target(body[2][1])):
            return False
        return _inert_target_safe(body[2][1])
    if not body or body[0] != ("word", "echo") or len(body) not in (4, 5):
        return False
    if len(body) == 5 and body[1] not in (("word", "-n"), ("word", "-e")):
        return False
    lit, op, target = body[-3], body[-2], body[-1]
    return (lit[0] == "lit" and op[0] == "op" and op[1] in (">", ">>")
            and target[0] == "word" and _inert_target(target[1]) and _inert_target_safe(target[1]))


def inert_text_form(cmd) -> str | None:
    """The command with its data-only quoted text blanked, when the whole
    command is one of the shapes above, else None."""
    if not isinstance(cmd, str) or len(cmd) > SHELL_TEXT_LIMIT:
        return None
    # The whole text as one line (a newline is allowed only inside quotes).
    tokens = _inert_tokens(cmd)
    if tokens is not None:
        if any(kind == "here" for kind, _ in tokens) or not _inert_shape(tokens, heredoc=False):
            return None
        return " ".join(_INERT_LIT if kind == "lit" else text for kind, text in tokens)
    # A heredoc: its first line is the command, then the body and the delimiter.
    first, newline, rest = cmd.partition("\n")
    tokens = _inert_tokens(first)
    if not newline or tokens is None:
        return None
    here = [text for kind, text in tokens if kind == "here"]
    if len(here) != 1 or any(kind == "lit" for kind, _ in tokens) or not _inert_shape(tokens, True):
        return None
    lines = rest.split("\n")
    try:
        end = lines.index(here[0])
    except ValueError:
        return None
    if any(line for line in lines[end + 1:]):
        return None                         # nothing may follow the heredoc
    shown = " ".join(f"<<'{text}'" if kind == "here" else text for kind, text in tokens)
    return f"{shown}\n_\n{here[0]}"


def _floor_forms(args: dict) -> tuple[str, ...]:
    """What the floor reads: shell_forms, with a data-only shape's quoted
    text blanked (see inert_text_form)."""
    if not args.get("script") and (inert := inert_text_form(args.get("cmd"))) is not None:
        return shell_forms({"cmd": inert})
    return shell_forms(args)


def _hard_deny(category: str, args: dict, tool: str | None = None) -> Decision | None:
    """The unconditional safety floor: rules nothing can override — not
    full_access, not a standing grant, not the user clicking "always allow".

    Split out of decide() so grants have something to be checked *after*. A
    grant that could lift one of these wouldn't be a permission model, it would
    be a bypass.
    """
    if category == "shell":
        # run_applescript's `do shell script` bodies and quote-stripped text too.
        rules = _rules("shell_deny", _SHELL_DENY)
        if plain_delete_paths(args) is not None:
            rules = [rule for rule in rules if rule != _SHELL_BROAD_RM]
        for cmd in _floor_forms(args):
            if (hit := _any(rules, cmd)):
                return Decision(Tier.DENY, "Wisp won't run this command (it matches a blocked "
                                f"pattern: {hit}). {NEVER_RUN_TAIL}")
    if tool in ALWAYS_CONFIRM_FILE_TOOLS and (problem := _save_folder_problem(args.get("folder"))):
        return Decision(Tier.DENY, problem)
    if category in ("fs_write", "fs_delete"):
        rules = _rules("path_deny", _PATH_DENY)
        # uninstall_app's `name` (an app basename joined onto each Applications
        # folder) was the one location not passed under a _PATH_ARG_KEYS name;
        # its special case went with the tool, retired in the consolidation.
        operands = _path_operands(args, tool)
        if any("\x00" in operand for _, operand in operands):
            return Decision(Tier.DENY, "invalid filesystem path: NUL character")
        if (tool == "organize_files" and isinstance(args.get("folder"), str)
                and isinstance(args.get("destination"), str)):
            # organize_files anchors relative destinations at its resolved
            # source folder, not home/cwd. Keep the generic spellings as well.
            try:
                destination = Path(args["destination"]).expanduser()
                if not destination.is_absolute():
                    source = Path(args["folder"]).expanduser().resolve()
                    operands.append(("destination", str(source / destination)))
            except (ValueError, OSError, RuntimeError):
                return Decision(Tier.DENY, "filesystem source path could not be safely resolved")
        try:
            # Check raw operands first, before deriving or listing anything.
            for key, operand in chain(operands, _derived_path_operands(tool, args)):
                if "\x00" in operand:
                    return Decision(Tier.DENY, "invalid derived filesystem path: NUL character")
                forms = _path_forms(operand)
                for form in forms:
                    if (hit := _any(rules, form)):
                        return Decision(Tier.DENY, f"protected path in {key}: {hit}")
        except (ValueError, OSError, RuntimeError):
            return Decision(Tier.DENY, "filesystem output path could not be safely resolved")
    return None


# --- commands Wisp never runs -------------------------------------------------
# Owner decision (2026-11-10, review finding b09-safety#9): a recursive delete
# of an absolute path, and any other very dangerous command, is refused
# outright instead of getting a card. Checked in every mode, before full
# access, grants and the configurable floor, and not configurable: no card and
# no "Always allow". The person can still run the command in Terminal.
#
# Unlike _SHELL_DENY this does not match one regex against the raw string. It
# lifts out $(...), `...` and <(...) bodies (each checked on its own), splits
# the line into simple commands at ; && || | & ( ) and newlines with shell
# quoting applied, strips wrappers (VAR=1, env, sudo, doas, nohup, time, nice,
# command, exec, builtin, caffeinate, timeout, watch, xargs, busybox), and
# follows sh/bash/zsh -c, eval, su -c, env -S, osascript's `do shell script`
# and find -exec into the command they run. `cd` is followed, so
# `cd / && rm -rf usr` is a delete of /usr.
#
# What stays a card (the 2026-08-16 design still holds for these): a recursive
# delete of a plain relative path below an ordinary folder of the working
# folder (run_shell runs in home: `rm -rf build`, `rm -rf proj/build`,
# `rm -rf build/*`, `rm -rf *.egg-info`), the same after `cd` into a folder
# below home, into an ordinary folder elsewhere (/tmp) or into one only known
# at run time (`cd "$PROJECT"`), `find . -name X -delete`, `find build ...
# -delete`, and a find with a name, age or size filter under an ordinary
# folder (`find ~/Projects -name .DS_Store -delete`). A relative path that is
# one of home's main folders (Documents, Desktop, Library, .ssh, .moe ...) or
# directly inside one is refused like its absolute spelling, and so is one
# that resolves to a top or system folder (`cd / && rm -rf usr`). Heredoc
# bodies are text unless a shell or interpreter reads its program from them.
#
# It is still a reader of text: a name or path assembled by other means
# (printf escapes, a script file written first and run second, an
# interpreter's own rmtree) gets past it, and then gets the ordinary card.
NEVER_RUN_TAIL = "If you want it done, you can run it yourself in Terminal."

_NR_SUBST = "$_w{}_"
_NR_SUBST_RE = re.compile(r"\$_w(\d+)_")
_NR_FIND_END = "__wisp_find_end__"
_NR_DEPTH = 8
_NR_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "mksh"})
_NR_INTERPRETERS = _NR_SHELLS | frozenset({"perl", "ruby", "node", "php", "osascript", "lua"})
# Something that fetches from the network, or decodes hidden text.
_NR_FETCH = re.compile(
    r"(?<![\w.-])(?:curl|wget|fetch|aria2c|lwp-request)(?![\w.-])"
    r"|(?<![\w.-])base64\s+(?:-\w+\s+)*?(?:-d|-D|--decode)(?![\w-])"
    r"|(?<![\w.-])xxd\s+(?:-\w+\s+)*?-r(?![\w-])|(?<![\w.-])uudecode(?![\w.-])"
    r"|(?<![\w.-])openssl\s+(?:base64|enc)\b[^|;&]*\s-d(?![\w-])", re.IGNORECASE)
_NR_HOME_NAMES = frozenset({"desktop", "documents", "downloads", "library", "movies", "music",
                            "pictures", "public", "applications", "sites", ".ssh", ".moe",
                            ".gnupg", ".aws", ".config", ".trash"})
_NR_ROOTS = frozenset({"/", "/users", "/system", "/library", "/usr", "/etc", "/private",
                       "/private/etc", "/var", "/private/var", "/bin", "/sbin", "/applications",
                       "/opt", "/volumes", "/cores"})
_NR_CRITICAL_PROCESSES = frozenset({"loginwindow", "windowserver", "launchd", "kernel_task"})
_NR_READERS = frozenset({"cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "ls",
                         "stat", "wc", "file", "diff", "cmp", "md5", "shasum", "echo"})
_NR_DISKUTIL = frozenset({"erasedisk", "erasevolume", "reformat", "partitiondisk", "zerodisk",
                          "randomdisk", "secureerase", "erasecd", "erasedvd"})
_NR_DISKUTIL_GROUPS = frozenset({"apfs", "cs", "corestorage", "ar", "appleraid"})
_NR_DISKUTIL_SUB = frozenset({"deletecontainer", "deletevolume", "erasevolume", "delete",
                              "deletelvg", "deletelv"})
_NR_TMUTIL = frozenset({"delete", "disable", "disablelocal", "deletelocalsnapshots",
                        "thinlocalsnapshots", "removedestination", "deleteinprogress"})
_NR_GNU = frozenset({"rm", "find", "chmod", "chown", "chgrp", "dd", "truncate", "shred", "mv",
                     "cp", "tee", "kill"})
# Top-level folder names (users, system, library, usr ...), refused as a
# relative delete when the working folder is only known at run time.
_NR_TOP_NAMES = frozenset(p.rsplit("/", 1)[-1] for p in _NR_ROOTS if p != "/")
# macOS's own folders: a relative delete after `cd` into one is refused.
_NR_SYSTEM = ("/system", "/bin", "/sbin", "/usr", "/etc", "/private/etc", "/private/var/db",
              "/library/launchdaemons", "/library/launchagents", "/library/keychains")
# Every regex in this section is linear: no quantified group whose body can
# match the same text two ways, so a 16,000-character line cannot stall the
# event loop. _NR_BUDGET is the backstop: a check that has used this much
# CPU time is abandoned and the command refused (see never_run_hit).
_NR_BUDGET = 0.25
# A function that runs itself in the background or into a pipe: `:(){ :|:& };:`.
# The lookbehind starts the name only at a word's first character.
_NR_FUNC = re.compile(r"(?<![^\s(){}|&;<>'\"])([^\s(){}|&;<>'\"]+)[ \t]*\([ \t]*\)\s*\{([^{}]*)\}")
_NR_FORK_CODE = re.compile(r"fork\s+while\s+fork|while\s*(?:\(\s*)?(?:1|true|True)\s*(?:\)\s*)?"
                           r"(?::\s*)?(?:\{\s*)?(?:os\.|posix\.|Process\.)?fork\b")
_NR_BRACES = re.compile(r"\{[^{}]*\}")
_NR_BRACE_LIST = re.compile(r"\{([^{},]*,[^{}]*)\}")
_NR_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|(\\?)([^\s;&|<>()'\"]+))")
_NR_HEREDOC_MARK = "__wisp_heredoc_{}__"
_NR_HEREDOC_MARK_RE = re.compile(r"__wisp_heredoc_(\d+)__")
_NR_HEREDOC_BODY = "__wisp_hdb_{}__"
_NR_HEREDOC_BODY_RE = re.compile(r"__wisp_hdb_([0-9a-f]*)__")
# An interpreter one-liner that runs a command: its string literals are judged.
_NR_EXEC_HINT = re.compile(r"system|popen|subprocess|exec|spawn|`|%x|shell_exec|passthru"
                           r"|check_output|check_call|\brun\s*\(|\bcall\s*\(")
_NR_LITERAL = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|`((?:[^`\\]|\\.)*)`")
_FORK_BOMB = "starts programs without end until the Mac stops responding"
_FETCHED_CODE = "runs code downloaded from the internet or decoded from hidden text"
_DISK_ERASE = "erases or repartitions a disk"
_SUDOERS = "edits the administrator rules"
_TOO_DEEP = "nests commands inside commands too deeply for Wisp to check"
_TOO_SLOW = "is too complicated for Wisp to check"
_REMOVES_USER = "deletes a user account"


class _NrTooSlow(Exception):
    """The never-run check used up _NR_BUDGET."""


class _NrContext:
    """Where a command line is when a simple command runs."""
    __slots__ = ("cwd", "fed", "depth", "inner", "deadline", "parens", "unknown", "shadowed")

    def __init__(self, cwd, depth=0, inner=None, deadline=None, unknown=None):
        # ("rel", path below home) or ("abs", path outside home), or None when
        # the folder is only known while the command runs.
        self.cwd = cwd
        self.fed = False          # operands also arrive on stdin (xargs)
        self.depth = depth
        self.inner = inner if inner is not None else []
        self.deadline = deadline if deadline is not None else time.thread_time() + _NR_BUDGET
        # The text being judged had an escaped or quoted parenthesis. They
        # reach a find expression as plain words and its grouping is not
        # worked out, so only the cautious filter rule applies.
        self.parens = False
        # Shared by every child: set when a shell's program text holds a
        # command's output somewhere other than its command word (see
        # _nr_resolve_program). The call then gets a card, not a refusal.
        self.unknown = unknown if unknown is not None else []
        # The line defines a function or alias named like a command whose
        # output is otherwise known (`echo(){ ...; }`), or switches a builtin
        # off: no output is known then (see _nr_shell_program).
        self.shadowed = False

    def child(self) -> "_NrContext":
        kid = _NrContext(self.cwd, self.depth + 1, self.inner, self.deadline, self.unknown)
        kid.parens = self.parens
        kid.shadowed = self.shadowed
        return kid

    def tick(self) -> None:
        if time.thread_time() > self.deadline:
            raise _NrTooSlow


def _nr_home() -> str:
    return str(Path.home()).rstrip("/") or "/"


def _nr_protected_hit(path: str | None, *, erasing: bool = True) -> str | None:
    """What a path holds when no command may delete or overwrite it. Wisp's
    own folder counts only when the command deletes or empties (`erasing`):
    writing into it or moving it keeps its card (the #409/#413 design)."""
    if not path:
        return None
    home = _nr_home()
    ssh = f"{home}/.ssh".casefold()
    if _within(path, [ssh]):
        inside = path.rstrip("/").casefold()[len(ssh):].lstrip("/")
        name = inside.rsplit("/", 1)[-1]
        if not inside:
            return "your SSH folder and the keys in it"
        if "/" not in inside and name.startswith(("config", "known_hosts")):
            return None             # SSH settings and known hosts keep their card
        if name.startswith("id_") or name.endswith((".pem", ".key", ".pub")) or "key" in name:
            return "your SSH keys"
        return "a file in your SSH folder"
    trees = [(f"{home}/Library/Keychains", "your keychains"),
             ("/etc/sudoers", "the administrator rules"),
             ("/private/etc/sudoers", "the administrator rules"),
             ("/etc/sudoers.d", "the administrator rules"),
             ("/private/etc/sudoers.d", "the administrator rules")]
    if erasing:
        trees += [(folder, "Wisp's own data") for folder in _wisp_folders()]
    for root, what in trees:
        if _within(path, [root.rstrip("/").casefold()]):
            return what
    return None


def _nr_close(text: str, start: int) -> int:
    """Index of the `)` closing the `(` just before `start` (quote-aware)."""
    depth, quote, i = 1, None, start
    while i < len(text):
        c = text[i]
        if quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            i += 1
        elif quote == '"':
            quote = None if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def _nr_lift(text: str, inner: list[str]) -> str:
    """Replace $(...), `...`, <(...) and >(...) with a placeholder word whose
    number indexes `inner`, where the body is kept for its own check."""
    out, quote, i, n = [], None, 0, len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            out.append(c)
            quote = None if c == "'" else quote
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "`":
            j = i + 1
            while j < n and text[j] != "`":
                j += 2 if text[j] == "\\" else 1
            inner.append(text[i + 1:j])
            out.append(_NR_SUBST.format(len(inner) - 1))
            i = j + 1
            continue
        if c in "$<>" and text[i + 1:i + 2] == "(" and (c == "$" or quote is None):
            j = _nr_close(text, i + 2)
            inner.append(text[i + 2:j])
            out.append(_NR_SUBST.format(len(inner) - 1))
            i = j + 1
            continue
        if c == '"':
            quote = None if quote == '"' else '"'
        elif c == "'" and quote is None:
            quote = "'"
        out.append(c)
        i += 1
    return "".join(out)


def _nr_mask(text: str) -> str:
    """`text` with every character inside quotes (not the quotes) replaced by
    "_", same length, so a check can look only at unquoted text. Text whose
    quotes do not balance comes back unchanged."""
    if "'" not in text and '"' not in text:
        return text
    out, quote, i, n = list(text), None, 0, len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                out[i] = "_"
        elif c == "\\" and i + 1 < n:
            if quote:
                out[i] = out[i + 1] = "_"
            i += 1
        elif quote == '"':
            if c == '"':
                quote = None
            else:
                out[i] = "_"
        elif c in "'\"":
            quote = c
        i += 1
    return text if quote else "".join(out)      # unbalanced quotes: nothing is masked


def _nr_at_command_start(out: list[str]) -> bool:
    """Whether what has been copied so far ends where a command starts."""
    tail = "".join(out[-64:]).rstrip(" \t")
    return not tail or tail[-1] in ";&|(\n" or tail.endswith(("then", "do", "else", "{", "!"))


def _nr_arith_end(text: str, i: int) -> int:
    """The index just past the arithmetic starting at text[i] ($[...] or
    ((...)), counting nested brackets; the end of the text if it never closes."""
    opener, closer = ("[", "]") if text.startswith("$[", i) else ("(", ")")
    depth, j, n = 0, i, len(text)
    while j < n:
        if text[j] == opener:
            depth += 1
        elif text[j] == closer:
            depth -= 1
            if depth == 0:
                return j + 1
        elif text[j] == "\n" and opener == "(" and depth <= 1:
            return j                    # `( (a) )` subshells, not arithmetic: stop at the line
        j += 1
    return n


def _nr_drop_fd(out: list[str]) -> None:
    """Remove a file-descriptor number just copied before a redirect (`0<<EOF`)."""
    k = len(out)
    while k and out[k - 1].isdigit() and len(out[k - 1]) == 1:
        k -= 1
    if k < len(out) and (k == 0 or out[k - 1] in (" ", "\t", "\n", ";", "&", "|", "(")):
        del out[k:]


def _nr_expansions(body: str) -> list[str]:
    """The $(...) and `...` commands in an unquoted heredoc body, which the
    shell runs while writing the body out (quotes are literal text there)."""
    found, i, n = [], 0, len(body)
    while i < n:
        if body[i] == "`":
            j = body.find("`", i + 1)
            j = n if j < 0 else j
            found.append(body[i + 1:j])
            i = j + 1
        elif body.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(body[j], 0)
                j += 1
            found.append(body[i + 2:j - 1 if depth == 0 else j])
            i = j
        else:
            i += 1
    return found


def _nr_stdin_program(words: list[str]) -> str | None:
    """'shell', 'applescript' or 'code' when a simple command runs a program it
    reads from stdin (sh, bash -s, python3 -, osascript ...), else None."""
    unwrapped, _, via = _nr_unwrap(list(words), _NrContext(("rel", "."), deadline=0.0))
    if via is not None or not unwrapped:
        return None
    name = _nr_prog(unwrapped[0])
    if name == "osascript":
        return "applescript" if not [a for a in unwrapped[1:] if not a.startswith("-")] else None
    if not (name in _NR_INTERPRETERS or re.fullmatch(r"python[\d.]*", name)):
        return None
    if not _nr_reads_stdin(name, unwrapped[1:]):
        return None
    return "shell" if name in _NR_SHELLS else "code"


def _nr_heredoc_feeds(line: str, marks: list[str]) -> dict[str, str]:
    """For each heredoc mark on one command line, what reads its body when
    that is a program (see _nr_stdin_program): the command the heredoc is on,
    or a later stage of the same pipeline (`cat <<EOF | sh`)."""
    segments = _nr_segments(line)
    if segments is None:                       # cannot tell: judge every body
        return {mark: "shell" for mark in marks}
    feeds: dict[str, str] = {}
    for k, seg in enumerate(segments):
        here = [w for w in seg["words"] if w in marks]
        if not here:
            continue
        stages = [seg]
        for later in segments[k + 1:]:
            if later["op"] not in ("|", "|&"):
                break
            stages.append(later)
        for stage in stages:
            words = [w for w in stage["words"] if not _NR_HEREDOC_MARK_RE.fullmatch(w)]
            if words and (kind := _nr_stdin_program(words)):
                feeds.update(dict.fromkeys(here, kind))
                break
    for mark in marks:
        if not any(mark in seg["words"] for seg in segments):
            feeds[mark] = "shell"
    return feeds


def _nr_heredocs(text: str, ctx: "_NrContext",
                 force: bool = False) -> tuple[str, list[str], list[tuple[str, str]]]:
    """Take comments and heredoc bodies out of a command line: a body is text
    handed to a command, not commands (a commit message saying "halt the
    timer", a Makefile with `rm -rf $(BUILD)`), unless what reads it is a
    shell, which runs it: then it stays in the line and is judged where it
    runs. Returns (the line, commands an unquoted body runs while it is
    written, (program text, 'code' or 'applescript') read by an interpreter).
    With `force` every body is treated as read by a shell: for a $(...) whose
    output is itself run (sh -c "$(cat <<'EOF' ... EOF)")."""
    if "<<" not in text and "#" not in text:
        return text, [], []
    out: list[str] = []
    shell: list[str] = []
    code: list[tuple[str, str]] = []
    pending: list[tuple[str, str, bool, bool]] = []
    frames: list[tuple] = []
    quote, depth, start, arith, marks = None, 0, 0, False, 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            out.append(c)
            quote = None if c == "'" else quote
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == '"':
            quote = None if quote == '"' else '"'
            out.append(c)
            i += 1
            continue
        if text.startswith("$[", i) or (quote is None and text.startswith("((", i)
                                         and _nr_at_command_start(out)):
            # Arithmetic ($[...], ((...)) as a command): `1 << 2` is a shift, not a heredoc.
            j = _nr_arith_end(text, i)
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("$(", i):
            frames.append((quote, depth, start, arith))
            quote, depth, arith = None, 0, text.startswith("$((", i)
            out.append("$(")
            i += 2
            start = len(out)
            continue
        if quote == '"':
            out.append(c)
            i += 1
            continue
        if c == "'":
            quote = "'"
        elif c == "(":
            depth += 1
        elif c == ")":
            if depth == 0 and frames:
                quote, depth, start, arith = frames.pop()
                out.append(c)
                i += 1
                continue
            depth = max(depth - 1, 0)
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            j = text.find("\n", i)
            i = n if j < 0 else j               # a comment never runs: dropped
            continue
        elif text.startswith("<<<", i):
            out.append("<<<")
            i += 3
            continue
        elif text.startswith("<<", i) and not arith and (m := _NR_HEREDOC.match(text, i)):
            delim = next(g for g in (m.group(2), m.group(3), m.group(5)) if g is not None)
            mark = _NR_HEREDOC_MARK.format(marks)
            marks += 1
            _nr_drop_fd(out)
            pending.append((mark, delim, m.group(1) == "-", m.group(5) is None or bool(m.group(4))))
            out.append(f" {mark} ")
            i = m.end()
            continue
        elif c == "\n" and pending:
            ctx.tick()
            out.append("\n")
            i += 1
            since = "".join(out[start:])
            feeds = _nr_heredoc_feeds(since[since.rfind("\n", 0, len(since) - 1) + 1:],
                                      [p[0] for p in pending])
            for mark, delim, strip, quoted in pending:
                rows = []
                while i < n:
                    j = text.find("\n", i)
                    j = n if j < 0 else j
                    row, i = text[i:j], j + 1
                    if (row.lstrip("\t") if strip else row).rstrip("\r") == delim:
                        break
                    rows.append(row)
                i = min(i, n)
                body = "\n".join(rows)
                kind = "shell" if force else feeds.get(mark)
                if kind == "shell":
                    inner, more_shell, more_code = _nr_heredocs(body, ctx, force)
                    out.append(inner + "\n")
                    shell += more_shell
                    code += more_code
                elif kind:
                    code.append((body, kind))
                elif not quoted:
                    shell += _nr_expansions(body)
                if frames and kind != "shell":
                    # Inside $(...) the body may be printed and run by whatever
                    # runs the output (sh -c "$(cat <<'EOF' ...)"): kept, encoded
                    # so its quotes cannot upset the parse, for _nr_run_output_hit.
                    out.append(f" {_NR_HEREDOC_BODY.format(body.encode('utf-8', 'replace').hex())} ")
            pending = []
            continue
        out.append(c)
        i += 1
    return _NR_HEREDOC_MARK_RE.sub(" ", "".join(out)), shell, code


# An operator character that is quoted or backslash-escaped is part of a word.
# The lexer would hand a wholly quoted `'('`, `")"`, `'|'` or `'&'` back as a
# bare token that reads as the operator, which ended the command there and hid
# the words after it from the check. Each one is carried through the lexer as a
# private-use character and put back in the word it belongs to.
_NR_OPERATOR_CHARS = "();<>|&"
_NR_OPERATOR_HOLD = {c: chr(0xF2A0 + k) for k, c in enumerate(_NR_OPERATOR_CHARS)}
_NR_OPERATOR_GONE = chr(0xF2AF)                 # stands where the escaping backslash was
_NR_OPERATOR_BACK = {ord(v): k for k, v in _NR_OPERATOR_HOLD.items()} | {ord(_NR_OPERATOR_GONE): None}
# A stand-in already in the text is an ordinary character to the shell. Left
# in, it would come back as an operator character inside a word, and a word
# that is itself a program (sh -c, eval) would then be split on it.
_NR_OPERATOR_STRAY = re.compile("[\uf2a0-\uf2af]")


def _nr_keep_quoted_operators(text: str, mask: str) -> str:
    """`text`, same length, with each quoted or escaped operator character
    swapped for its stand-in (`mask` is _nr_mask(text))."""
    if not any(c in text for c in _NR_OPERATOR_CHARS):
        return text
    chars, n, i = list(text), len(text), 0
    while i < n:
        c = text[i]
        if c == "\\" and mask[i] == "\\" and i + 1 < n and text[i + 1] in _NR_OPERATOR_HOLD:
            chars[i], chars[i + 1] = _NR_OPERATOR_GONE, _NR_OPERATOR_HOLD[text[i + 1]]
            i += 2
            continue
        if c in _NR_OPERATOR_HOLD and mask[i] != c:
            chars[i] = _NR_OPERATOR_HOLD[c]
        i += 1
    return "".join(chars)


def _nr_segments(text: str) -> list[dict] | None:
    """Simple commands: the operator before each, its words and its output
    redirect targets. None when the quoting does not parse."""
    text = _NR_OPERATOR_STRAY.sub("_", text)
    # find's `-exec ... ;` terminator: an escaped or quoted `;` that is a word
    # of its own. Inside a longer word (`'a';'b'`) the `;` is a real separator.
    text = re.sub(r"(?<!\S)(?:\\;|';'|\";\")(?!\S)", f" {_NR_FIND_END} ", text).replace("\r", "\n")
    mask = _nr_mask(text)
    text = _nr_keep_quoted_operators(text, mask)
    # A file-descriptor number on a redirect (`2>/dev/null`, `0<<<`) is not a word.
    fds = [m.span() for m in re.finditer(r"(?<![^\s;&|()])\d+(?=[<>])", mask)]
    if fds or "\n" in text:
        chars = list(text)
        for a, b in fds:
            chars[a:b] = " " * (b - a)
        # A newline ends a command; inside quotes it is part of a word.
        text = "".join(" ; " if c == "\n" and m == "\n" else c for c, m in zip(chars, mask))
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments, current, redirect = [], {"op": "", "words": [], "out": [], "here": []}, None
    try:
        for token in lexer:
            if token and set(token) <= set("();<>|&"):
                if ">" in token:
                    redirect = "append" if ">>" in token else "out"
                elif "<<<" in token:
                    redirect = "here"
                elif "<" in token:
                    redirect = "in"
                else:
                    if current["words"] or current["out"]:
                        segments.append(current)
                    current = {"op": token, "words": [], "out": [], "here": []}
                continue
            token = token.translate(_NR_OPERATOR_BACK)
            if redirect == "here":
                current["here"].append(token)
            elif redirect is not None and redirect != "in":
                current["out"].append((token, redirect == "append"))
            elif redirect is None:
                current["words"].append(token)
            redirect = None
    except ValueError:
        return None
    if current["words"] or current["out"]:
        segments.append(current)
    return segments


def _nr_prog(word: str) -> str:
    name = word.rsplit("/", 1)[-1].lower()
    return name[1:] if name.startswith("g") and name[1:] in _NR_GNU else name   # Homebrew's grm


def _nr_skip_opts(words: list[str], with_arg: str = "", duration: bool = False) -> list[str]:
    """Drop leading options; single letters in `with_arg` take the next word."""
    while words and words[0].startswith("-") and words[0] != "-":
        flag = words.pop(0)
        if flag == "--":
            break
        if len(flag) == 2 and flag[1] in with_arg and words:
            words.pop(0)
    if duration and words and re.fullmatch(r"[\d.]+[smhd]?", words[0]):
        words.pop(0)
    return words


def _nr_unwrap(words: list[str], ctx: _NrContext) -> tuple[list[str], bool, str | None]:
    """(the command inside its wrappers, whether it runs as administrator,
    a command string a wrapper hands to a shell instead)."""
    admin = False
    while words:
        w = words[0]
        name = _nr_prog(w)
        if w in ("{", "}", "!", "then", "do", "else", "elif", "if", "while", "until", "function",
                 "time", "builtin", "nohup", "noglob"):
            words = words[1:]
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w):
            words = words[1:]
        elif name == "env":
            rest = words[1:]
            while rest and rest[0].startswith("-") and rest[0] != "-":
                flag = rest.pop(0)
                if flag == "--":
                    break
                if flag in ("-S", "--split-string"):
                    return [], admin, " ".join(rest)
                if flag.startswith("-S"):
                    return [], admin, " ".join([flag[2:], *rest])
                if flag in ("-u", "-P", "-C", "--unset", "--chdir") and rest:
                    rest.pop(0)
            while rest and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", rest[0]):
                rest.pop(0)
            words = rest
        elif name in ("sudo", "doas", "pkexec"):
            admin = True
            words = _nr_skip_opts(words[1:], "ugphCDrtTUR")
        elif name == "su":
            rest = words[1:]
            if "-c" in rest and rest.index("-c") + 1 < len(rest):
                return [], True, rest[rest.index("-c") + 1]
            return [], True, None
        elif name == "nice":
            words = _nr_skip_opts(words[1:], "n")
        elif name in ("command", "exec"):
            if words[1:2] in (["-v"], ["-V"]):
                return [], admin, None              # only looks the name up
            words = _nr_skip_opts(words[1:], "a")
        elif name == "caffeinate":
            words = _nr_skip_opts(words[1:], "tw")
        elif name == "timeout":
            words = _nr_skip_opts(words[1:], "sk", duration=True)
        elif name in ("stdbuf", "arch", "busybox", "unbuffer", "chronic"):
            words = _nr_skip_opts(words[1:])
        elif name == "watch":
            return [], admin, " ".join(_nr_skip_opts(words[1:], "n"))
        elif name == "script":
            # script [-aeFkqr] [-t time] [file [command ...]]; util-linux: -c CMD.
            rest = words[1:]
            while rest and rest[0].startswith("-") and rest[0] != "-":
                flag = rest.pop(0)
                if flag == "--":
                    break
                if flag in ("-c", "--command") and rest:
                    return [], admin, rest[0]
                if flag in ("-t", "-T", "-I", "-O", "-B", "-E", "-m") and rest:
                    rest.pop(0)
            words = rest[1:]
        elif name == "xargs":
            rest, replace = words[1:], None
            while rest and rest[0].startswith("-") and rest[0] != "-":
                flag = rest.pop(0)
                if flag == "--":
                    break
                if flag[:2] in ("-I", "-J"):
                    replace = flag[2:] or (rest.pop(0) if rest else None)
                elif len(flag) == 2 and flag[1] in "LnPsERSda" and rest:
                    rest.pop(0)
            ctx.fed = True
            words = [("$__wisp_stdin__" if replace and replace in r else r) for r in rest] or ["echo"]
        else:
            break
    return words, admin, None


def _nr_target(word: str, cwd) -> tuple[str, str | None]:
    """How a path operand resolves: ("dynamic", None), ("glob", the folder the
    wildcard is in, as below), ("abs", path), ("rel", path below home) or
    ("unknown", the relative path) in a folder only known at run time."""
    w = re.sub(r"^\$(?:HOME|\{HOME\})(?=/|$)", "~", word)
    if "$" in w or "`" in w or "\0" in w:          # a NUL cannot name a real path
        return "dynamic", None
    if re.search(r"[*?\[]", w) or any("," in b or ".." in b for b in _NR_BRACES.findall(w)):
        base = re.split(r"[*?\[{]", w, maxsplit=1)[0]
        head = base[:base.rfind("/") + 1] if "/" in base else ""
        _, folder = _nr_target(head.rstrip("/") or ("/" if head else "."), cwd)
        return "glob", folder
    if w == "~" or w.startswith("~/"):
        return "abs", os.path.normpath(_nr_home() + w[1:])
    if w.startswith("~"):
        return "abs", os.path.normpath(os.path.expanduser(w))
    if w.startswith("/"):
        return "abs", os.path.normpath(w).replace("//", "/") or "/"
    if cwd is None:
        return "unknown", os.path.normpath(w)
    if cwd[0] == "rel":
        return "rel", os.path.normpath(os.path.join(cwd[1], w))
    return "abs", os.path.normpath(os.path.join(cwd[1], w))


def _nr_full(kind: str, path: str | None) -> str | None:
    """An absolute spelling of a resolved operand, for the protected-tree check."""
    if kind == "abs":
        return path
    if kind == "rel" and path and not path.startswith(".."):
        return _nr_home() if path == "." else f"{_nr_home()}/{path}"
    return None


def _nr_root_like(path: str | None) -> bool:
    if not path:
        return False
    home = _nr_home().casefold()
    p = path.rstrip("/").casefold() or "/"
    return (p in _NR_ROOTS or p == home or p == f"{home}/library"
            or (p.startswith(home + "/") and p[len(home) + 1:] in _NR_HOME_NAMES))


def _nr_rel_problem(rel: str) -> str | None:
    """Why a recursive change to a path below home is too broad, or None.
    A main folder (Documents, Desktop, Library ...) and what sits directly in
    it are refused; a folder deeper down (Desktop/MOE_Project/build) is not."""
    if rel == ".":
        return "the whole working folder, which is your home folder"
    if rel == ".." or rel.startswith("../"):
        return "a path outside the working folder"
    parts = rel.split("/")
    if parts[0].casefold() in _NR_HOME_NAMES:
        if len(parts) == 1:
            return "one of your main folders"
        if len(parts) == 2:
            return "a folder directly inside one of your main folders"
    return None


def _nr_rm(args: list[str], ctx: _NrContext, admin: bool, unknown_name: bool = False) -> str | None:
    recursive = False
    operands, options = [], True
    for a in args:
        if options and a == "--":
            options = False
        elif options and a.startswith("--") and len(a) > 2:
            recursive |= "recursive".startswith(a[2:].split("=", 1)[0].lower())
        elif options and a.startswith("-") and len(a) > 1:
            recursive |= "r" in a[1:].lower()
        else:
            operands.append(a)
    if unknown_name and not recursive:
        return None
    if admin and not unknown_name:
        return "deletes files as an administrator"
    if recursive and ctx.fed:
        return "recursively deletes paths it only learns while running"
    what = "recursively deletes" if recursive else "deletes"
    for op in _nr_expand_braces(operands):
        kind, path = _nr_target(op, ctx.cwd)
        if (hit := _nr_protected_hit(_nr_full(kind, path))):
            return f"deletes {hit} ({op})"
        if not recursive:
            continue
        w = re.sub(r"^\$(?:HOME|\{HOME\})(?=/|$)", "~", op)
        if kind == "dynamic":
            return f"{what} a path that is only known while it runs ({op})"
        if w.startswith(("/", "~")):
            return (f"{what} everything a wildcard matches ({op})" if kind == "glob"
                    else f"{what} an absolute path ({op})")
        if (problem := _nr_rel_delete(w, ctx.cwd)):
            return f"{what} {problem}"
    return None


def _nr_expand_braces(words: list[str], limit: int = 64) -> list[str]:
    """Words with {a,b} lists expanded (a list past `limit` words becomes a
    wildcard) and {1..9} ranges spelled as a wildcard."""
    out, todo = [], list(reversed(words))
    while todo:
        w = todo.pop()
        m = _NR_BRACE_LIST.search(w)
        if m is None:
            out.append(re.sub(r"\{[^{}]*\.\.[^{}]*\}", "*", w))
        elif len(out) + len(todo) >= limit:
            out.append(re.sub(r"[{},]", "*", w))      # too many words: judged as a wildcard
        else:
            todo += reversed([w[:m.start()] + alt + w[m.end():] for alt in m.group(1).split(",")])
    return out


def _nr_place(path: str) -> tuple[str, str]:
    """An absolute folder as a working-folder state: below home it is
    ("rel", path below home), so the relative-folder rules apply to it."""
    home = _nr_home()
    p, h = path.rstrip("/").casefold() or "/", home.casefold()
    if p == h:
        return "rel", "."
    if p.startswith(h + "/"):
        return "rel", path.rstrip("/")[len(home) + 1:]
    return "abs", path


def _nr_show(cwd, rel: str) -> str:
    """How a reason names the folder a relative path resolves to."""
    if cwd is None:
        return rel
    if cwd[0] == "rel":
        path = os.path.normpath(os.path.join(cwd[1], rel))
        return "~" if path == "." else f"~/{path}"
    return os.path.normpath(os.path.join(cwd[1], rel))


def _nr_system_like(path: str | None) -> bool:
    """Root-like, or inside one of macOS's own folders (/usr/local aside)."""
    if not path or _nr_root_like(path):
        return bool(path)
    p = path.rstrip("/").casefold() or "/"
    if p == "/usr/local" or p.startswith("/usr/local/"):
        return False
    return any(p == s or p.startswith(s + "/") for s in _NR_SYSTEM)


def _nr_rel_delete(word: str, cwd) -> str | None:
    """Why a recursive delete of a relative `word` (wildcards allowed), run in
    the working folder `cwd`, is too broad, or None for a card. The reason
    names the folder the delete resolves to."""
    wild = re.search(r"[*?\[]", word)
    if wild:
        head = word[:word.rfind("/", 0, wild.start()) + 1]
        pattern = word[len(head):].split("/", 1)[0]
        folder = head.rstrip("/") or "."
        bare = not re.sub(r"\[[^\]]*\]|[*?.]", "", pattern)
        homeish = bare or any(fnmatch.fnmatchcase(name, pattern.casefold()[:200])
                              for name in _NR_HOME_NAMES)
    else:
        folder, bare, homeish = word, False, False
    parts = [p for p in folder.split("/") if p not in ("", ".")]
    up = ".." in parts
    if cwd is None:
        where = "a folder that is only known while it runs"
        if up or (not wild and not parts):
            return f"the working folder or a folder above it, which is only known while it runs ({word})"
        if parts and parts[0].casefold() in (_NR_HOME_NAMES | _NR_TOP_NAMES
                                             | {_nr_home().rsplit("/", 1)[-1].casefold()}):
            return f"a folder named like one of your main folders, in {where} ({word})"
        if wild and not parts and homeish:
            return f"everything in {where} ({word})"
        return None
    shown = _nr_show(cwd, folder)
    if cwd[0] == "rel":
        target = os.path.normpath(os.path.join(cwd[1], folder))
        if target == ".." or target.startswith("../"):
            return f"a path outside your home folder ({word})"
        # `cd build && rm -rf *` is judged as `rm -rf build/*`; in home itself
        # a bare `*`, `.*` or `./*`, or a pattern that can match a main folder
        # (`D*`), is refused.
        if wild and target == ".":
            return f"everything in your home folder ({word})" if homeish else None
        if (problem := _nr_rel_problem(target)):
            if target == ".":
                problem = "your whole home folder"
            return f"{'everything in ' if wild else ''}{problem} ({shown})"
        return None
    base = cwd[1]
    if _nr_system_like(base):
        return f"{'everything in ' if wild else ''}{shown}, inside {base}, one of the Mac's top or system folders"
    if up:
        return f"{shown}, a path above the working folder {base}"
    if not parts and not wild:
        return f"the whole working folder ({base})"
    if wild and not parts and bare:
        return f"everything in {base}"
    if _nr_system_like(shown):
        return f"{shown}, one of the Mac's top or system folders"
    return None


def _nr_mode_narrows(mode: str) -> bool:
    """Whether a chmod mode only takes permissions from group and others
    (go-rwx, 700, 600, u+rw,go-rwx), so it can only tighten a folder."""
    if re.fullmatch(r"0?[0-7]00", mode):
        return True
    for clause in mode.split(","):
        m = re.fullmatch(r"([ugoa]*)([-+=])([rwxXst]*)", clause)
        if not m:
            return False
        who, op, perms = m.groups()
        if op == "-" or (who and set(who) == {"u"}) or (op == "=" and not perms and "u" not in who and who):
            continue
        return False
    return True


def _nr_find(args: list[str], ctx: _NrContext, admin: bool) -> str | None:
    rest = list(args)
    while rest and rest[0] in ("-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"):
        rest.pop(0)
    roots = []
    while rest and not rest[0].startswith("-") and rest[0] not in ("(", "!"):
        word = rest.pop(0)
        roots.append(";" if word == _NR_FIND_END else word)     # find reads a leading `;` as a path
    runs: list[list[str]] = []
    i = 0
    while i < len(rest):
        if rest[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(rest) and rest[j] != _NR_FIND_END and not (rest[j] == "+" and rest[j - 1] == "{}"):
                j += 1
            runs.append(rest[i + 1:j])
            i = j
        i += 1
    named = _nr_find_named(rest, ctx.parens)
    deleters = ("rm", "srm", "unlink")
    deletes = "-delete" in rest or any(run and _nr_prog(run[0]) in deleters for run in runs)
    for root in roots or ["."]:
        if deletes:
            if admin:
                return "deletes files as an administrator"
            if (problem := _nr_find_root(root, ctx.cwd, named)):
                return f"deletes files found under {problem}"
        for run in runs:
            if run and _nr_prog(run[0]) in deleters:
                # The matches were judged with the root above; other operands still count.
                words = [w for w in run if w != "{}"]
            else:
                match = os.path.join(root, "__wisp_match__")
                words = [w.replace("{}", match) for w in run]
            if (hit := _nr_judge_words(words, ctx.child())):
                return hit
    return None


_NR_FIND_FILTERS = frozenset({"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename",
                              "-regex", "-iregex", "-mtime", "-mmin", "-atime", "-amin", "-ctime",
                              "-cmin", "-size", "-empty", "-user", "-group", "-perm", "-links",
                              "-inum", "-samefile"})
# find tests and actions that take no operand; every other one is read as
# taking the next word, so `! -fstype -name ...` is not mistaken for a filter.
_NR_FIND_PARENS = re.compile(r"\\[()]|'[()]'|\"[()]\"")
_NR_FIND_NO_OPERAND = frozenset({"-empty", "-delete", "-print", "-print0", "-prune", "-true",
                                 "-false", "-nouser", "-nogroup", "-xdev", "-mount", "-ls",
                                 "-quit", "-follow", "-and", "-a", "-d"})


def _nr_find_named(expr: list[str], parens: bool = True) -> bool:
    """Whether a find expression only matches files a filter picks by name,
    age, size or owner (-type or -mindepth alone still matches everything of
    that kind). With -o / -or / `,` every alternative needs a filter of its
    own, and a negated filter (`-not -name x`, `! -name x`) picks everything
    else, so it is no filter: `-name '*.pyc' -not -path './keep/*'` is
    filtered, `-not -name x` alone is not.

    Escaped or quoted parentheses arrive as plain words and are not worked
    out, so when the text had any (`parens`) the grouping is unknown: then
    only a filter that comes before every
    negation of its alternative counts (`! \\( -name a -name b \\)` has none).
    Without parentheses a negation covers the one test right after it."""
    branch: list[str] = []
    branches = [branch]
    skip = False
    for w in expr:
        if skip:                                # a -exec body: not a filter
            skip = w not in (_NR_FIND_END, "+")
            continue
        if w in ("-exec", "-execdir", "-ok", "-okdir"):
            skip = True
            continue
        if w in ("-o", "-or", ","):
            branch = []
            branches.append(branch)
            continue
        branch.append(w)

    def filtered(words: list[str]) -> bool:
        negated, k = False, 0
        while k < len(words):
            w = words[k]
            k += 1
            if w in ("!", "-not"):
                if parens:
                    return False                # nothing after it counts
                negated = True
                continue
            if not w.startswith("-"):
                continue                        # a stray word: no test
            if not negated and (w in _NR_FIND_FILTERS or w.startswith("-newer")):
                return True
            negated = False                     # the negation covered this test
            if w == "-depth":                   # BSD's `-depth n`, or GNU's bare -depth
                k += 1 if k < len(words) and re.fullmatch(r"[+-]?\d+", words[k]) else 0
            elif w not in _NR_FIND_NO_OPERAND:
                k += 1                          # its operand, even one spelled `-name`
        return False

    return all(filtered(b) for b in branches)


def _nr_find_root(root: str, cwd, named: bool) -> str | None:
    """Why a find that deletes is too broad for this root, or None for a card.
    A plain folder of the working folder keeps its card; `.` needs a name,
    age or size filter (`find . -type f -delete` empties home).
    Elsewhere a name, age or size filter is needed and the root must not be a
    top folder, home, one of home's main folders, or a protected folder.
    `find ~ -name '*.txt' -delete` stays refused: home is a top folder."""
    kind, path = _nr_target(root, cwd)
    if kind == "dynamic":
        return f"a folder that is only known while it runs ({root})"
    if kind == "glob":
        return f"everything a wildcard matches ({root})"
    if kind == "unknown":
        parts = [p for p in path.split("/") if p not in ("", ".")]
        if ".." in parts or (not parts and not named):
            return f"the working folder or a folder above it, which is only known while it runs ({root})"
        if parts and parts[0].casefold() in (_NR_HOME_NAMES | _NR_TOP_NAMES):
            return f"a folder named like one of your main folders ({root})"
        return None
    if kind == "rel":
        if path == "." and named:
            return None
        problem = _nr_rel_problem(path)
        if problem is None:
            return None
        full = _nr_full(kind, path)
        if not (named and full and not path.startswith("..") and not _nr_root_like(full)
                and not _nr_protected_hit(full)):
            return f"{problem} ({_nr_show(('rel', '.'), path)})"
        return None
    if not named:
        return f"an absolute path, with no filter for which files ({root})"
    if _nr_system_like(path) or _nr_protected_hit(path):
        return f"{path}, one of the Mac's top folders, your home folder or one of its main folders"
    return None


def _nr_fetches(word: str, inner: list[str]) -> bool:
    """Whether a word is a $(...) / <(...) whose body downloads or decodes."""
    return any(int(n) < len(inner) and _NR_FETCH.search(inner[int(n)])
               for n in _NR_SUBST_RE.findall(word))


def _nr_code(args: list[str], flags: tuple[str, ...], cluster: str = "") -> str | None:
    """The program text an interpreter is given (sh -c BODY, perl -e CODE)."""
    for k, a in enumerate(args):
        if a in flags or (cluster and a.startswith("-") and not a.startswith("--") and cluster in a[1:]):
            return next((b for b in args[k + 1:] if not b.startswith("-")), "")
    return None


def _nr_code_hit(code: str, ctx: _NrContext) -> str | None:
    """A refused command inside an interpreter program that runs commands
    (os.system('...'), system("..."), execSync('...'), `...`): each string
    literal, and each list of literals (['rm', '-rf', '/']) as one command,
    is judged as a command line. A program that runs nothing is not read."""
    if not _NR_EXEC_HINT.search(code):
        return None
    texts = ["".join(m) for m in _NR_LITERAL.findall(code)]
    for group in re.findall(r"\[([^\[\]]*)\]", code):
        items = ["".join(m) for m in _NR_LITERAL.findall(group)]
        if len(items) > 1:
            texts.append(" ".join(shlex.quote(re.sub(r"\\(.)", r"\1", t)) for t in items))
    for text in texts:
        # JS, Perl and Ruby drop the backslash of an unknown escape and Python
        # keeps it, so a literal with one is judged both ways.
        readings = [text, re.sub(r"\\(.)", r"\1", text)] if "\\" in text else [text]
        for reading in readings:
            if (hit := _nr_judge_text(reading, ctx.child())):
                return hit
    return None


def _nr_run_output_hit(text: str, ctx: _NrContext) -> str | None:
    """For text whose $(...) output is itself run as commands (sh -c "$(...)",
    bash <<< "$(...)", a $(...) in command position): a download or decode
    there runs fetched code, and a heredoc the body prints (`cat <<'EOF'`)
    is judged as the commands it becomes."""
    for n in _NR_SUBST_RE.findall(text):
        if int(n) >= len(ctx.inner):
            continue
        body = ctx.inner[int(n)]
        if _NR_FETCH.search(body):
            return _FETCHED_CODE
        for encoded in _NR_HEREDOC_BODY_RE.findall(body):
            printed = bytes.fromhex(encoded).decode("utf-8", "replace")
            if (hit := _nr_judge_text(printed, ctx.child())):
                return hit
        if "<<" in body:
            printed, _, _ = _nr_heredocs(body, ctx, force=True)
            if (hit := _nr_judge_text(printed, ctx.child())):
                return hit
    return None


_UNRESOLVED_PROGRAM = "runs a program it only learns while running, from a command's output"
# A $(...) whose body only prints a heredoc (sh -c "$(cat <<'EOF' ... EOF)"):
# that body was already judged as the program (see _nr_run_output_hit).
_NR_PRINTS_HEREDOC = re.compile(r"\s*cat\s+(?:__wisp_hdb_[0-9a-f]*__\s*)+")


# A $(...) whose output is known from its text: `echo` of plain words.
_NR_LITERAL_ECHO = re.compile(r"[ \tA-Za-z0-9._+@/=:,%-]*")
# A $(...) whose output no text in the command can choose (`$(date)`,
# `$(whoami)`, `$(uname -s)`); not `date +FORMAT`, `pwd` or `hostname`.
_NR_FIXED_OUTPUT = re.compile(r"\s*(?:date|whoami|uname(?:\s+-[amnoprsv]+)*)\s*")
# A $(...) that prints a path or a count the machine picks, not the command's
# own text: the only unknown output a shell program may hold and still get a
# card. Its output is pasted into the program before the shell reads it, like
# any other, so the card says Wisp cannot see what runs.
_NR_PLACE_OUTPUT = re.compile(
    r"\s*(?:pwd(?:\s+-[LP])?|git\s+rev-parse(?:\s+--(?:show-toplevel|show-cdup|git-dir))+|mktemp(?:\s+-d)?"
    r"|brew\s+--prefix(?:\s+[A-Za-z0-9@._+-]+)?|xcode-select\s+(?:-p|--print-path)"
    r"|nproc|sysctl\s+-n\s+hw\.[a-z_.]+|id\s+-u[n]?)\s*")


_NR_KNOWN_NAMES = r"(?:echo|date|whoami|uname|pwd|git|mktemp|brew|xcode-select|nproc|sysctl|id)"
_NR_SHADOWS = re.compile(
    rf"\b{_NR_KNOWN_NAMES}\s*\(\s*\)|\bfunction\s+{_NR_KNOWN_NAMES}\b"
    rf"|\balias\b[^;|&\n]*\b{_NR_KNOWN_NAMES}=|\benable\s+-[a-z]*n|\bunset\s+-f\b")


def _nr_literal_output(body: str) -> str | None:
    """What a $(...) body prints when it is `echo <plain words>`, else None.

    A first word starting with `-` may be an option (`-e` makes echo read
    escapes, and /bin/sh prints `-n` where bash drops it) and zsh expands a
    leading `=word` to a path, so an echo with either prints something this
    text does not show: its output is unknown."""
    if not _NR_LITERAL_ECHO.fullmatch(body):
        return None
    words = body.split()
    if (words[:1] != ["echo"] or words[1:2] and words[1][0] == "-"
            or any(w[0] == "=" for w in words[1:])):
        return None
    return " ".join(words[1:])


def _nr_command_word_from(text: str, marks: list[str], depth: int = 0) -> bool:
    """Whether one of the placeholders `marks` (outer $(...) whose output is
    unknown) can become a command in shell program `text`: it is or is in a
    command word, eval/source's text, a nested shell's or an interpreter's
    program or script, or `echo ... | sh`'s text. A program with a mark and
    a command word only known while it runs (`p=$(x); $p`) counts too. Text
    that does not parse, or holds a heredoc, counts."""
    if not any(m in text for m in marks):
        return False
    if depth > _NR_DEPTH or "<<" in text:
        return True
    segments = _nr_segments(text)
    if segments is None:
        return True
    scratch = _NrContext(None, deadline=0.0)
    pipeline: list[list[str]] = []
    for seg in [*segments, None]:
        if seg is None or seg["op"] not in ("|", "|&"):
            if len(pipeline) > 1 and pipeline[0] and _nr_prog(pipeline[0][0]) in ("echo", "printf"):
                last, _, _ = _nr_unwrap(list(pipeline[-1]), scratch)
                if (last and _nr_prog(last[0]) in _NR_SHELLS
                        and _nr_reads_stdin(_nr_prog(last[0]), last[1:])
                        and _nr_command_word_from(" ".join(pipeline[0][1:]), marks, depth + 1)):
                    return True
            if seg is None:
                break
            pipeline = [seg["words"]]
        else:
            pipeline.append(seg["words"])
        words, _, via = _nr_unwrap(list(seg["words"]), scratch)
        if via is not None:
            if _nr_command_word_from(via, marks, depth + 1):
                return True
            continue
        if not words:
            continue
        first, args = words[0], words[1:]
        if "$" in first or "`" in first:
            return True
        name = _nr_prog(first)
        marked = any(m in a for a in args for m in marks)
        if name in ("eval", "source", ".") and (marked or any("$" in a for a in args)):
            return True
        if name in _NR_SHELLS:
            code = _nr_code(args, ("-c", "--command"), cluster="c")
            if code is not None:
                if _nr_command_word_from(code, marks, depth + 1):
                    return True
            elif _nr_reads_stdin(name, args):
                if any(_nr_command_word_from(h, marks, depth + 1) for h in seg["here"]):
                    return True
            elif marked:
                return True                     # `sh $(x)`: the script's path is output
        elif (name in _NR_INTERPRETERS or re.fullmatch(r"python[\d.]*", name)) and (
                marked or any(m in h for h in seg["here"] for m in marks)):
            return True
    return False


def _nr_shell_program(code: str, ctx: _NrContext) -> str | None:
    """Why a shell's program text (sh -c CODE, a here-string or `echo CODE |`
    into a shell) is never run, or None.

    A $(...) or `...` from the outer line runs before the shell reads CODE,
    so its output is part of the program. One whose output is known
    (`$(echo ls)`) is put in and judged with the rest; one that prints a
    heredoc was judged as that heredoc. Any other output is unknown, and it
    is pasted into the program before the shell parses it: a `;` or a newline
    in it starts a command wherever it sits, so "only an argument" is not a
    promise. The call is refused unless every unknown output is a fixed one
    (`$(date)`) or a place the machine picks (_NR_PLACE_OUTPUT: `$(pwd)`,
    `$(git rev-parse --show-toplevel)`) that is not in a command position
    (see _nr_command_word_from). Those are noted in ctx.unknown and the call
    gets a card in every mode, which says Wisp cannot see what will run."""
    if (hit := _nr_run_output_hit(code, ctx)):
        return hit
    unknown: list[str] = []

    def put(m: re.Match) -> str:
        n = int(m.group(1))
        body = ctx.inner[n] if n < len(ctx.inner) else ""
        if _NR_PRINTS_HEREDOC.fullmatch(body):
            return m.group()
        if ctx.shadowed:                        # `echo(){ ... }` earlier in the line
            opaque.append(m.group())
            return m.group()
        if (printed := _nr_literal_output(body)) is not None:
            return printed
        ctx.unknown.append(m.group())
        if not _NR_FIXED_OUTPUT.fullmatch(body):
            unknown.append(m.group())
            if not _NR_PLACE_OUTPUT.fullmatch(body):
                opaque.append(m.group())
        return m.group()

    opaque: list[str] = []
    resolved = _NR_SUBST_RE.sub(put, code)
    if opaque or (unknown and _nr_command_word_from(resolved, unknown)):
        return _UNRESOLVED_PROGRAM
    return _nr_judge_text(resolved, ctx.child())


def _nr_reads_stdin(name: str, args: list[str]) -> bool:
    """True when an interpreter takes its program from stdin."""
    for a in args:
        if a in ("-c", "-e", "-E", "-m", "-p", "--command", "--eval"):
            return False
        if name in _NR_SHELLS and a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            return False
    operands = [a for a in args if not a.startswith("-")]
    return not operands or operands[0] == "-" or "-s" in args


def _nr_judge_words(words: list[str], ctx: _NrContext) -> str | None:
    """Why one simple command is never run, or None."""
    if ctx.depth > _NR_DEPTH:
        return _TOO_DEEP
    ctx.tick()
    words, admin, via_shell = _nr_unwrap(list(words), ctx)
    if via_shell is not None:
        return _nr_judge_text(via_shell, ctx.child())
    if not words:
        return None
    first, args = words[0], words[1:]
    name = _nr_prog(first)
    low = [a.lower() for a in args]
    if "$" in first or "`" in first:
        # A program name only known at run time: what a $(...) prints is run,
        # so a download or a heredoc it prints is judged; otherwise as if it were rm.
        return _nr_run_output_hit(first, ctx) or _nr_rm(args, ctx, admin, unknown_name=True)
    if name in ("cd", "pushd", "popd"):
        # Later relative paths are judged in the folder this leads to. Below
        # home that is a path below home; a folder only known at run time
        # (`cd "$PROJECT"`, `cd -`, `popd`, `cd ..` out of home) is None.
        dest = next((a for a in args if not a.startswith("-")), "~")
        if name == "popd" or "-" in args or dest.startswith(("+", "-")):
            ctx.cwd = None
            return None
        kind, path = _nr_target(dest, ctx.cwd)
        ctx.cwd = (_nr_place(path) if kind == "abs"
                   else ("rel", path) if kind == "rel" and not (path == ".." or path.startswith("../"))
                   else None)
        return None
    if name in ("rm", "unlink"):
        return _nr_rm(args, ctx, admin)
    if name == "srm":
        return "erases files so they can never be recovered"
    if name == "find":
        return _nr_find(args, ctx, admin)
    if name in ("eval", "source", "."):
        if any(_nr_fetches(a, ctx.inner) for a in args):
            return _FETCHED_CODE
        return _nr_judge_text(" ".join(args), ctx.child()) if name == "eval" else None
    if name in _NR_INTERPRETERS or re.fullmatch(r"python[\d.]*", name):
        if name == "osascript":
            for body in _DO_SHELL_SCRIPT.findall(" ".join(args)):
                if (hit := _nr_judge_text(re.sub(r"\\(.)", r"\1", body), ctx.child())):
                    return hit
            return None
        code = (_nr_code(args, ("-c", "--command"), cluster="c") if name in _NR_SHELLS
                else _nr_code(args, ("-c", "-e", "-E", "--eval", "-p", "--print"),
                              cluster="e" if name in ("perl", "ruby", "node", "php", "lua") else ""))
        program = code if code is not None else next((a for a in args if not a.startswith("-")), "")
        if program and _nr_fetches(program, ctx.inner):
            return _FETCHED_CODE
        if code and _NR_FORK_CODE.search(code):
            return _FORK_BOMB
        if code is not None and name in _NR_SHELLS:
            return _nr_shell_program(code, ctx)
        if code:
            return _nr_code_hit(code, ctx)
        return None
    if name == "dd":
        for a in args:
            if a.lower().startswith("of="):
                target = a[3:]
                if re.match(r"/dev/(?!null$|stdout$|stderr$|zero$|fd/)", target, re.IGNORECASE):
                    return f"writes straight onto a disk device ({target})"
                if (hit := _nr_protected_hit(_nr_full(*_nr_target(target, ctx.cwd)))):
                    return f"overwrites {hit} ({target})"
        return None
    if re.fullmatch(r"(?:mkfs|newfs)(?:[._].*)?|fdisk|pdisk", name):
        return _DISK_ERASE
    if name == "gpt" and any(a in ("destroy", "create", "remove", "migrate") for a in low):
        return _DISK_ERASE
    if name == "asr" and "restore" in low and any(a in ("--erase", "-erase") for a in low):
        return _DISK_ERASE
    if name == "diskutil":
        sub = [a for a in low if not a.startswith("-")]
        if sub and (sub[0] in _NR_DISKUTIL or (sub[0] in _NR_DISKUTIL_GROUPS and len(sub) > 1
                                                and sub[1] in _NR_DISKUTIL_SUB)):
            return _DISK_ERASE
        return None
    if name in ("chmod", "chown", "chgrp", "chflags"):
        if not any((a.startswith("-") and not a.startswith("--") and "R" in a) or a == "--recursive"
                   for a in args):
            return None
        plain = [a for a in args if not a.startswith("-")]
        ssh = f"{_nr_home()}/.ssh".casefold()
        for op in plain[1:]:
            kind, path = _nr_target(op, ctx.cwd)
            if (name == "chmod" and _nr_mode_narrows(plain[0])
                    and _within(_nr_full(kind, path) or "", [ssh])):
                continue        # `chmod -R go-rwx ~/.ssh` only tightens it
            if (kind == "dynamic" or _nr_root_like(path)
                    or (kind in ("rel", "unknown") and path
                        and (path in (".", "..") or path.startswith("../")
                             or path.casefold() in _NR_HOME_NAMES))
                    or (kind == "glob" and path == ".")):
                return f"changes permissions on every file in {op}"
        return None
    if name == "kill":
        rest = list(args)
        if rest and rest[0].startswith("-") and rest[0] != "--":
            if rest.pop(0) in ("-s", "-n") and rest:
                rest.pop(0)
        if rest and rest[0] == "--":
            rest.pop(0)
        return "stops every program you are running" if "-1" in rest else None
    if name in ("killall", "pkill"):
        if any(a.casefold() in _NR_CRITICAL_PROCESSES for a in args):
            return "stops a process macOS needs, which logs you out or freezes the Mac"
        return None
    if name in ("shutdown", "halt", "reboot", "fasthalt", "fastboot"):
        return "shuts down or restarts the Mac"
    if name == "launchctl":
        if "reboot" in low:
            return "shuts down or restarts the Mac"
        if low and low[0] in ("bootout", "unload", "remove", "disable") and any(
                re.search(r"system/|/system/|/library/launchdaemons|com\.apple\.", a) for a in low):
            return "removes a macOS system service"
        return None
    if name == "csrutil":
        return "turns off System Integrity Protection" if {"disable", "clear"} & set(low) else None
    if name == "spctl":
        return ("turns off Gatekeeper" if {"--master-disable", "--global-disable"} & set(low)
                else None)
    if name == "nvram":
        if any(a in ("-c", "-d", "-f") or "=" in a for a in args):
            return "changes the Mac's startup settings"
        return None
    if name == "tmutil":
        return "deletes or turns off Time Machine backups" if low[:1] and low[0] in _NR_TMUTIL else None
    if name == "security" and low[:1] == ["delete-keychain"]:
        return "deletes a keychain of saved passwords"
    if name == "visudo":
        return _SUDOERS
    if name == "defaults" and low[:1] == ["delete"]:
        rest = [a for a in low[1:] if a != "-currenthost" and not a.startswith("-host")]
        if not rest or (len(rest) == 1 and rest[0] in ("-g", "-globaldomain", "nsglobaldomain")):
            return "erases every global macOS preference"
        return None
    if name in ("truncate", "tee", "shred"):
        for op in (a for a in args if not a.startswith("-")):
            # tee writes content (Wisp's folder keeps its card); truncate and shred erase.
            if (hit := _nr_protected_hit(_nr_full(*_nr_target(op, ctx.cwd)), erasing=name != "tee")):
                return f"{'overwrites' if name == 'tee' else 'empties or erases'} {hit} ({op})"
        return "erases files so they can never be recovered" if name == "shred" else None
    if name in ("cp", "mv", "ditto", "install", "rsync"):
        operands = [a for a in args if not a.startswith("-")]
        emptying = name == "cp" and operands[:1] in (["/dev/null"], ["/dev/zero"])
        for op in (operands if name == "mv" else operands[-1:]):
            if (hit := _nr_protected_hit(_nr_full(*_nr_target(op, ctx.cwd)), erasing=emptying)):
                return f"{'empties' if emptying else 'overwrites or moves'} {hit} ({op})"
        if name == "mv" and len(operands) > 1:
            for op in operands[:-1]:
                full = _nr_full(*_nr_target(op, ctx.cwd))
                # Moving Wisp's own folder keeps its card (the #409/#413 design).
                if _nr_root_like(full) and _nr_protected_hit(full) != "Wisp's own data":
                    return f"moves your home folder, one of its main folders or a top folder of the Mac ({op})"
        if name == "rsync" and len(operands) > 1 and any(a.startswith("--del") for a in low):
            dest = operands[-1]
            if ":" not in dest and _nr_root_like(_nr_full(*_nr_target(dest, ctx.cwd))):
                return (f"makes {dest} match another folder, deleting whatever is only in it "
                        f"(rsync --delete into your home folder, one of its main folders or a top folder)")
        return None
    if name == "fdesetup" and low[:1] == ["disable"]:
        return "turns off FileVault disk encryption"
    if name == "pfctl" and any(a in ("-d", "-dl") for a in args):
        return "turns off the firewall"
    if name == "dscl" and low[-2:-1] in (["-delete"], ["delete"]) and re.match(
            r"/(?:local/default/)?users/[^/]+/?$", low[-1]):
        return _REMOVES_USER            # the whole record, not one of its attributes
    if name == "sysadminctl" and "-deleteuser" in low:
        return _REMOVES_USER
    if name not in _NR_READERS and any("/etc/sudoers" in a for a in low):
        return _SUDOERS
    return None


def _nr_writes_nothing(words: list[str]) -> bool:
    """Whether a command's output is empty, so redirecting it empties the file."""
    if not words:
        return True
    name, args = _nr_prog(words[0]), words[1:]
    return (name in (":", "true", "false") or (name == "cat" and args == ["/dev/null"])
            or (name == "echo" and args in (["-n"], ["-n", ""]))
            or (name == "printf" and args in ([""], ["%s", ""])))


def _nr_pipeline(stages: list[list[str]], ctx: _NrContext) -> str | None:
    """Downloaded or decoded text piped into a shell or interpreter, and
    `echo "..." | sh`, whose text is judged as the command it is."""
    fetched = False
    for words in stages:
        unwrapped, _, _ = _nr_unwrap(list(words), _NrContext(ctx.cwd, ctx.depth, ctx.inner))
        if not unwrapped:
            continue
        name = _nr_prog(unwrapped[0])
        if (fetched and (name in _NR_INTERPRETERS or re.fullmatch(r"python[\d.]*", name))
                and _nr_reads_stdin(name, unwrapped[1:])):
            return _FETCHED_CODE
        fetched = fetched or bool(_NR_FETCH.search(" ".join(unwrapped)))
    if stages[0] and _nr_prog(stages[0][0]) in ("echo", "printf"):
        last, _, _ = _nr_unwrap(list(stages[-1]), _NrContext(ctx.cwd, ctx.depth, ctx.inner))
        if last and _nr_prog(last[0]) in _NR_SHELLS and _nr_reads_stdin(_nr_prog(last[0]), last[1:]):
            printed = " ".join(stages[0][1:])
            return _nr_shell_program(printed, ctx)
    return None


def _nr_judge_text(text: str, ctx: _NrContext) -> str | None:
    """Why a command line is never run, or None."""
    if not text.strip():
        return None
    if ctx.depth > _NR_DEPTH:
        return _TOO_DEEP
    ctx.tick()
    text, run_texts, programs = _nr_heredocs(text, ctx)
    for body in run_texts:
        if (hit := _nr_judge_text(body, ctx.child())):
            return hit
    for body, kind in programs:
        if kind == "applescript":
            hits = (_nr_judge_text(re.sub(r"\\(.)", r"\1", b), ctx.child())
                    for b in _DO_SHELL_SCRIPT.findall(body))
            hit = next((h for h in hits if h), None)
        else:
            hit = _NR_FORK_CODE.search(body) and _FORK_BOMB or _nr_code_hit(body, ctx)
        if hit:
            return hit
    # Quoted text is an argument (a commit message naming a fork bomb), not
    # code; quoted code that runs (sh -c '...', eval, echo ... | sh) is judged
    # where it runs.
    bare = _nr_mask(text)
    for m in _NR_FUNC.finditer(bare):
        if re.search(r"(?<![\w:.-])" + re.escape(m.group(1)) + r"\s*(?:\||&(?!&))", m.group(2)):
            return _FORK_BOMB
    if _NR_FORK_CODE.search(bare):
        return _FORK_BOMB
    start = len(ctx.inner)
    outer = _nr_lift(text, ctx.inner)
    for body in ctx.inner[start:]:
        if (hit := _nr_judge_text(body, ctx.child())):
            return hit
    ctx.parens = ctx.parens or bool(_NR_FIND_PARENS.search(outer))
    segments = _nr_segments(outer)
    if segments is None:
        # Unbalanced quotes: judge it with the quotes dropped. A lone backslash
        # at the end still fails to parse, so then the backslashes go too.
        segments = (_nr_segments(re.sub(r"[\"']", "", outer))
                    or _nr_segments(re.sub(r"[\"'\\]", "", outer)) or [])
    pipeline: list[list[str]] = []
    for seg in segments:
        for target, append in seg["out"]:
            if re.match(r"/dev/r?disk", target, re.IGNORECASE):
                return f"writes straight onto a disk device ({target})"
            if "/etc/sudoers" in target.lower():
                return _SUDOERS
            # `> file`, `: > file`, `cat /dev/null > file` empty it; Wisp's own
            # folder is refused only for that, other writes into it keep their card.
            emptying = not append and _nr_writes_nothing(seg["words"])
            if (hit := _nr_protected_hit(_nr_full(*_nr_target(target, ctx.cwd)), erasing=emptying)):
                return f"{'empties' if emptying else 'overwrites'} {hit} ({target})"
        if seg["op"] in ("|", "|&"):
            pipeline.append(seg["words"])
        else:
            if len(pipeline) > 1 and (hit := _nr_pipeline(pipeline, ctx)):
                return hit
            pipeline = [seg["words"]]
        ctx.fed = False
        if seg["here"] and seg["words"]:
            # A here-string is the program when a shell reads its program from stdin.
            kind = _nr_stdin_program(seg["words"])
            for body in seg["here"] if kind else ():
                if (hit := (_nr_shell_program(body, ctx)
                            if kind == "shell"
                            else _nr_code_hit(body, ctx) if kind == "code"
                            else next((h for b in _DO_SHELL_SCRIPT.findall(body)
                                       if (h := _nr_judge_text(re.sub(r"\\(.)", r"\1", b), ctx.child()))),
                                      None))):
                    return hit
        if seg["words"] and (hit := _nr_judge_words(seg["words"], ctx)):
            return hit
    if len(pipeline) > 1 and (hit := _nr_pipeline(pipeline, ctx)):
        return hit
    return None


# --- one plain delete of an absolute path --------------------------------------
# Owner decision (2026-11-10, after the never-run check): a recursive delete of
# an absolute path inside an ordinary folder gets a card instead of a refusal.
#
# Three attempts to exempt such a delete from inside the parser were beaten by
# pairing a harmless visible rm with a hidden one (a variable, an alias, a
# quoted string, a launcher). So nothing is exempted from inside: the WHOLE
# command has to be `rm`, its -r/-R/-f flags (one of them recursive), and paths written with plain
# characters only, single spaces between them and nothing else. No variable,
# wildcard, chain, redirect, wrapper, newline or `--`, and no quoting beyond
# the one form described below; that text has
# one reading in any shell. Anything else keeps the refusal it had.
#
# Each path, and what it resolves to on disk (a link is judged as its target),
# must sit under home below the main folders (the rule a relative delete has),
# or under one of _PLAIN_ORDINARY, or two folders down on another disk. The call still gets the destructive-delete
# card in every mode; no grant covers it.
#
# A path may also have spaces (rc2 fix): written inside one pair of single or
# double quotes that covers the whole path, or with backslash-space outside
# quotes (`~/Library/Application\ Support/App/Cache`). Its own tokenizer
# (_plain_delete_words) accepts nothing else: inside quotes only the plain
# characters, `/` and space; no other escape; no quote that starts or ends
# inside a word; nothing the shell would expand. The decoded path takes the
# same checks, and run_shell runs exactly the decoded words, without a shell.
_PLAIN_FLAG = re.compile(r"-[rRf]{1,3}")
_PLAIN_CHAR = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+@-")
_PLAIN_QUOTED = re.compile(r"/[A-Za-z0-9._+@/ -]*")
# (folder, how many names below it a deleted path needs at least)
_PLAIN_ORDINARY = (("/tmp", 1), ("/private/tmp", 1), ("/var/folders", 4),
                   ("/private/var/folders", 4), ("/users/shared", 1),
                   ("/usr/local", 2), ("/opt", 3))
# Never carded wherever they sit: Time Machine's backups.
_PLAIN_NEVER_NAMES = frozenset({"backups.backupdb", ".timemachine", ".mobilebackups"})
# macOS's own helper volumes, when they are mounted under /Volumes.
_PLAIN_SYSTEM_DISKS = frozenset({"recovery", "preboot", "vm", "update", "xarts", "hardware",
                                 "isccache"})
_PLAIN_MAX_PATHS = 16


def _plain_delete_problem(full: str) -> bool:
    """True when an absolute path is not one a plain delete may be carded for."""
    low = full.rstrip("/").casefold()
    if not low.startswith("/") or _nr_protected_hit(full) or _nr_system_like(full):
        return True
    if _PLAIN_NEVER_NAMES & set(low.split("/")):
        return True
    # Home under the name it is given and the name the disk has for it.
    for home in dict.fromkeys((_nr_home().casefold(), os.path.realpath(_nr_home()).casefold())):
        if low == home:
            return True
        if low.startswith(home + "/"):
            return _nr_rel_problem(low[len(home) + 1:]) is not None
    for root, depth in _PLAIN_ORDINARY:
        if low.startswith(root + "/"):
            return len(low[len(root) + 1:].split("/")) < depth
    if low.startswith("/volumes/"):
        # Another disk is read as a disk of its own: nothing at its top level,
        # none of the folders a Mac's own disk has, and a /Users/<name> on it
        # by the rule home has (it can be a second mount of this Mac's data).
        disk, *inner = low[len("/volumes/"):].split("/")
        if disk in _PLAIN_SYSTEM_DISKS or len(inner) < 2 or _nr_system_like("/" + "/".join(inner)):
            return True
        if inner[0] == "users":
            return len(inner) < 3 or _nr_rel_problem("/".join(inner[2:])) is not None
        return False
    return True


_PLAIN_KNOWN_NAMES = (_NR_HOME_NAMES | _NR_TOP_NAMES | _PLAIN_NEVER_NAMES | _PLAIN_SYSTEM_DISKS
                      | {"keychains", "launchagents", "launchdaemons", "volumes", "users", "shared",
                         "folders", "local", "opt", "tmp", "var"})


def _plain_look_alike(part: str) -> bool:
    """A decoded path name with a space at either end, or one that is a known
    folder's name once spaces are dropped (`Docu ments`, `Library `): refused,
    so a card never shows a name that only looks like another."""
    if part != part.strip():
        return True
    squashed = re.sub(r"\s+", "", part).casefold()
    return squashed != part.casefold() and (
        squashed in _PLAIN_KNOWN_NAMES or squashed == _nr_home().rsplit("/", 1)[-1].casefold())


def _plain_delete_words(cmd: str) -> tuple[list[str], list[str]] | None:
    """(flags, paths as written once quoting is removed) for `rm`, its flags
    and plain absolute paths separated by single spaces, else None."""
    s = cmd.strip(" ")
    if not s.startswith("rm "):
        return None
    words: list[tuple[str, bool]] = []          # (decoded word, was quoted)
    i, n = 3, len(s)
    while True:
        if i >= n or s[i] == " ":
            return None                         # two spaces, or nothing after one
        if s[i] in "'\"":
            end = s.find(s[i], i + 1)
            if end < 0 or not _PLAIN_QUOTED.fullmatch(s[i + 1:end]):
                return None
            words.append((s[i + 1:end], True))
            i = end + 1
            if i < n and s[i] != " ":
                return None                     # no quote glued to more text
        else:
            out = []
            while i < n and s[i] != " ":
                c = s[i]
                if c == "\\":
                    if s[i + 1:i + 2] != " ":
                        return None             # the only escape is backslash-space
                    out.append(" ")
                    i += 2
                    continue
                if not (c in _PLAIN_CHAR or c == "/" or (c == "~" and not out)):
                    return None
                out.append(c)
                i += 1
            words.append(("".join(out), False))
        if i >= n:
            break
        i += 1                                  # the one space between words
    flags, paths = [], []
    for word, quoted in words:
        if not quoted and not paths and _PLAIN_FLAG.fullmatch(word):
            flags.append(word)
            continue
        if not (word.startswith("/") or (not quoted and word.startswith("~/"))):
            return None
        if "//" in word or "~" in word[1:]:
            return None
        paths.append(word)
    if not flags or not paths or not any("r" in f.lower() for f in flags):
        return None                             # a delete that is not recursive never needed this
    return flags, paths


def plain_delete_targets(args: dict) -> tuple[list[str], tuple[tuple[str, str], ...]] | None:
    """(rm's flags, ((path, what it resolves to on disk), ...)) when a run_shell
    call is nothing but one plain `rm` of absolute paths in ordinary folders
    (see above), else None. run_shell compares the second part again just
    before it runs the delete."""
    if not isinstance(args, dict) or args.get("script"):
        return None
    cmd = args.get("cmd")
    if not isinstance(cmd, str) or len(cmd) > 4000:
        return None
    parsed = _plain_delete_words(cmd)
    if parsed is None:
        return None
    flags, operands = parsed
    if len(operands) > _PLAIN_MAX_PATHS:
        return None
    targets = []
    for op in operands:
        if any(part in (".", "..") for part in op.split("/")):
            return None
        if any(_plain_look_alike(part) for part in op.split("/")):
            return None
        full = ((_nr_home() + op[1:]) if op.startswith("~") else op).rstrip("/")
        try:
            real = os.path.realpath(full)
        except (OSError, ValueError, RuntimeError):
            return None
        if _plain_delete_problem(full) or _plain_delete_problem(real):
            return None
        targets.append((full, real))
    return flags, tuple(targets)


def plain_delete_paths(args: dict) -> list[str] | None:
    """The paths a run_shell call deletes when the call is nothing but one
    plain `rm` of absolute paths in ordinary folders (see above), else None."""
    found = plain_delete_targets(args)
    return None if found is None else [full for full, _ in found[1]]


def plain_delete_argv(args: dict) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]] | None:
    """The fixed argv run_shell runs for a plain delete (no shell reads it),
    and the (path, resolved path) pairs it was judged on."""
    found = plain_delete_targets(args)
    if found is None:
        return None
    flags, targets = found
    return ("/bin/rm", *flags, "--", *(full for full, _ in targets)), targets


def _plain_delete_card(targets: tuple[tuple[str, str], ...]) -> str:
    """The card reason for a plain delete: every path, as the shell reads it.

    A path that is itself a link is named as one: rm removes only the link.
    One reached through a link the person made shows where it really is. The
    Mac's own spellings (/tmp is /private/tmp, /var is /private/var, home
    under another name) are not called links."""
    home = _nr_home()
    shown = []
    for full, real in targets:
        name = "~" + full[len(home):] if full.startswith(home + "/") else full
        if os.path.islink(full):
            shown.append(f"“{name}” (a link to “{real}”: this removes the link, not what it "
                         "points to)")
        elif real.rstrip("/") not in (full.rstrip("/"), _plain_system_spelling(full)):
            shown.append(f"“{name}” (reached through a link: it is really “{real}”) and "
                         "everything inside")
        else:
            shown.append(f"“{name}” and everything inside")
    return ("⚠ This permanently deletes " + ", ".join(shown) + ". It cannot be undone and does "
            "NOT go to the Trash. Check the paths before allowing it.")


def _plain_system_spelling(full: str) -> str:
    """`full` with only home, or its top folder (/tmp, /var, /etc), resolved:
    what realpath gives when no folder the person made is a link."""
    home = _nr_home()
    try:
        if full == home or full.startswith(home + "/"):
            return os.path.realpath(home).rstrip("/") + full[len(home):]
        top = "/" + full.split("/")[1]
        return os.path.realpath(top).rstrip("/") + full[len(top):]
    except (OSError, ValueError, RuntimeError):
        return full


def never_run_hit(text: str) -> str | None:
    """Why Wisp refuses to run this shell text in any mode, or None.

    Text longer than SHELL_TEXT_LIMIT is refused by decide() anyway; its first
    SHELL_TEXT_LIMIT characters are still judged here, so a dangerous start
    gets this refusal's wording. A check that runs past _NR_BUDGET seconds of
    CPU time (only pathological text gets near it) refuses the command."""
    return _nr_check(text)[0]


def _nr_check(text: str) -> tuple[str | None, bool]:
    """(never_run_hit, whether a shell program holds a command's output Wisp
    cannot know, as an argument only: see _nr_shell_program)."""
    if not isinstance(text, str) or not text.strip():
        return None, False
    # Wisp's own placeholder words are made while judging. One already in the
    # text (a file named like a judged heredoc body) must not pass for one.
    text = text.replace("__wisp_", "__wisp-")
    ctx = _NrContext(("rel", "."))
    ctx.shadowed = bool(_NR_SHADOWS.search(text[:SHELL_TEXT_LIMIT]))
    try:
        return _nr_judge_text(text[:SHELL_TEXT_LIMIT], ctx), bool(ctx.unknown)
    except _NrTooSlow:
        return _TOO_SLOW, False
    except RecursionError:
        return _TOO_DEEP, False
    except (ValueError, OSError):          # e.g. a NUL a path lookup rejects: fail closed
        return _TOO_SLOW, False


UNKNOWN_OUTPUT_CARD = ("⚠ This hands a shell a program that includes another command's output, "
                       "so Wisp cannot see exactly what it will run. Check the command before "
                       "allowing it.")


def _never_run(args: dict) -> tuple[Decision | None, bool]:
    """(the refusal for a run_shell `cmd`, or an AppleScript `do shell script`
    body, that Wisp never runs (see NEVER_RUN_TAIL), whether the `cmd` hands
    a shell a command's unknown output as an argument)."""
    if plain_delete_paths(args) is not None:
        return None, False
    texts = [str(args.get("cmd") or "")]
    texts += [re.sub(r"\\(.)", r"\1", body)
              for body in _DO_SHELL_SCRIPT.findall(str(args.get("script") or ""))]
    unknown = False
    for k, text in enumerate(texts):
        hit, partly = _nr_check(text)
        if hit:
            return Decision(Tier.DENY, f"Wisp won't run this command because it {_card_text(hit, 300)}. "
                            f"{NEVER_RUN_TAIL}"), False
        unknown = unknown or (partly and k == 0)
    return None, unknown


def never_run_decision(args: dict) -> Decision | None:
    """The refusal for a run_shell `cmd`, or an AppleScript `do shell script`
    body, that Wisp never runs (see NEVER_RUN_TAIL)."""
    return _never_run(args)[0]


# A review run (the corpus runner's normal mode) uses the user's ordinary path
# but must change nothing. Inside review_scope(), whatever would run without
# asking, except a read, asks instead; the approver then declines it and
# records that Wisp would have asked (agent/approver.py). Per task, so a review
# run never loosens or tightens the user's own concurrent turns.
_REVIEW: ContextVar[bool] = ContextVar("wisp_policy_review", default=False)
_REVIEW_ALLOWED_CATEGORIES = {"compute"}
# An MCP server labels its own tools; its claim that one only reads is not
# trusted in a review run.
_REVIEW_UNTRUSTED_READS = {"mcp_read"}


def review_only() -> bool:
    return _REVIEW.get()


@contextmanager
def review_scope():
    token = _REVIEW.set(True)
    try:
        yield
    finally:
        _REVIEW.reset(token)


# A picture turn (service/vision.py, "pictures that lead to actions"): the turn
# carries a reading of an image, and text in an image can be planted by anyone.
# Like a review run, only a local read runs without asking; everything else
# (a write, a send, a web lookup, a skill or MCP action) asks, even under full
# access or a standing grant, and its card offers no "Always allow"
# (agent/approver.py). Set for the /agent request's own task only.
_PICTURE: ContextVar[bool] = ContextVar("wisp_policy_picture_turn", default=False)
# Local reads whose result stays on this Mac. web_read and mcp_read are left out:
# a lookup can carry what the picture said to someone else.
_PICTURE_UNCONFIRMED_CATEGORIES = {"compute", "wisp_admin"}
_PICTURE_CONFIRMED_READS = {"mcp_read", "web_read"}
PICTURE_TURN_REASON = ("this request includes a picture, and text in a picture is not your "
                       "instruction, so anything Wisp would change, send or look up online "
                       "is confirmed, even with full access")


def picture_turn() -> bool:
    return _PICTURE.get()


def mark_picture_turn() -> None:
    """Mark the CURRENT task's context as a picture turn. /agent calls it inside
    the request's own runner task, so it ends with that request."""
    _PICTURE.set(True)


def _picture_unconfirmed(category: str) -> bool:
    return ((str(category).endswith("_read") and category not in _PICTURE_CONFIRMED_READS)
            or category in _PICTURE_UNCONFIRMED_CATEGORIES)


def decide(category: str, args: dict, tool: str | None = None) -> Decision:
    """Return the policy decision for a tool action (see _decide). In a review
    run, only reads run without asking; in a picture turn, only local reads."""
    if category == "shell" and len(shell_text(args)) > SHELL_TEXT_LIMIT:
        return _too_long_for_card(args)
    decision = _decide(category, args, tool)
    if category == "shell" and decision.tier is Tier.CONFIRM and len(shell_text(args)) > CARD_TEXT_LIMIT:
        return _too_long_for_card(args)
    if (_REVIEW.get() and decision.tier is Tier.ALLOW
            and not ((str(category).endswith("_read") and category not in _REVIEW_UNTRUSTED_READS)
                     or category in _REVIEW_ALLOWED_CATEGORIES)):
        return Decision(Tier.CONFIRM, f"review run: Wisp would ask before {tool or category}")
    if _PICTURE.get() and decision.tier is Tier.ALLOW and not _picture_unconfirmed(category):
        if category == "shell" and len(shell_text(args)) > CARD_TEXT_LIMIT:
            return _too_long_for_card(args)
        return Decision(Tier.CONFIRM, PICTURE_TURN_REASON)
    return decision


# --- scripts a command runs -------------------------------------------------
# `sh ~/x.sh` shows only `sh ~/x.sh` on a card; what it does is in the file.
# These find the files a shell or AppleScript call executes, so the card can
# show them and, after an untrusted read, a file that cannot be shown is
# refused (see agent/loop.py tool_policy).
_SCRIPT_RUNNERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "source", ".", "node", "ruby", "perl", "php",
    "osascript", "swift", "deno", "bun", "lua", "tclsh",
})
_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "-m", "-E", "--eval", "--command"})
_COMMAND_PREFIXES = frozenset({"env", "command", "exec", "nohup", "time", "nice", "sudo", "caffeinate"})
_SYSTEM_PROGRAM_DIRS = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/libexec/",
                        "/opt/homebrew/bin/", "/usr/local/bin/", "/System/", "/Library/Apple/")
_RUN_SCRIPT_FILE = re.compile(r'(?:run|load)\s+script\s+(?:POSIX\s+)?file\s+"((?:[^"\\]|\\.)*)"',
                              re.IGNORECASE)


def _shell_segments(text: str) -> list[list[str]]:
    """Simple commands in a shell line, split at ; & | ( ) and newlines."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments, current = [], []
    try:
        for token in lexer:
            if token and set(token) <= set("();<>|&\n"):
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(token)
    except ValueError:
        return []
    if current:
        segments.append(current)
    return segments


def _files_run_by(words: list[str]) -> list[str]:
    while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])
                     or words[0].rsplit("/", 1)[-1] in _COMMAND_PREFIXES):
        words = words[1:]
        while words and words[0].startswith("-"):
            words = words[1:]                 # env -i, nice -n (its number is skipped below)
        while words and re.fullmatch(r"\d+", words[0]):
            words = words[1:]
    if not words:
        return []
    first = words[0]
    name = first.rsplit("/", 1)[-1]
    if name in _SCRIPT_RUNNERS or re.fullmatch(r"python[\d.]*", name):
        for word in words[1:]:
            if word in _INLINE_CODE_FLAGS:
                return []                     # the code is on the card already
            if word.startswith("-"):
                continue
            return [word]
        return []
    if "/" in first and not first.startswith(_SYSTEM_PROGRAM_DIRS):
        return [first]                        # ./x.sh, ~/bin/thing, /Users/me/run
    return []


def executed_script_files(args: dict) -> list[str]:
    """The files a shell-category call executes, as written in the call."""
    text = shell_text(args)
    if not text or len(text) > SHELL_TEXT_LIMIT:
        return []
    script = str(args.get("script") or "")
    lines = ([re.sub(r"\\(.)", r"\1", body) for body in _DO_SHELL_SCRIPT.findall(script)]
             if script else [str(args.get("cmd") or "")])
    found = [re.sub(r"\\(.)", r"\1", path) for path in _RUN_SCRIPT_FILE.findall(script)]
    for line in lines:
        for words in _shell_segments(line):
            found.extend(_files_run_by(words))
    return list(dict.fromkeys(found))


def read_script_file(raw: str) -> tuple[str | None, str | None]:
    """(the file's text, None) or (None, why it cannot be shown). Relative
    paths are read from home, where run_shell runs."""
    if re.search(r"[$`*?\[\]{}]", raw):
        return None, f"{raw} is built at run time, so Wisp cannot show what it contains"
    try:
        path = Path(os.path.expanduser(raw))
        path = path if path.is_absolute() else Path.home() / path
        if not path.is_file():
            return None, f"Wisp cannot find {raw} to show what it contains"
        if path.stat().st_size > CARD_TEXT_LIMIT:
            return None, f"{raw} is too long to show for confirmation"
        return path.read_bytes().decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"{raw} is not a text script Wisp can show"
    except OSError:
        return None, f"Wisp cannot read {raw} to show what it contains"


def script_file_problem(args: dict) -> Decision | None:
    """A refusal when a call runs a script file the card could not show whole."""
    for raw in executed_script_files(args):
        _, problem = read_script_file(raw)
        if problem:
            return Decision(Tier.DENY, f"this runs {raw}, and {problem}, so Wisp did not run it. "
                            "If the user wants it run, they can run it themselves.")
    return None


def _too_long_for_card(args: dict) -> Decision:
    """A command that needs a card but is too long for the card to show whole.
    Approving it would approve a tail nobody read, so it is refused."""
    return Decision(Tier.DENY, f"this command is {len(shell_text(args)):,} characters long, too long "
                    f"to show in full for confirmation (the limit is {CARD_TEXT_LIMIT:,}), so Wisp "
                    "did not run it. If the user wants it run, they can run it themselves.")


def _card_text(value, limit: int = 200) -> str:
    """A model-chosen argument as one line of a card reason, cut to fit. The
    card shows the call's full arguments as well; this only names the change."""
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _fs_change(category: str, tool: str | None, args: dict) -> str:
    """What an fs_write/fs_delete call changes, worded for its card.

    The memory tools reuse these categories for their access-mode rules but
    take no `path`, so the generic f"{verb} {path}" rendered their cards as
    "writing " and "deleting " with nothing after them: a prompt asking the
    user to approve a change it never named. move_path takes source and
    destination, not `path`, and read the same way.
    """
    if tool == "remember":
        replacing = args.get("supersedes_id")
        suffix = f" (replacing memory {_card_text(replacing, 64)})" if replacing not in (None, "") else ""
        return f"saving a memory: {_card_text(args.get('fact'))}{suffix}"
    if tool == "forget":
        return f"forgetting: {_card_text(args.get('query'))}"
    if tool == "save_knowledge":
        return f"saving to your knowledge: {_card_text(args.get('title'))}"
    if tool == "edit_knowledge":
        return (f"replacing saved knowledge item {_card_text(args.get('record_id'), 64)} "
                f"with “{_card_text(args.get('title'))}”")
    if tool == "delete_knowledge":
        return f"deleting saved knowledge item {_card_text(args.get('record_id'), 64)}"
    if tool == "study_make":
        return f"making {_card_text(args.get('kind'), 20)} from your slides with the Study model"
    if tool == "move_path":
        return f"moving {_card_text(args.get('source'))} to {_card_text(args.get('destination'))}"
    verb = "deleting" if category == "fs_delete" else "writing"
    return f"{verb} {_card_text(args.get('path'))}"


# manage_timers actions that only report: "how long is left", the stopwatch's
# current reading. The tool defaults a missing or empty action to "list". They
# used to fall to "unclassified action" with every other timer_write call, so
# "how much time is left on the pasta timer" showed a confirmation card in the
# default mode and was refused outright in view-only.
_TIMER_READ_ACTIONS = frozenset({"list", "stopwatch_read"})
_STOPWATCH_CHANGES = {"stopwatch_start": "starts the stopwatch",
                      "stopwatch_lap": "records a lap on the stopwatch",
                      "stopwatch_stop": "stops the stopwatch"}


def _timer_read(tool: str | None, args: dict) -> bool:
    if tool != "manage_timers":
        return False
    action = args.get("action")
    if action is None or action == "":
        return True
    # Anything that is not a plain read action, including a non-string the
    # tool would choke on, takes the write rules.
    return isinstance(action, str) and action.strip().lower() in _TIMER_READ_ACTIONS


def _timer_change(tool: str | None, args: dict) -> str:
    """The timer_write change, worded for its card."""
    if tool == "set_timer":
        kind = str(args.get("kind") or "timer").strip().lower()
        if kind == "alarm":
            return f"sets an alarm for {_card_text(args.get('time'), 40)}"
        if kind == "sleep":
            return f"pauses playback in {_card_text(args.get('duration'), 40)}"
        return f"sets a timer for {_card_text(args.get('duration'), 40)}"
    action = str(args.get("action") or "").strip().lower()
    if action == "cancel":
        return f"cancels the timer {_card_text(args.get('label'), 80)}"
    if action == "cancel_all":
        return "cancels every timer and alarm"
    return _STOPWATCH_CHANGES.get(action, "changes your timers")


def _contact_change(args: dict) -> str:
    """The manage_contacts change, worded for its always-confirm card."""
    action = str(args.get("action") or "").strip().lower()
    name, value = _card_text(args.get("name"), 80), _card_text(args.get("value"), 120)
    # The tool writes to the one card whose name *contains* `name`, so the
    # card says "matching", not "Mom's": a lone "Tom Mommsen" would match.
    if action == "add_phone":
        return f"adds the phone number {value} to the contact matching “{name}”"
    if action == "add_email":
        return f"adds the email address {value} to the contact matching “{name}”"
    if action == "create":
        return f"creates a new contact named “{name}”"
    return f"changes the contact matching “{name}”"


def _decide(category: str, args: dict, tool: str | None = None) -> Decision:
    """Return the policy decision for a tool action.

    `category` is the tool's safety category (shell, fs_read, fs_write,
    fs_delete, app_control, network, screen). `args` are the tool arguments.
    `tool` is the tool's name, used to look up standing grants the user has
    given ("always allow this") — see service/safety/grants.py. Optional so
    older call sites keep working; without it, only the category rules apply.
    """
    # Remote inference denies only tools that need on-Mac models/extensions
    # and otherwise falls through to the ordinary rules below (floor,
    # always-confirm sends, grants, view-only mode) unchanged.
    from service.inference.remote_policy import remote_only_profile, remote_tool_allowed
    if remote_only_profile() is not None and not remote_tool_allowed(tool):
        return Decision(Tier.DENY, "This tool needs on-Mac models or extensions, "
                        "which this remote-inference build does not run")
    # The floor runs before everything, including full_access and grants.
    # `disable_safety_floor` only ever meant "let full_access mean literally
    # everything" — outside full_access these checks have always been
    # unconditional, and stay that way.
    # A plain grep or a validated read cannot open a connection, so a search
    # through docs that mention the port is not refused.
    if category == "shell" and not _cannot_reach_network(args):
        text = shell_text(args)
        if local_send_endpoint_hit(text):
            return Decision(Tier.DENY, "Wisp's Send buttons are for the user's own tap; "
                            "commands may not call them")
        if local_service_hit(text):
            return Decision(Tier.DENY, "commands may not call Wisp's own service (port 8765); "
                            "use Wisp's tools instead")
    # Very dangerous commands (never_run_decision): refused in every mode, even
    # with the configurable floor switched off, so nothing below can card them.
    never, unknown_output = _never_run(args) if category == "shell" else (None, False)
    if never is not None:
        return never
    if (_KEEP_FLOOR or not _FULL_ACCESS) and (floor := _hard_deny(category, args, tool)) is not None:
        return floor
    # A shell program holding another command's output as an argument (see
    # _nr_shell_program): unknown, not known to be dangerous, so a card in
    # every mode, before grants, so no Always allow and no access mode skips it.
    if unknown_output:
        if read_only():
            return Decision(Tier.DENY, "view-only: only read-only commands are allowed")
        return Decision(Tier.CONFIRM, UNKNOWN_OUTPUT_CARD)
    # One plain delete of absolute paths: a card in every mode, whatever the
    # person's own destructive_shell list says, and before grants, so no saved
    # Always allow and no access mode skips it. The card names every path.
    if category == "shell" and (plain := plain_delete_targets(args)) is not None:
        if read_only():
            return Decision(Tier.DENY, "view-only mode: deleting is disabled")
        return Decision(Tier.CONFIRM, _plain_delete_card(plain[1]), delete_targets=plain[1])

    if tool == "organize_files":
        if not args.get("confirm", False):
            return Decision(Tier.ALLOW, "file listing preview only; no files move")
        if read_only():
            return Decision(Tier.DENY, "view-only mode: moving files is disabled")
        if not args.get("preview_token"):
            return Decision(Tier.DENY, "a matching file preview is required before moving")
        return Decision(Tier.CONFIRM, "attempts approved moves; exact bytes require stable sources; "
                        "unknown results may include completed moves and must not be retried automatically")

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
    # to="sam.rivera@example.com" — the user's OWN email, present in the
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
    destructive = (next((hit for form in shell_forms(args) if (hit := is_destructive_shell(form))),
                        None) if category == "shell" else None)
    # Shell text that can send something off this Mac or read a secret, and a
    # file tool aimed at a credential. Computed before grants for the same
    # reason: a grant on `curl` was never agreement to every upload.
    outbound = ((shell_egress_hit(args) or shell_secret_hit(args))
                if category == "shell" and tool not in ALWAYS_CONFIRM_TOOLS
                else app_open_hit(args) if tool == "open_app" else None)
    secret = (credential_path_hit(args, tool, mutating=category != "fs_read")
              if category in ("fs_read", "fs_write", "fs_delete") else None)
    # A Mac setting switched off that cuts the network, a device or a
    # protection (_TOGGLE_OFF_CUTS). A grant on "wifi" was given for a call the
    # user saw, not for every future Wi-Fi off.
    cut_off = toggle_off_hit(tool, args)

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
        if (verdict == "allow" and category not in ALWAYS_CONFIRM_CATEGORIES
                and tool not in ALWAYS_CONFIRM_TOOLS and tool not in ALWAYS_CONFIRM_FILE_TOOLS
                and tool not in ALWAYS_CONFIRM_DELETE_TOOLS
                and not destructive and not outbound and not secret and not cut_off
                and grants.is_grantable(tool, args)):
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

    if tool in ALWAYS_CONFIRM_TOOLS:
        if read_only():
            return Decision(Tier.DENY, "view-only mode: AppleScript is disabled")
        return Decision(Tier.CONFIRM, "AppleScript can send messages and mail as you, or run "
                        "shell commands — always confirmed. Check the script before allowing it.")

    if tool in ALWAYS_CONFIRM_FILE_TOOLS:
        if read_only():
            return Decision(Tier.DENY, "view-only mode: saving files is disabled")
        return Decision(Tier.CONFIRM, "saves a file somebody sent onto this Mac — always confirmed; "
                        "the card names the exact file")

    if tool in ALWAYS_CONFIRM_DELETE_TOOLS:
        if read_only():
            return Decision(Tier.DENY, "view-only mode: changing notes is disabled")
        from service.tools.notes_write import delete_note_reason
        return Decision(Tier.CONFIRM, delete_note_reason(args.get("title"), args.get("folder")))

    if outbound and tool == "open_app":
        if read_only():
            return Decision(Tier.DENY, f"view-only mode: {category} is disabled")
        return Decision(Tier.CONFIRM, f"this {outbound} — it can start a call, a message or a "
                        "Shortcut, so it is always confirmed. Check the app and target.")
    if outbound:
        if read_only():
            return Decision(Tier.DENY, "view-only: only read-only commands are allowed")
        return Decision(Tier.CONFIRM, "this command can send something off this Mac, control "
                        f"another app, or read a secret (matched: {outbound}) — always confirmed. "
                        "Check the command before allowing it.")

    if secret:
        if read_only():
            return Decision(Tier.DENY, "view-only mode: keys and saved sign-ins are off limits")
        return Decision(Tier.CONFIRM, f"{secret} holds keys or saved sign-ins — always confirmed")

    if cut_off:
        if read_only():
            return Decision(Tier.DENY, "view-only mode: system changes are disabled")
        return Decision(Tier.CONFIRM, f"turns {cut_off} off, which can disconnect this Mac or "
                        "its devices — confirmed even with full access")

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

    # Contacts writes redirect every later send to that person
    # (_ALWAYS_CONFIRM_CONTACTS), so full access does not skip the card.
    if category in _ALWAYS_CONFIRM_CONTACTS:
        if read_only():
            return Decision(Tier.DENY, "view-only mode: changing contacts is disabled")
        return Decision(Tier.CONFIRM, f"{_contact_change(args)} — messages and email to them can "
                        "go wherever their card points, so this is always confirmed")

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

    # Skill and MCP actions ask in every mode, and a grant above cannot answer
    # them (_ALWAYS_CONFIRM_EXTENSION). mcp_effect_word only words the card.
    if category == "mcp_action":
        if read_only():
            return Decision(Tier.DENY, "view-only mode: MCP actions are disabled")
        word = mcp_effect_word(tool)
        reason = ("an MCP tool Wisp cannot see inside" if word is None
                  else f"an MCP tool that can {word}" if " " in word
                  else f"an MCP tool that can {word} things outside Wisp")
        return Decision(Tier.CONFIRM, reason + " — confirmed even with full access")
    if category == "skill_tool":
        if read_only():
            return Decision(Tier.DENY, "view-only mode: skill tools are disabled")
        return Decision(Tier.CONFIRM, "a tool from an installed skill runs a command Wisp "
                        "cannot see inside — confirmed even with full access")

    if _FULL_ACCESS:
        # Auto-allow everything else; the floor and every confirm above already ran.
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
        if ro:
            return Decision(Tier.DENY, "view-only mode: modifying files is disabled")
        return Decision(Tier.CONFIRM, _fs_change(category, tool, args))

    # create_note / append_note write into Notes.app, and set_timer /
    # manage_timers start or cancel something that fires later. Ordinary local
    # writes: nothing leaves the Mac and nothing is destroyed beyond a timer, so
    # they take the plain access-mode rules (full access ran above, view-only
    # refuses, the default mode asks). Until these rules existed both
    # categories fell to "unclassified action" below, which happened to give
    # the same tiers but named nothing on the card.
    if category == "notes_write":
        if ro:
            return Decision(Tier.DENY, "view-only mode: changing notes is disabled")
        title = _card_text(args.get("title"), 120)
        return Decision(Tier.CONFIRM, f"adds to the note “{title}” in Notes" if tool == "append_note"
                        else f"creates the note “{title}” in Notes")
    if category == "timer_write":
        if _timer_read(tool, args):
            return Decision(Tier.ALLOW, "reading your timers")
        if ro:
            return Decision(Tier.DENY, "view-only mode: timers and alarms are disabled")
        return Decision(Tier.CONFIRM, _timer_change(tool, args))

    if category in ("app_control", "network"):
        if ro:
            return Decision(Tier.DENY, f"view-only mode: {category} is disabled")
        return Decision(Tier.CONFIRM, f"{category} action")

    # Tools from a configured MCP server (service/mcp). A server the user
    # pointed Wisp at is trusted enough to run, not trusted enough to decide
    # its own permissions — see _category_for for why only a read annotation
    # can lower the tier, and never the reverse. Every mcp_action asks, in
    # every mode (_ALWAYS_CONFIRM_EXTENSION, above, answers first); only a tool
    # the user listed as trusted_read_only (mcp_read) runs without a card.
    if category == "mcp_read":
        return Decision(Tier.ALLOW, "read-only MCP tool")
    if category == "mcp_action":
        if ro:
            return Decision(Tier.DENY, "view-only mode: MCP actions are disabled")
        return Decision(Tier.CONFIRM, "running a tool from an MCP server")

    # A tool contributed by an installed skill (service/skills/tools.py). It
    # runs a real command, and the skill file itself is just a folder someone
    # dropped in, so it asks in every mode no matter what the skill claims
    # about itself (_ALWAYS_CONFIRM_EXTENSION, above, answers first); neither
    # full access nor a standing grant covers it.
    if category == "skill_tool":
        if ro:
            return Decision(Tier.DENY, "view-only mode: skill tools are disabled")
        return Decision(Tier.CONFIRM, "running a tool from an installed skill")

    # Arithmetic, unit conversion and world clocks read nothing and change
    # nothing. They used to fall through to "unclassified": denied in view-only
    # and a confirmation card in the default mode, for "convert 72 F to C"
    # (2026-10-29 Ask run).
    if category == "compute":
        return Decision(Tier.ALLOW, "a calculation")

    # Wisp describing itself (wisp_status, wisp_capabilities, wisp_skills,
    # wisp_mcp) only reads its own state. It also fell through to
    # "unclassified", so "is my data private?" got a guess instead.
    if category == "wisp_admin":
        return Decision(Tier.ALLOW, "reading Wisp's own state")

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


# Categories whose results carry text Wisp did not write and the user did not
# type in this turn: files and PDFs, web pages, mail, messages, notes, calendar
# events and Wisp's own digests of them (assistant_read: get_recent_activity,
# daily_brief, get_upcoming...), the screen, MCP servers and skills. The
# clipboard and most shell output count too (see turn_reads_untrusted). Once a
# turn has read any of it, an opaque outbound action could be following
# instructions planted there.
UNTRUSTED_CONTENT_CATEGORIES = frozenset({
    "fs_read", "web_read", "email_read", "messages_read", "notes_read", "assistant_read",
    "browser_history_read", "screen", "mcp_read", "mcp_action", "skill_tool",
})
# Window titles, and text other people wrote (installed skills, MCP servers).
UNTRUSTED_CONTENT_TOOLS = frozenset({"clipboard_read", "list_running_apps", "wisp_skills", "wisp_mcp"})
# Validated read-only programs whose output is not file text: names and counts.
_NON_CONTENT_READS = frozenset({"pwd", "echo", "whoami", "uname", "ls", "wc"})


def turn_reads_untrusted(tool: str, category: str, args: dict) -> bool:
    """Whether running this call puts outside text into the turn. Every shell
    or AppleScript call does, except a validated pwd/echo/whoami/uname/ls/wc:
    a full-access shell can print any file, page or message."""
    if category in UNTRUSTED_CONTENT_CATEGORIES or tool in UNTRUSTED_CONTENT_TOOLS:
        return True
    if category != "shell":
        return False
    cmd = args.get("cmd")
    if args.get("script") or not isinstance(cmd, str):
        return True
    argv = read_only_shell_argv(cmd)
    if argv is None or argv[0].rsplit("/", 1)[-1] not in _NON_CONTENT_READS:
        return True
    # An `ls` of a named folder (Downloads, Desktop...) lists names other
    # people chose; a bare `ls` of home does not.
    return argv[0].endswith("/ls") and any(not a.startswith("-") for a in argv[1:])


# Categories whose actions are opaque to the host in the same way as
# OPAQUE_OUTBOUND_TOOLS: what they do is decided by text Wisp cannot inspect.
OPAQUE_ACTION_CATEGORIES = frozenset({"mcp_action", "skill_tool"})


def opaque_outbound_decision(tool: str, category: str, args: dict, decision: Decision, *,
                             reason: str, categories: frozenset[str] = frozenset()) -> Decision:
    """Turn an ALLOW for an opaque outbound action into a CONFIRM.

    Shared by both routes in agent/loop.py, both passing
    OPAQUE_ACTION_CATEGORIES (skill and MCP actions): model-led routing applies
    it to every call, legacy routing once the turn has read untrusted content.
    Left at the default, only OPAQUE_OUTBOUND_TOOLS are covered. Full access and standing grants do
    not cover an action whose real-world effect the host cannot inspect. A
    validated read-only shell command is not opaque and keeps its ALLOW.
    """
    if decision.tier is not Tier.ALLOW:
        return decision
    from service.router.model_led import OPAQUE_OUTBOUND_TOOLS
    if tool not in OPAQUE_OUTBOUND_TOOLS and category not in categories:
        return decision
    if (tool == "run_shell" and set(args) == {"cmd"}
            and read_only_shell_argv(args.get("cmd")) is not None):
        return decision
    if category == "shell" and len(shell_text(args)) > CARD_TEXT_LIMIT:
        return _too_long_for_card(args)
    return Decision(Tier.CONFIRM, reason)
