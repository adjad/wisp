"""Checks for fixed workflow skills (inbox-cleanup, meeting-prep, week-review, file-tidy).

A workflow skill ships as instructions plus a `contract` under
`metadata.wisp` in its SKILL.md. The contract is the skill's declaration of
which of Wisp's own tools it may call, and which of those change anything:

    contract:
      read_only: true | false
      grants: none              # a skill never adds, widens or implies a grant
      folders: [...]            # only for skills that move files
      reads: [tool, ...]        # tools that only read
      preview: [tool, ...]      # tools with a no-change preview mode
      confirms: [tool, ...]     # tools that change something; each call is confirmed
      steps: [{tool, args, confirm?}, ...]   # the calls the skill makes, in order

A contract adds no authority. Every tool still runs through the policy engine
(service/safety/policy.py) and the standing-grant rules (service/safety/grants.py)
exactly as if the model had called it unprompted; `validate` only proves the
declaration matches what those rules do today, so a later change to a tool's
category or policy shows up as a failing check rather than a silent change in
what the skill may do.
"""
from __future__ import annotations

from pathlib import Path

from service.skills import Skill

_KNOWN_KEYS = {"read_only", "grants", "folders", "reads", "preview", "confirms", "steps"}
_STEP_CONFIRM = {"group", "plan"}

# A workflow skill may never call a tool in these categories: sending, deleting,
# running commands, editing the calendar, or anything an extension supplies.
_FORBIDDEN_CATEGORIES = frozenset({
    "email_send", "messages_send", "scheduled_send", "network_write", "fs_delete",
    "shell", "tool_authoring", "calendar_write", "skill_tool", "mcp_action",
    "messages_write", "email_draft", "messages_draft", "app_control",
})
# Folders a file-moving skill may name: the person's own, never home itself.
_USER_FOLDERS = ("Downloads", "Desktop", "Documents", "Pictures", "Movies", "Music")


def _names(contract: dict, key: str) -> list[str]:
    value = contract.get(key) or []
    return [str(v) for v in value] if isinstance(value, list) else []


def _inside_user_folders(path: str) -> bool:
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    home = Path.home().resolve()
    return any(resolved == home / f or (home / f) in resolved.parents for f in _USER_FOLDERS)


def validate(skill: Skill, registry: dict | None = None) -> list[str]:
    """Problems with a workflow skill's contract; an empty list means it holds.

    `registry` defaults to the tool registry, which the caller must have
    populated (importing service.main registers every built-in tool).
    """
    from service.safety import policy
    if registry is None:
        from service.tools.registry import REGISTRY as registry

    problems: list[str] = []
    contract = skill.contract
    if skill.error:
        return [f"skill does not load: {skill.error}"]
    if not contract:
        return ["no contract declared"]
    if unknown := sorted(set(contract) - _KNOWN_KEYS):
        problems.append(f"unknown contract keys: {unknown}")
    if str(contract.get("grants")) != "none":
        problems.append("a workflow skill must declare `grants: none`")

    reads, preview, confirms = (_names(contract, k) for k in ("reads", "preview", "confirms"))
    read_only = bool(contract.get("read_only"))
    if read_only and (confirms or preview):
        problems.append("a read-only skill declares tools that change something")
    declared = set(reads) | set(preview) | set(confirms)

    for name in sorted(declared):
        tool = registry.get(name)
        if tool is None:
            problems.append(f"{name}: not a registered tool")
            continue
        if tool.category in _FORBIDDEN_CATEGORIES:
            problems.append(f"{name}: category {tool.category} is not allowed in a workflow skill")
        if name in reads and not (tool.category.endswith("_read")
                                  or tool.category in {"fs_read", "assistant_read"}):
            problems.append(f"{name}: declared as a read but its category is {tool.category}")
        if name in confirms and name not in reads and tool.category.endswith("_read"):
            problems.append(f"{name}: declared as confirmed but it is a read")
        if name in confirms and name in reads:
            problems.append(f"{name}: declared both as a read and as confirmed")

    steps = contract.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("no steps declared")
        steps = []
    used: set[str] = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or "tool" not in step:
            problems.append(f"step {i}: not a tool step")
            continue
        name, args = str(step["tool"]), dict(step.get("args") or {})
        used.add(name)
        if name not in declared:
            problems.append(f"step {i}: {name} is not declared in reads/preview/confirms")
            continue
        tool = registry.get(name)
        if tool is None:
            continue
        asks = step.get("confirm")
        if asks is not None and asks not in _STEP_CONFIRM:
            problems.append(f"step {i}: confirm must be one of {sorted(_STEP_CONFIRM)}")
        if name in confirms and asks is None and not (name in preview and not args.get("confirm")):
            problems.append(f"step {i}: {name} changes something but the step has no confirm")
        # The real policy, in the default mode with no standing grant: a step
        # that asks must be a card, a step that does not must not change anything.
        decision = policy.decide(tool.category, args, tool=name)
        if asks and decision.tier is not policy.Tier.CONFIRM:
            problems.append(f"step {i}: {name} is {decision.tier.value} under policy, not a confirm")
        if not asks and decision.tier is not policy.Tier.ALLOW:
            problems.append(f"step {i}: {name} is {decision.tier.value} under policy, not a plain read")
        if name == "organize_files":
            folder, dest = str(args.get("folder", "")), str(args.get("destination", ""))
            if not _inside_user_folders(folder):
                problems.append(f"step {i}: folder {folder!r} is outside the user's folders")
            if dest != "." and not _inside_user_folders(dest):
                problems.append(f"step {i}: destination {dest!r} is outside the user's folders")
    if unused := sorted(declared - used):
        problems.append(f"declared but never called by a step: {unused}")
    for folder in _names(contract, "folders"):
        if not _inside_user_folders(folder):
            problems.append(f"folder {folder!r} is not one of the user's own folders")
    if confirms and not contract.get("folders") and "organize_files" in confirms:
        problems.append("a file-moving skill must declare `folders`")

    body = skill.body
    for name in sorted(declared):
        if name not in body:
            problems.append(f"{name} is declared but the instructions never mention it")
    return problems
