"""Policy engine — decides allow / confirm / deny for every tool action.

Deterministic and rule-based on purpose. Reads optional overrides from
service/config/policy.yaml but ships with safe defaults so it works standalone.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import chain
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import unquote

import yaml

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
# fix is a per-launch secret the send routes require; that is not built yet.
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
_ACTING_APPS = re.compile(r"^(?:shortcuts|terminal|iterm2?)(?:\.app)?$",
                          re.IGNORECASE)


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
    if _ACTING_APPS.match(name):
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
_PATH_NAME_TOOLS = frozenset({"uninstall_app"})


def _path_operands(args: dict, tool: str | None = None) -> list[tuple[str, str]]:
    """(argument name, value) for every string location in `args`, list items included."""
    found: list[tuple[str, str]] = []
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        # archive_files alone treats an empty/whitespace optional output as
        # a default beside its first input, not Path('') at cwd.
        if tool == "archive_files" and key == "archive_path" and isinstance(value, str) and not value.strip():
            continue
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
    if tool == "archive_files":
        paths, output = args.get("paths"), args.get("archive_path", "")
        if not isinstance(paths, list) or not paths or not isinstance(paths[0], str) or not isinstance(output, str):
            return
        first = Path(paths[0]).expanduser()
        if output.strip():
            target = Path(output).expanduser()
            if target.suffix != ".zip":
                target = target.with_suffix(".zip")
        else:
            target = first.parent / f"{first.stem or first.name}.zip"
        yield "archive output", str(target)
    elif tool in {"convert_file", "write_document", "spreadsheet_ops", "encrypt_file"}:
        if not isinstance(args.get("path"), str):
            return
        target = Path(args["path"]).expanduser()
        if tool == "convert_file":
            fmt = args.get("to_format", "")
            if not isinstance(fmt, str):
                return
            target = target.with_suffix("." + fmt.strip().lower().lstrip("."))
        elif tool == "encrypt_file":
            target = target.with_suffix(target.suffix + (".dec" if args.get("decrypt", False) else ".enc"))
        else:
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

# View-only mode: the app may READ/inspect anything but never modify the machine.
# Default ON — a local assistant shouldn't be able to change things unless you opt in.
_READ_ONLY = bool(_CFG.get("read_only", True))


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
_FULL_ACCESS = bool(_CFG.get("full_access", False))
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


# EVERY category that must be confirmed on every call. One definition, read by
# both decide() (a standing grant cannot lift these) and grants (an allow rule
# cannot be stored for, or honoured on, a tool in one of these). Before this
# existed grantability was a hand-kept list of tool names that missed
# update_event, clear_reminders, clear_past_reminders, forward_email and
# http_request (audit H-1), and decide() consulted grants against a different,
# smaller set. A new tool in any of these categories is covered automatically.
ALWAYS_CONFIRM_CATEGORIES = frozenset(
    _ALWAYS_CONFIRM | _ALWAYS_CONFIRM_OUTBOUND | _ALWAYS_CONFIRM_CALENDAR
    | _ALWAYS_CONFIRM_TOOL_AUTHORING)

# Tools confirmed on every call, in every mode, whatever their category says.
# run_applescript is category "shell", but its text drives other apps: one line
# sends an iMessage or a Mail message as the user, and `do shell script` is a
# full shell. Full access and standing grants do not cover it (grants.py
# refuses to store an allow for it).
ALWAYS_CONFIRM_TOOLS = frozenset({"run_applescript"})


# Where keys, tokens and Wisp's own grants live. Reading one puts a secret in
# the model's context; writing one can rewrite who Wisp trusts. Either asks
# first in every mode (view-only refuses), and a grant cannot lift it.
def _credential_roots() -> list[Path]:
    from service.paths import MOE_DIR
    home = Path.home()
    roots = [home / ".ssh", home / ".aws", home / ".gnupg", home / ".netrc",
             home / ".config" / "gh", home / ".config" / "gcloud", home / "Library" / "Keychains"]
    for moe in dict.fromkeys((MOE_DIR, home / ".moe")):
        roots += [moe / "gmail.json", moe / "mcp.json", moe / "google_workspace",
                  moe / "grants.json",
                  # /resetwisp's commit marker: its presence erases Wisp's data at the next start.
                  moe / ".reset-pending"]
    return roots


_SHELL_SECRET = [
    r"(?<![\w-])\.(?:ssh|aws|gnupg|netrc)(?![\w-])",
    r"\.config/(?:gh|gcloud)\b", r"\bkeychains?\b", r"\bsecurity\s+(?:find|dump|export)-",
    r"\b(?:gmail|mcp|grants)\.json\b", r"\bgoogle_workspace\b", r"\.reset-pending\b",
]


def _within(path: str, roots: list[str]) -> bool:
    p = path.rstrip("/").casefold()       # APFS is case-insensitive by default
    return any(p == r or p.startswith(r + "/") for r in roots)


def credential_path_hit(args: dict, tool: str | None = None) -> str | None:
    """The credential location a file tool's arguments name, or None."""
    operands = _path_operands(args, tool)
    if not operands:
        return None
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


def shell_secret_hit(args: dict) -> str | None:
    """The credential location a shell-category call names, or None."""
    cmd = str(args.get("cmd") or "")
    if not args.get("script") and len(cmd) <= SHELL_TEXT_LIMIT:
        if read_only_shell_argv(cmd) is not None:
            return None               # validated reads never reach hidden paths or ~/Library
        if _cannot_reach_network(args):
            cmd = _grep_file_operands(cmd)
            return next((hit for form in _quote_forms(cmd)
                         if (hit := _first_match(_SHELL_SECRET, form))), None)
    return next((hit for form in shell_forms(args)
                 if (hit := _first_match(_SHELL_SECRET, form))), None)


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


def _hard_deny(category: str, args: dict, tool: str | None = None) -> Decision | None:
    """The unconditional safety floor: rules nothing can override — not
    full_access, not a standing grant, not the user clicking "always allow".

    Split out of decide() so grants have something to be checked *after*. A
    grant that could lift one of these wouldn't be a permission model, it would
    be a bypass.
    """
    if category == "shell":
        # run_applescript's `do shell script` bodies and quote-stripped text too.
        for cmd in shell_forms(args):
            if (hit := _any(_rules("shell_deny", _SHELL_DENY), cmd)):
                return Decision(Tier.DENY, f"matches blocked pattern: {hit}")
    if category in ("fs_write", "fs_delete"):
        rules = _rules("path_deny", _PATH_DENY)
        operands = _path_operands(args, tool)
        if tool in _PATH_NAME_TOOLS and isinstance(args.get("name"), str):
            # uninstall_app strips/appends .app, then joins each Applications
            # root. Admit a basename only; an absolute name replaces the root.
            name = args["name"].strip()
            if not name or "/" in name or "\x00" in name or name in {".", ".."}:
                return Decision(Tier.DENY, "uninstall_app requires an application basename, not a path")
            if not name.endswith(".app"):
                name += ".app"
            operands.extend(("name", str(root / name))
                            for root in (Path("/Applications"), Path.home() / "Applications"))
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


def decide(category: str, args: dict, tool: str | None = None) -> Decision:
    """Return the policy decision for a tool action (see _decide). In a review
    run, only reads run without asking."""
    if category == "shell" and len(shell_text(args)) > SHELL_TEXT_LIMIT:
        return _too_long_for_card(args)
    decision = _decide(category, args, tool)
    if category == "shell" and decision.tier is Tier.CONFIRM and len(shell_text(args)) > CARD_TEXT_LIMIT:
        return _too_long_for_card(args)
    if (_REVIEW.get() and decision.tier is Tier.ALLOW
            and not ((str(category).endswith("_read") and category not in _REVIEW_UNTRUSTED_READS)
                     or category in _REVIEW_ALLOWED_CATEGORIES)):
        return Decision(Tier.CONFIRM, f"review run: Wisp would ask before {tool or category}")
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
    if (_KEEP_FLOOR or not _FULL_ACCESS) and (floor := _hard_deny(category, args, tool)) is not None:
        return floor

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
    destructive = (next((hit for form in shell_forms(args) if (hit := is_destructive_shell(form))),
                        None) if category == "shell" else None)
    # Shell text that can send something off this Mac or read a secret, and a
    # file tool aimed at a credential. Computed before grants for the same
    # reason: a grant on `curl` was never agreement to every upload.
    outbound = ((shell_egress_hit(args) or shell_secret_hit(args))
                if category == "shell" and tool not in ALWAYS_CONFIRM_TOOLS
                else app_open_hit(args) if tool == "open_app" else None)
    secret = (credential_path_hit(args, tool)
              if category in ("fs_read", "fs_write", "fs_delete") else None)

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
                and tool not in ALWAYS_CONFIRM_TOOLS and not destructive
                and not outbound and not secret and grants.is_grantable(tool, args)):
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

    # An MCP action asks even under full access unless its name plainly only
    # reads (mcp_effect_word). A grant above can still answer it.
    if category == "mcp_action" and (word := mcp_effect_word(tool)):
        if read_only():
            return Decision(Tier.DENY, "view-only mode: MCP actions are disabled")
        reason = (f"an MCP tool that can {word}" if " " in word
                  else f"an MCP tool that can {word} things outside Wisp")
        return Decision(Tier.CONFIRM, reason + " — confirmed even with full access")

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
    # can lower the tier, and never the reverse. Outside full access every
    # mcp_action asks. Under full access only a plain lookup runs without a
    # card (mcp_effect_word, above).
    if category == "mcp_read":
        return Decision(Tier.ALLOW, "read-only MCP tool")
    if category == "mcp_action":
        if ro:
            return Decision(Tier.DENY, "view-only mode: MCP actions are disabled")
        return Decision(Tier.CONFIRM, "running a tool from an MCP server")

    # A tool contributed by an installed skill (service/skills/tools.py). It
    # runs a real command, and the skill file itself is just a folder someone
    # dropped in, so outside full access it asks no matter what the skill
    # claims about itself (under full access it runs, inside the skill
    # sandbox). A user who trusts a specific skill tool can grant it standing
    # approval; the skill can't grant that to itself.
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

    Shared by both routes in agent/loop.py. Model-led routing applies it to
    every call; legacy routing applies it (with OPAQUE_ACTION_CATEGORIES) once
    the turn has read untrusted content. Full access and standing grants do
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
