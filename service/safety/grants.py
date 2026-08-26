"""Standing permission grants — "always allow this, don't ask again".

The policy engine (policy.py) is deterministic and rule-based, and that stays
true: it decides what a *category* of action is worth asking about. What it
couldn't express is the user's own answer persisting past the moment they gave
it. Before this, every confirm-tier action re-prompted forever, which pushes
people toward the only escape hatch available — flipping on global full_access,
which disables confirmation for EVERYTHING. A per-tool grant is the narrow
version of that choice.

Grants live at ~/.moe/grants.json and are keyed by tool name, optionally
narrowed by a scope string (a recipient address, a URL host, a path prefix) so
"always allow emailing my team" doesn't become "always allow emailing anyone".

Two rules keep this from becoming a hole in the safety model:

1. A grant can never override a DENY. policy.decide() consults grants only in
   the confirm branch, after every deny rule has already run.
2. Tools in `_NEVER_GRANTABLE` can't be allow-listed at all. Sending mail and
   messages under the user's own identity is irreversible and visible to other
   people; a stray "always" click there is exactly the mistake worth making
   impossible rather than merely unlikely.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from service.paths import MOE_DIR

GRANTS_PATH = MOE_DIR / "grants.json"

# Outbound actions under the user's identity, plus installing self-authored
# code. Always ask, every time, no standing grant available. See the module
# docstring. Note create_tool is here but the TOOL IT INSTALLS is not — once a
# tool exists and the user has seen it run, granting standing approval for
# THAT tool (a normal skill_tool) is a legitimate, narrower decision they can
# make afterward.
# schedule_send and reply_to_email join the originals for the same reason: both
# deliver real mail/messages under the user's identity. schedule_send is if
# anything the least grantable of all — its approval covers a send that happens
# later, unattended, so an "always" there would hand over blanket authority to
# deliver anything at any future time.
_NEVER_GRANTABLE = {"send_email", "send_message", "create_tool",
                    "reply_to_email", "schedule_send",
                    # 2026-08-18: cancel_event/add_calendar_event moved to
                    # policy.py's always-confirm _ALWAYS_CONFIRM_CALENDAR
                    # after an unconfirmed bulk cancel wrongly removed an
                    # event and fabricated a wrongly-dated duplicate. A
                    # standing "always allow" would silently defeat that —
                    # same reasoning as the outbound-send tools above: a
                    # one-time approval of "cancel this event" is not the
                    # user agreeing to every future cancel_event call.
                    "cancel_event", "add_calendar_event"}

_lock = threading.Lock()


def _load() -> dict:
    try:
        if GRANTS_PATH.exists():
            data = json.loads(GRANTS_PATH.read_text())
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001 — a corrupt file must not break every turn
        pass
    return {}


_CACHE: dict = _load()


def _save() -> None:
    try:
        GRANTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        GRANTS_PATH.write_text(json.dumps(_CACHE, indent=2))
    except Exception:  # noqa: BLE001
        pass


def scope_for(tool: str, args: dict) -> str:
    """The narrowing key for a grant on this call, or "" for tool-wide.

    Chosen per tool so a grant stays as specific as it can be while still
    matching the next similar call: the host for a web request (not the full
    URL with its query string, which would never repeat), the directory for a
    file write (not the exact filename).
    """
    try:
        if tool in ("web_fetch", "http_request"):
            from urllib.parse import urlparse
            return (urlparse(str(args.get("url", ""))).hostname or "").lower()
        if tool in ("write_file", "delete_path", "read_file"):
            p = Path(str(args.get("path", "")))
            return str(p.parent) if p.name else str(p)
        if tool == "run_shell":
            # First word only — the program being run. The full command line
            # never repeats verbatim, so anything more specific would make the
            # grant useless, and anything less specific ("all shell") is too
            # broad to be a *grant* rather than just full_access.
            return str(args.get("cmd", "")).strip().split()[0] if args.get("cmd") else ""
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _entry(tool: str) -> dict:
    return _CACHE.setdefault(tool, {"allow": [], "deny": []})


def check(tool: str, args: dict) -> str | None:
    """"allow" / "deny" if a standing grant covers this call, else None.

    A stored scope of "" is tool-wide and matches any call.
    """
    entry = _CACHE.get(tool)
    if not entry:
        return None
    scope = scope_for(tool, args)
    for rule in entry.get("deny", []):
        if rule.get("scope", "") in ("", scope):
            return "deny"
    if tool in _NEVER_GRANTABLE:
        return None
    for rule in entry.get("allow", []):
        if rule.get("scope", "") in ("", scope):
            return "allow"
    return None


def is_grantable(tool: str, args: dict | None = None) -> bool:
    """Whether an "always allow" is permitted for this exact call.

    Tool-level for the outbound/authoring set, but ARGUMENT-level for shell:
    `run_shell` as such is perfectly grantable — the vast majority of commands
    are ordinary — while an irreversible bulk delete never is, whatever tool
    carries it. Checking the name alone would have let one "always allow" on a
    harmless `ls` silently pre-approve every future `rm -rf`.
    """
    if tool in _NEVER_GRANTABLE:
        return False
    from service.safety.policy import is_destructive_shell
    return not is_destructive_shell(str((args or {}).get("cmd", "")))


def grant(tool: str, args: dict | None = None, *, decision: str = "allow",
          scoped: bool = True) -> dict:
    """Record a standing decision. `decision` is "allow" or "deny".

    `scoped=True` narrows it via scope_for; False makes it tool-wide.
    """
    if decision == "allow" and not is_grantable(tool, args):
        return {"ok": False, "error": f"{tool} always asks — it can't be pre-approved"}
    scope = scope_for(tool, args or {}) if scoped else ""
    with _lock:
        entry = _entry(tool)
        bucket = entry.setdefault(decision, [])
        if not any(r.get("scope", "") == scope for r in bucket):
            bucket.append({"scope": scope, "granted_at": time.time()})
        # A tool can't be both allowed and denied for the same scope — the new
        # answer replaces the old one rather than sitting alongside it.
        other = "deny" if decision == "allow" else "allow"
        entry[other] = [r for r in entry.get(other, []) if r.get("scope", "") != scope]
        _save()
    return {"ok": True, "tool": tool, "decision": decision, "scope": scope}


def revoke(tool: str, scope: str | None = None) -> bool:
    """Drop grants for a tool — all of them, or just one scope."""
    with _lock:
        if tool not in _CACHE:
            return False
        if scope is None:
            _CACHE.pop(tool)
        else:
            for key in ("allow", "deny"):
                _CACHE[tool][key] = [r for r in _CACHE[tool].get(key, [])
                                     if r.get("scope", "") != scope]
            if not _CACHE[tool].get("allow") and not _CACHE[tool].get("deny"):
                _CACHE.pop(tool)
        _save()
    return True


def all_grants() -> dict:
    return json.loads(json.dumps(_CACHE))


def never_grantable() -> list[str]:
    return sorted(_NEVER_GRANTABLE)
