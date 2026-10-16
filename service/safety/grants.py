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
import math
import threading
import time
from pathlib import Path

from service.paths import MOE_DIR
from service.publication_authority import Authority, Witness, assert_mutation_allowed, revocation

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

# Tools that absorbed several single-purpose tools in the 100-tool
# consolidation: (argument that selects the behaviour, its default, the
# behaviours one tool-wide grant covered before the merge). A grant is scoped
# to the behaviour, so "always allow Do Not Disturb" cannot silently become
# "always turn Wi-Fi off". A tool-wide rule stored before the merge keeps
# covering only what the tool did then; grant(scoped=False) now records
# "all_behaviours" when the user deliberately widens it.
_MERGED_SCOPES = {
    "toggle_setting": ("setting", "", frozenset({
        "bluetooth", "do_not_disturb", "low_power_mode", "firewall", "airdrop"})),
    "email_triage": ("action", "", frozenset()),
    "set_timer": ("kind", "timer", frozenset({"timer"})),
    "manage_timers": ("action", "list", frozenset({"list", "cancel", "cancel_all"})),
}

# Retired tool -> (the merged tool, the behaviours it became). A DENY the user
# stored on the old name still applies to those behaviours. A stored ALLOW is
# deliberately not carried over: Wisp asks again rather than guessing.
_RETIRED_INTO = {
    "set_wifi": ("toggle_setting", frozenset({"wifi"})),
    "accessibility_toggle": ("toggle_setting", frozenset({"voiceover"})),
    "set_appearance": ("toggle_setting", frozenset({"dark_mode", "auto_appearance"})),
    "archive_email": ("email_triage", frozenset({"archive"})),
    "mark_email_read": ("email_triage", frozenset({"mark_read", "mark_unread"})),
    "flag_email": ("email_triage", frozenset({"flag", "unflag"})),
    "set_alarm": ("set_timer", frozenset({"alarm"})),
    "set_sleep_timer": ("set_timer", frozenset({"sleep"})),
    "stopwatch": ("manage_timers", frozenset({
        "stopwatch_start", "stopwatch_lap", "stopwatch_read", "stopwatch_stop"})),
}

_lock = threading.Lock()


def _known_cache(data) -> bool:
    if type(data) is not dict:
        return False
    for tool, entry in data.items():
        if type(tool) is not str or not tool or type(entry) is not dict:
            return False
        if set(entry) - {"allow", "deny"}:
            return False
        for rules in entry.values():
            if type(rules) is not list:
                return False
            for rule in rules:
                if (type(rule) is not dict or set(rule) - {"scope", "granted_at", "all_behaviours"}
                        or type(rule.get("scope", "")) is not str
                        or "all_behaviours" in rule and type(rule["all_behaviours"]) is not bool):
                    return False
                stamp = rule.get("granted_at")
                if "granted_at" in rule:
                    if type(stamp) not in (int, float):
                        return False
                    try:
                        if not math.isfinite(stamp):
                            return False
                    except OverflowError:
                        return False
    return True


def _load_snapshot() -> tuple[dict, bool]:
    try:
        if GRANTS_PATH.exists():
            duplicate = False
            def pairs(items):
                nonlocal duplicate
                result = {}
                for key, value in items:
                    duplicate = duplicate or key in result
                    result[key] = value
                return result
            data = json.loads(GRANTS_PATH.read_text(), object_pairs_hook=pairs)
            if isinstance(data, dict):
                return data, not duplicate and _known_cache(data)
            return {}, False
        return {}, not GRANTS_PATH.is_symlink()
    except Exception:  # noqa: BLE001 — a corrupt file must not break every turn
        pass
    return {}, False


def _load() -> dict:
    """Read a snapshot only; no live cache replacement or epoch rearming."""
    return _load_snapshot()[0]


_CACHE, _proof_known = _load_snapshot()
_publication = Authority("action-grants")
if _proof_known:
    _publication.publish()


def publication_witness() -> Witness | None:
    """Current process-owned cache proof, not proof of external file edits.

    Call before full grant/policy preparation and retain this original witness.
    """
    token = _publication.token() if _proof_known else None
    return Witness((token,)) if token is not None else None


def _save() -> None:
    # Private helper called under outer revocation THEN _lock by grant/revoke.
    # Keep historical swallowed save errors, but never advertise failed proof.
    global _proof_known
    # Public writers already hold the outer lease before _lock; this nested
    # lease also fences a direct private save without taking an inverted lock.
    with revocation():
        _publication.withdraw()
        _proof_known = False
        try:
            GRANTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(_CACHE, indent=2)
            if GRANTS_PATH.write_text(payload) == len(payload):
                _proof_known = _known_cache(_CACHE)
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
        if tool in _MERGED_SCOPES:
            arg, default, _ = _MERGED_SCOPES[tool]
            value = str(args.get(arg) or default)
            if tool == "toggle_setting":
                from service.tools.system_extras import setting_key
                return setting_key(value)
            return value.strip().lower()
        if tool in ("web_fetch", "http_request"):
            from urllib.parse import urlparse
            return (urlparse(str(args.get("url", ""))).hostname or "").lower()
        if tool in ("write_file", "read_file"):
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


def _may_hold_allow(tool: str) -> bool:
    """Whether a standing "always allow" may exist for this tool at all.

    Decided by the tool's SAFETY CATEGORY as well as the name list, so every
    tool the policy always confirms is refused here too, including ones added
    after this file was written. A tool whose category cannot be determined is
    judged on the name list alone; the policy check in decide() still applies.
    """
    if tool in _NEVER_GRANTABLE:
        return False
    try:
        from service.safety.policy import ALWAYS_CONFIRM_CATEGORIES
        from service.tools.registry import REGISTRY
        registered = REGISTRY.get(tool)
        return registered is None or registered.category not in ALWAYS_CONFIRM_CATEGORIES
    except Exception:  # noqa: BLE001 — never let a lookup failure widen what is grantable
        return False


def _entry(tool: str) -> dict:
    return _CACHE.setdefault(tool, {"allow": [], "deny": []})


def _retired_deny(tool: str, scope: str) -> bool:
    """Whether a deny stored under a retired name covers this merged call."""
    for old, (merged, behaviours) in _RETIRED_INTO.items():
        if merged == tool and scope in behaviours and _CACHE.get(old, {}).get("deny"):
            return True
    return False


def _split_retired_denies(tool: str, scope: str) -> None:
    """Move retired-name denies onto the merged tool, minus what is now allowed.

    Called with _lock held when the user allows `scope`. The other behaviours
    the old name covered stay denied explicitly. A tool-wide allow ("") lifts
    none of them, the same as an explicit per-behaviour deny on the merged tool.
    """
    for old, (merged, behaviours) in _RETIRED_INTO.items():
        if merged != tool or not _CACHE.get(old, {}).get("deny"):
            continue
        if scope and scope not in behaviours:
            continue
        _CACHE.pop(old)
        deny = _entry(tool).setdefault("deny", [])
        for kept in sorted(behaviours - {scope}):
            if not any(r.get("scope", "") == kept for r in deny):
                deny.append({"scope": kept, "granted_at": time.time()})


def _allow_covers(tool: str, rule: dict, scope: str) -> bool:
    stored = rule.get("scope", "")
    if stored == scope:
        return True
    if stored != "":
        return False
    if tool not in _MERGED_SCOPES or rule.get("all_behaviours"):
        return True
    return scope in _MERGED_SCOPES[tool][2]


def check(tool: str, args: dict) -> str | None:
    """"allow" / "deny" if a standing grant covers this call, else None.

    A stored scope of "" is tool-wide. A tool-wide deny matches any call; a
    tool-wide allow on a merged tool matches only what it covered when stored.
    """
    entry = _CACHE.get(tool) or {}
    scope = scope_for(tool, args)
    if tool in _MERGED_SCOPES and _retired_deny(tool, scope):
        return "deny"
    if not entry:
        return None
    for rule in entry.get("deny", []):
        if rule.get("scope", "") in ("", scope):
            return "deny"
    if not _may_hold_allow(tool):
        return None
    for rule in entry.get("allow", []):
        if _allow_covers(tool, rule, scope):
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
    if not _may_hold_allow(tool):
        return False
    from service.safety.policy import is_destructive_shell
    return not is_destructive_shell(str((args or {}).get("cmd", "")))


def grant(tool: str, args: dict | None = None, *, decision: str = "allow",
          scoped: bool = True) -> dict:
    """Record a standing decision. `decision` is "allow" or "deny".

    `scoped=True` narrows it via scope_for; False makes it tool-wide.
    """
    assert_mutation_allowed()
    if decision == "allow" and not is_grantable(tool, args):
        return {"ok": False, "error": f"{tool} always asks — it can't be pre-approved"}
    scope = scope_for(tool, args or {}) if scoped else ""
    if scoped and scope == "" and tool in _MERGED_SCOPES:
        # No behaviour named (a malformed call): a scoped answer has nothing
        # to attach to, and storing "" would silently widen it to every one.
        return {"ok": False, "error": f"{tool} needs a {_MERGED_SCOPES[tool][0]} to remember this"}
    with revocation(), _lock:
        _publication.withdraw()
        if decision == "allow" and tool in _MERGED_SCOPES:
            _split_retired_denies(tool, scope)
        entry = _entry(tool)
        bucket = entry.setdefault(decision, [])
        existing = next((r for r in bucket if r.get("scope", "") == scope), None)
        if existing is None:
            existing = {"scope": scope, "granted_at": time.time()}
            bucket.append(existing)
        if tool in _MERGED_SCOPES and not scoped:
            existing["all_behaviours"] = True
        # A tool can't be both allowed and denied for the same scope — the new
        # answer replaces the old one rather than sitting alongside it.
        other = "deny" if decision == "allow" else "allow"
        entry[other] = [r for r in entry.get(other, []) if r.get("scope", "") != scope]
        _save()
        if _proof_known:
            _publication.publish()
    return {"ok": True, "tool": tool, "decision": decision, "scope": scope}


def revoke(tool: str, scope: str | None = None) -> bool:
    """Drop grants for a tool — all of them, or just one scope."""
    with revocation(), _lock:
        if tool not in _CACHE:
            return False
        _publication.withdraw()
        if scope is None:
            _CACHE.pop(tool)
        else:
            for key in ("allow", "deny"):
                _CACHE[tool][key] = [r for r in _CACHE[tool].get(key, [])
                                     if r.get("scope", "") != scope]
            if not _CACHE[tool].get("allow") and not _CACHE[tool].get("deny"):
                _CACHE.pop(tool)
        _save()
        if _proof_known:
            _publication.publish()
    return True


def all_grants() -> dict:
    return json.loads(json.dumps(_CACHE))


def never_grantable() -> list[str]:
    return sorted(_NEVER_GRANTABLE)
