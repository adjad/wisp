"""Skills — new capabilities as installable folders, not code changes.

A skill is a directory under ~/.moe/skills/<name>/ containing a SKILL.md with
YAML frontmatter. Two things it can contribute:

1. **Instructions.** The body of SKILL.md is injected into the system prompt
   when the turn looks relevant (matched against `triggers`). This is how a
   skill teaches Wisp a procedure it doesn't otherwise know — how the user
   likes their standup notes formatted, what their deploy checklist is, which
   accounts matter for expense reports.

2. **Tools.** A skill can declare tools in frontmatter, each backed by a shell
   command template or a script inside the skill folder. They register into the
   normal tool registry and go through the normal policy engine — a skill is a
   packaging mechanism, not a way around the safety layer.

Everything is local files. Installing a skill is copying a folder; there's no
registry to phone home to and nothing about a skill leaves the machine.

Relevance-gating rather than always-injecting matters at this scale: the agent model
runs on a 24GB machine sharing memory with the model weights, and ten always-on
skill bodies would eat the context budget that context.py works to protect. So
the catalog (one line per skill) is always present and the full body arrives
only when triggered, or when the model deliberately calls `use_skill`.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from service.paths import MOE_DIR

SKILLS_DIR = MOE_DIR / "skills"
BUNDLED_SKILLS_DIR = Path(__file__).parent / "bundled"
# A bundled skill's on/off, by name. The signed bundle can't hold it, and a copy
# of the skill in SKILLS_DIR would freeze it at the version it was copied from:
# later releases' edits to its SKILL.md would never reach the user (#414 audit).
_BUNDLED_STATE = ".bundled-enabled.json"  # in SKILLS_DIR
# Held around every read-modify-write of _BUNDLED_STATE, so two toggles at
# once can't each save over the other's change.
_BUNDLED_STATE_LOCK = ".bundled-enabled.lock"
# Where load() moves an older build's toggle copy (dot-named: never loaded).
_RETIRED_DIR = ".retired"

# The body (sha256 of `_parse`'s body) of every bundled SKILL.md Wisp has
# shipped. Before #415, set_enabled copied a bundled skill into SKILLS_DIR to
# flip `enabled`, and that copy outranks the bundled skill for good. One whose
# body (and every other file) is a shipped version is such a copy, not the
# user's own edit, so load() retires it into _BUNDLED_STATE. A copy the user
# changed doesn't match and stays theirs. Add the new hash whenever a bundled
# body changes (tests/test_conversational_skills.py checks the current ones).
_SHIPPED_BODIES: dict[str, frozenset[str]] = {
    "humanizer": frozenset({"d6763c18aeb53e0ff94678b9261c63a84777febb668783807d545c67cfa704a1"}),
    "idea-refine": frozenset({"f70b9cde78d2abbeae12a7a3ace0993e4f2f39db77429535f198ec64374645cb"}),
    "interview-me": frozenset({"f4451e9cc23fb16348516936980577ce592dddd6aea99ed27a4e6da037e740b4"}),
    "mac-controls": frozenset({"6fe9b434bf7b76c7fec3940ecf93a2b4a43455f38e53d5a2aeeaef1b04b57240",
                               "7968c32f1d5aa6e80101ba8217ebe1a0d0b3c9ea3be7414b20960c148283f188"}),
}

# Frontmatter is fenced by --- on its own line, same convention as every other
# tool that reads SKILL.md-style files.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)

# Ceiling on one skill body injected into a turn. A skill that wants to teach a
# 20-page procedure has to be split; silently truncating is better than blowing
# the context budget, and the cap is generous enough that no reasonable skill
# hits it.
MAX_BODY_CHARS = 6000


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    triggers: list[str] = field(default_factory=list)
    enabled: bool = True
    tools: list[dict] = field(default_factory=list)
    error: str = ""
    # Bypasses `matches()` entirely — injected into every eligible turn
    # instead of only on a trigger hit. See `always_skills_block()`. Such a
    # skill is a reply style for plain chat only: it is never in the agent's
    # catalog, never trigger-loaded there, and use_skill does not hand it out.
    always: bool = False
    bundled: bool = False
    conversation_workflow: bool = False

    def matches(self, text: str) -> bool:
        """Whether this skill's triggers fire for `text`.

        Triggers are plain phrases, not regexes — a skill author writing
        "expense report" shouldn't have to think about escaping, and a
        malformed regex in a user-authored file shouldn't be able to break
        routing for every turn. They match on word boundaries: as bare
        substrings, "mute the" fired inside "commute the".
        """
        if not self.enabled:
            return False
        low = text.lower()
        explicit = (f"@{self.name.lower()}" in low
                    or f"${self.name.lower()}" in low)
        return explicit or any(trigger_matches(t, low) for t in self.triggers if t.strip())

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "triggers": self.triggers, "enabled": self.enabled,
                "tools": [t.get("name") for t in self.tools],
                "path": str(self.path), "error": self.error,
                "bundled": self.bundled,
                "conversation_workflow": self.conversation_workflow}


def trigger_matches(trigger: str, text: str) -> bool:
    """`trigger` occurs in `text` as whole words (case-insensitive)."""
    phrase = trigger.lower().strip()
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text.lower()) is not None


_skills: dict[str, Skill] = {}


def _parse(path: Path, *, bundled: bool = False) -> Skill | None:
    """Parse one SKILL.md. A broken skill is reported, never raised — one bad
    file in the folder must not take down skill loading for the rest."""
    try:
        raw = path.read_text(errors="replace")
    except Exception as e:  # noqa: BLE001
        return Skill(path.parent.name, "", "", path.parent,
                     error=f"unreadable: {e}", bundled=bundled)

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        # No frontmatter: still usable as a pure-instructions skill, named after
        # its folder. Lowering the barrier to "drop a markdown file in" is worth
        # more than strictness here.
        return Skill(path.parent.name, "", raw.strip()[:MAX_BODY_CHARS], path.parent,
                     bundled=bundled)

    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            raise ValueError("frontmatter must be a mapping")
    except Exception as e:  # noqa: BLE001
        return Skill(path.parent.name, "", "", path.parent,
                     error=f"bad frontmatter: {e}", bundled=bundled)

    # Wisp's runtime fields live under standard ``metadata`` in new skills so
    # the same package remains valid in other Agent Skills implementations.
    # Top-level keys stay supported for every skill users already installed.
    metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    wisp_meta = (metadata.get("wisp")
                 if isinstance(metadata.get("wisp"), dict) else {})

    triggers = meta.get("triggers") or wisp_meta.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [t.strip() for t in triggers.split(",")]

    tools = meta.get("tools") or wisp_meta.get("tools") or []
    if not isinstance(tools, list):
        tools = []

    return Skill(
        name=str(meta.get("name") or path.parent.name),
        description=str(meta.get("description") or ""),
        body=m.group(2).strip()[:MAX_BODY_CHARS],
        path=path.parent,
        triggers=[str(t) for t in triggers],
        enabled=bool(meta.get("enabled", wisp_meta.get("enabled", True))),
        tools=[t for t in tools if isinstance(t, dict)],
        always=bool(meta.get("always", wisp_meta.get("always", False))),
        bundled=bundled,
        conversation_workflow=bool(wisp_meta.get("conversation_workflow", False)),
    )


def load() -> dict[str, Skill]:
    """(Re)scan bundled skills, then user-installed overrides.

    Bundled workflows ship inside Wisp.app, while ``~/.moe/skills`` remains
    the user's editable layer. A user skill with the same frontmatter name
    intentionally wins, which lets someone customize a bundled workflow
    without modifying the signed app bundle.
    """
    global _skills
    found: dict[str, Skill] = {}
    try:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        _retire_toggle_copies()
        for root, bundled in ((BUNDLED_SKILLS_DIR, True), (SKILLS_DIR, False)):
            if not root.exists():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                md = child / "SKILL.md"
                if not md.exists():
                    continue
                skill = _parse(md, bundled=bundled)
                if skill:
                    found[skill.name] = skill
    except Exception:  # noqa: BLE001 — skills are optional; never break startup
        pass
    for name, enabled in _bundled_state().items():
        skill = found.get(name)
        if skill is not None and skill.bundled:
            skill.enabled = enabled
    _skills = found

    from service.skills.tools import register_skill_tools
    register_skill_tools(found)
    return found


def _bundled_state() -> dict[str, bool]:
    try:
        raw = json.loads((SKILLS_DIR / _BUNDLED_STATE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, bool)}


def _save_bundled_state(name: str, enabled: bool, *, unless_set: bool = False) -> bool:
    """Record a bundled skill's on/off: locked, then written to a fresh
    temporary file and renamed over the old one. `unless_set` keeps a choice
    already recorded (a retired copy must not undo a newer toggle)."""
    path = SKILLS_DIR / _BUNDLED_STATE
    try:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        with open(SKILLS_DIR / _BUNDLED_STATE_LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = _bundled_state()
            if unless_set and name in state:
                return True
            state[name] = enabled
            fd, tmp = tempfile.mkstemp(dir=SKILLS_DIR, prefix=_BUNDLED_STATE + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as out:
                    out.write(json.dumps(state, indent=2, sort_keys=True))
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
    except OSError:
        return False
    return True


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _same_files(copy: Path, bundled: Path) -> bool:
    """Every file but SKILL.md is the same in both folders."""
    def files(root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
                if p.is_file() and p.name != "SKILL.md"}
    try:
        return files(copy) == files(bundled)
    except OSError:
        return False


def _retire_toggle_copies() -> None:
    """Move aside each override an older build's set_enabled made, keeping the
    on/off it carried (see _SHIPPED_BODIES), so the bundled skill applies again."""
    if not BUNDLED_SKILLS_DIR.exists():
        return
    for bundled_dir in sorted(BUNDLED_SKILLS_DIR.iterdir()):
        copy = SKILLS_DIR / bundled_dir.name
        bundled = _parse(bundled_dir / "SKILL.md", bundled=True) if (bundled_dir / "SKILL.md").is_file() else None
        if bundled is None or bundled.error or not (copy / "SKILL.md").is_file():
            continue
        mine = _parse(copy / "SKILL.md")
        if (mine is None or mine.error or mine.name != bundled.name
                or _body_hash(mine.body) not in _SHIPPED_BODIES.get(bundled.name, frozenset())
                or not _same_files(copy, bundled_dir)):
            continue
        if not _save_bundled_state(bundled.name, mine.enabled, unless_set=True):
            continue
        retired = SKILLS_DIR / _RETIRED_DIR / f"{copy.name}-{int(time.time())}"
        try:
            retired.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(copy), str(retired))
        except OSError:
            continue


def all_skills() -> dict[str, Skill]:
    return dict(_skills)


def get(name: str) -> Skill | None:
    return _skills.get(name)


def set_enabled(name: str, enabled: bool) -> bool:
    """Toggle a skill by rewriting `enabled:` in its frontmatter, so the state
    survives a reload — the folder on disk stays the single source of truth
    rather than a separate index that can drift out of sync with it.

    A bundled skill is the exception: the signed app bundle is not a
    user-state store, and copying the skill out to flip one flag would pin it
    to this release's text. Its on/off goes in SKILLS_DIR/_BUNDLED_STATE instead."""
    skill = _skills.get(name)
    if not skill:
        return False
    if skill.bundled:
        if not _save_bundled_state(skill.name, enabled):
            return False
        load()
        return True
    md = skill.path / "SKILL.md"
    try:
        raw = md.read_text()
        m = _FRONTMATTER_RE.match(raw)
        if not m:
            return False
        meta = yaml.safe_load(m.group(1)) or {}
        metadata = meta.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            meta["metadata"] = metadata
        wisp_meta = metadata.get("wisp")
        if not isinstance(wisp_meta, dict):
            wisp_meta = {}
            metadata["wisp"] = wisp_meta
        # Preserve the legacy location if this is an older user-authored
        # skill; otherwise write the portable extension location.
        if "enabled" in meta:
            meta["enabled"] = enabled
        else:
            wisp_meta["enabled"] = enabled
        md.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n"
                      + m.group(2).lstrip())
    except Exception:  # noqa: BLE001
        return False
    load()
    return True


def install(source: str) -> dict:
    """Install a skill by copying a folder (or a single .md file) into
    ~/.moe/skills/. Local paths only — deliberately no download-and-run: a
    local-first assistant shouldn't grow a way to execute code fetched from a
    URL, and a skill can register shell-backed tools."""
    src = Path(source).expanduser()
    if not src.exists():
        return {"ok": False, "error": f"no such path: {src}"}
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if src.is_file():
            if src.suffix.lower() != ".md":
                return {"ok": False, "error": "a single-file skill must be a .md file"}
            dest = SKILLS_DIR / src.stem
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / "SKILL.md")
        else:
            if not (src / "SKILL.md").exists():
                return {"ok": False, "error": f"{src} has no SKILL.md"}
            dest = SKILLS_DIR / src.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    load()
    installed = next((s.name for s in _skills.values() if s.path == dest), dest.name)
    return {"ok": True, "name": installed, "path": str(dest)}


def uninstall(name: str) -> dict:
    skill = _skills.get(name)
    if not skill:
        return {"ok": False, "error": f"no skill named {name}"}
    if skill.bundled:
        return {"ok": False,
                "error": f"{name} ships with Wisp; disable it instead"}
    try:
        shutil.rmtree(skill.path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    load()
    reverted = (BUNDLED_SKILLS_DIR / name / "SKILL.md").exists()
    return {"ok": True, "reverted_to_bundled": reverted}


def skills_context_block(user_text: str = "", active_name: str = "",
                         catalog: bool = True) -> str:
    """The per-turn system-prompt contribution: a one-line catalog of every
    enabled skill, plus the FULL body of any whose triggers matched this turn.

    The catalog is what makes an untriggered skill discoverable — without it
    the model has no idea `use_skill` would return anything useful, and a skill
    with imperfect triggers would be permanently invisible. `catalog=False` is
    for a turn that does not offer `use_skill` at all: the model could not act
    on the list, so only matched (or active) skill bodies are returned.
    """
    # An always-on style skill belongs to plain chat (always_skills_block).
    enabled = [s for s in _skills.values() if s.enabled and not s.error and not s.always]
    if not enabled:
        return ""

    matched = [s for s in enabled if s.matches(user_text)]
    active = _skills.get(active_name) if active_name else None
    if active and active in enabled and active not in matched:
        matched.append(active)
    matched_names = {s.name for s in matched}

    out = []
    if catalog:
        out.append("\nInstalled skills — procedures the user has set up for specific "
                   "tasks. Call use_skill(name) to load one's full instructions when a "
                   "request looks like it belongs to that skill:")
        for s in enabled:
            line = f"- {s.name}: {s.description or '(no description)'}"
            if s.name in matched_names:
                line += "  [loaded below]"
            out.append(line)
    elif not matched:
        return ""

    for s in matched:
        out.append(f"\n--- Skill: {s.name} ---\nThe user set up this procedure "
                   f"for requests like this one. Follow it:\n{s.body}")
    return "\n".join(out)


_STOP_WORKFLOW_RE = re.compile(
    r"\b(?:stop|cancel|leave|exit|end)\s+(?:the\s+)?"
    r"(?:interview|interviewing|ideation|brainstorm|workflow|skill)\b",
    re.I,
)
_COMPLETE_RE = re.compile(
    r"<!--\s*wisp-skill-complete:\s*([a-z0-9-]+)\s*-->", re.I)


def select_for_turn(user_text: str, active_name: str = "",
                    last_assistant: str = "") -> str:
    """Choose the one conversational workflow active for this turn.

    Interactive skills are stateful: once the user starts an interview or an
    ideation session, their next answer rarely repeats the original trigger.
    The active name therefore persists in the session store. An explicit stop
    wins immediately, while the completion marker lets a skill release itself
    after delivering its final artifact.
    """
    if _STOP_WORKFLOW_RE.search(user_text):
        return ""
    complete = _COMPLETE_RE.search(last_assistant or "")
    if complete and complete.group(1).lower() == active_name.lower():
        active_name = ""

    enabled = [s for s in _skills.values()
               if s.enabled and not s.error and s.conversation_workflow]
    low = user_text.lower()
    for skill in enabled:
        if f"@{skill.name.lower()}" in low or f"${skill.name.lower()}" in low:
            return skill.name
    for skill in enabled:
        if skill.matches(user_text):
            return skill.name
    if (active_name and active_name in _skills and _skills[active_name].enabled
            and _skills[active_name].conversation_workflow):
        return active_name
    return ""


def turn_skill_names(user_text: str) -> list[str]:
    """Enabled, error-free skills this user turn invokes: an explicit @name or $name,
    or a trigger phrase. Ordinary and conversational skills alike.

    ``select_for_turn`` only ever returns conversational workflows, so it cannot tell
    the router that an ORDINARY skill is about to be loaded. This can.
    """
    return [s.name for s in _skills.values()
            if s.enabled and not s.error and not s.always and s.matches(user_text)]


# Tools that return skill content or metadata although they are not skill-defined tools.
# wisp_skills lists every installed skill with its enabled state and any load error.
_SKILL_CONTENT_TOOLS = frozenset({"use_skill", "wisp_skills"})


def skill_tool_names() -> frozenset[str]:
    """Tools whose results or schemas carry skill content: ``use_skill``, the skill-listing
    ``wisp_skills`` and every tool an installed skill defines."""
    from service.tools.registry import REGISTRY
    return frozenset(_SKILL_CONTENT_TOOLS | {name for name, tool in REGISTRY.items()
                                             if tool.category == "skill_tool"})


def digest_used_skill(tool_digest: str | None) -> bool:
    """Whether a stored assistant tool digest records a skill being loaded or run,
    so the follow-up to that turn is itself a skill turn."""
    if not tool_digest:
        return False
    used = {part.strip() for part in str(tool_digest).split(",") if part.strip()}
    return bool(used & skill_tool_names())


def selected_skill_block(user_text: str = "", active_name: str = "") -> str:
    """Prompt block for tool-free chat paths.

    Unlike ``skills_context_block`` this omits the full catalog: a plain chat
    turn cannot call ``use_skill``, so advertising every installed procedure
    would spend context without giving the model a way to load them. The
    already-selected workflow body is all that path needs.
    """
    active = _skills.get(active_name) if active_name else None
    selected: list[Skill] = []
    if (active and active.enabled and not active.error
            and active.conversation_workflow):
        selected = [active]
    else:
        selected = [skill for skill in _skills.values()
                    if skill.enabled and not skill.error
                    and skill.conversation_workflow
                    and skill.matches(user_text)][:1]
    return "".join(
        f"\n--- Active skill: {skill.name} ---\n"
        "The user chose this conversational workflow. Follow it until it "
        f"finishes or they stop it:\n{skill.body}\n"
        for skill in selected
    )


def always_skills_block() -> str:
    """Body text of every enabled, error-free skill with `always: true` in its
    frontmatter — unconditional, unlike `skills_context_block()`'s
    trigger-matched injection. Deliberately a separate function rather than a
    flag on `skills_context_block()`: the two call sites want different
    things (trigger-matched-only on the tool-calling agent path, always-only
    on the plain-chat path), and a boolean that changes behavior at a
    distance is easy to wire into the wrong place by accident.
    """
    always_on = [s for s in _skills.values()
                 if s.enabled and not s.error and s.always]
    if not always_on:
        return ""
    out = []
    for s in always_on:
        out.append(f"\n--- {s.name} ---\n{s.body}")
    return "\n".join(out)
