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

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from service.paths import MOE_DIR

SKILLS_DIR = MOE_DIR / "skills"

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
    # instead of only on a trigger hit. See `always_skills_block()`.
    always: bool = False

    def matches(self, text: str) -> bool:
        """Whether this skill's triggers fire for `text`.

        Triggers are plain substrings/phrases, not regexes — a skill author
        writing "expense report" shouldn't have to think about escaping, and a
        malformed regex in a user-authored file shouldn't be able to break
        routing for every turn.
        """
        if not self.enabled or not self.triggers:
            return False
        low = text.lower()
        return any(t.lower().strip() in low for t in self.triggers if t.strip())

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "triggers": self.triggers, "enabled": self.enabled,
                "tools": [t.get("name") for t in self.tools],
                "path": str(self.path), "error": self.error}


_skills: dict[str, Skill] = {}


def _parse(path: Path) -> Skill | None:
    """Parse one SKILL.md. A broken skill is reported, never raised — one bad
    file in the folder must not take down skill loading for the rest."""
    try:
        raw = path.read_text(errors="replace")
    except Exception as e:  # noqa: BLE001
        return Skill(path.parent.name, "", "", path.parent, error=f"unreadable: {e}")

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        # No frontmatter: still usable as a pure-instructions skill, named after
        # its folder. Lowering the barrier to "drop a markdown file in" is worth
        # more than strictness here.
        return Skill(path.parent.name, "", raw.strip()[:MAX_BODY_CHARS], path.parent)

    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            raise ValueError("frontmatter must be a mapping")
    except Exception as e:  # noqa: BLE001
        return Skill(path.parent.name, "", "", path.parent,
                     error=f"bad frontmatter: {e}")

    triggers = meta.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [t.strip() for t in triggers.split(",")]

    tools = meta.get("tools") or []
    if not isinstance(tools, list):
        tools = []

    return Skill(
        name=str(meta.get("name") or path.parent.name),
        description=str(meta.get("description") or ""),
        body=m.group(2).strip()[:MAX_BODY_CHARS],
        path=path.parent,
        triggers=[str(t) for t in triggers],
        enabled=bool(meta.get("enabled", True)),
        tools=[t for t in tools if isinstance(t, dict)],
        always=bool(meta.get("always", False)),
    )


def load() -> dict[str, Skill]:
    """(Re)scan the skills directory. Safe to call any time."""
    global _skills
    found: dict[str, Skill] = {}
    try:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        for child in sorted(SKILLS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            md = child / "SKILL.md"
            if not md.exists():
                continue
            skill = _parse(md)
            if skill:
                found[skill.name] = skill
    except Exception:  # noqa: BLE001 — skills are optional; never break startup
        pass
    _skills = found

    from service.skills.tools import register_skill_tools
    register_skill_tools(found)
    return found


def all_skills() -> dict[str, Skill]:
    return dict(_skills)


def get(name: str) -> Skill | None:
    return _skills.get(name)


def set_enabled(name: str, enabled: bool) -> bool:
    """Toggle a skill by rewriting `enabled:` in its frontmatter, so the state
    survives a reload — the folder on disk stays the single source of truth
    rather than a separate index that can drift out of sync with it."""
    skill = _skills.get(name)
    if not skill:
        return False
    md = skill.path / "SKILL.md"
    try:
        raw = md.read_text()
        m = _FRONTMATTER_RE.match(raw)
        if not m:
            return False
        meta = yaml.safe_load(m.group(1)) or {}
        meta["enabled"] = enabled
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
    try:
        shutil.rmtree(skill.path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    load()
    return {"ok": True}


def skills_context_block(user_text: str = "") -> str:
    """The per-turn system-prompt contribution: a one-line catalog of every
    enabled skill, plus the FULL body of any whose triggers matched this turn.

    The catalog is what makes an untriggered skill discoverable — without it
    the model has no idea `use_skill` would return anything useful, and a skill
    with imperfect triggers would be permanently invisible.
    """
    enabled = [s for s in _skills.values() if s.enabled and not s.error]
    if not enabled:
        return ""

    matched = [s for s in enabled if s.matches(user_text)]
    matched_names = {s.name for s in matched}

    out = ["\nInstalled skills — procedures the user has set up for specific "
           "tasks. Call use_skill(name) to load one's full instructions when a "
           "request looks like it belongs to that skill:"]
    for s in enabled:
        line = f"- {s.name}: {s.description or '(no description)'}"
        if s.name in matched_names:
            line += "  [loaded below]"
        out.append(line)

    for s in matched:
        out.append(f"\n--- Skill: {s.name} ---\nThe user set up this procedure "
                   f"for requests like this one. Follow it:\n{s.body}")
    return "\n".join(out)


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
