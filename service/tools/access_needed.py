"""Which sources a tool couldn't read because macOS hasn't allowed them yet.

Ask shows the same fix button under its reply that the Brief and Day show
("Allow in Wisp…", "Open Calendar settings ↗"). The button must not depend on
the model repeating a phrase, so a tool that stops for lack of access calls
``need(source, kind)`` and the agent loop attaches what was noted to that
call's ``tool_result`` event as ``needs_access``. The kinds are the Brief's own
(today.source_access's ``blocked``, the readers' ``reason_kind``), so the app
maps them with the code that already drives those buttons.

Every path that answers Ask collects this way: ``run_agent`` for the whole
turn (``start``/``drain``), and the deterministic read, workflow and task
executors per call (``noting``/``payload``). Inert anywhere else: the scheduler
and the Brief read the same sources and have no reply to attach a button to.
"""
from __future__ import annotations

import contextlib
import contextvars

# What the app has a fix for (SourceAccess.fix(for:) and Permissions.askFix).
FIXABLE = frozenset({"calendar_access", "reminders_access", "contacts_access", "full_disk_access",
                     "mail_automation", "mail_closed", "notes_closed", "mail_not_configured",
                     "no_accounts"})

_PANE = "System Settings > Privacy & Security > "
_IN_WISP = "Wisp's Settings > Privacy > Permissions…"

# The exact place that fixes each kind, in a sentence the model can pass on.
_HOW = {
    "calendar_access": f"Click Allow next to Calendar in {_IN_WISP}, or turn Wisp on in {_PANE}Calendars.",
    "reminders_access": f"Click Allow next to Reminders in {_IN_WISP}, or turn Wisp on in {_PANE}Reminders.",
    "contacts_access": f"Click Allow next to Contacts in {_IN_WISP}, or turn Wisp on in {_PANE}Contacts.",
    "full_disk_access": f"Turn Wisp on in {_PANE}Full Disk Access.",
    "mail_closed": f"Open Mail, or turn Wisp on in {_PANE}Full Disk Access.",
    "notes_closed": f"Open Notes, or turn Wisp on in {_PANE}Full Disk Access.",
    "mail_automation": f"Click Allow next to Mail & Notes in {_IN_WISP}, or turn on Mail for Wisp in {_PANE}Automation.",
    "mail_not_configured": "Add an account in System Settings > Internet Accounts.",
    "no_accounts": "Add an account in System Settings > Internet Accounts.",
}

_noted: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar("_access_needed", default=None)


def start() -> list[dict]:
    """Begin collecting for one agent turn. Tools run in tasks copied from this
    context share the same list, so a note from any of them is seen."""
    collected: list[dict] = []
    _noted.set(collected)
    return collected


def need(source: str, kind: str) -> str:
    """Note that ``source`` couldn't be read for lack of access and return the
    sentence naming where to fix it. A kind the app has no fix for (a read that
    failed for another reason, or no kind at all) notes nothing and returns "",
    so a passing failure never offers a permission button."""
    if kind not in FIXABLE:
        return ""
    collected = _noted.get()
    if collected is not None and not any(n["source"] == source and n["kind"] == kind for n in collected):
        collected.append({"source": source, "kind": kind})
    return _HOW[kind]


def drain(collected: list[dict]) -> list[dict]:
    """What was noted since the last drain, for the next tool_result event."""
    out = list(collected)
    collected.clear()
    return out


@contextlib.contextmanager
def noting():
    """Collect for exactly one tool call, so its note can't ride on another
    call's result. The outer collector (if any) is restored afterwards."""
    collected: list[dict] = []
    token = _noted.set(collected)
    try:
        yield collected
    finally:
        _noted.reset(token)


def payload(collected: list[dict]) -> dict:
    """The ``needs_access`` field for a tool_result event, or nothing."""
    return {"needs_access": list(collected)} if collected else {}


def merged(event: dict, collected: list[dict]) -> dict:
    """``event`` with ``collected`` added to any ``needs_access`` it carries."""
    if not collected:
        return event
    have = list(event.get("needs_access") or [])
    have += [n for n in drain(collected) if n not in have]
    return {**event, "needs_access": have}
