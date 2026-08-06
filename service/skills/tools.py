"""Tools contributed by installed skills, plus the `use_skill` loader.

Two hard rules shape this file:

**A skill-declared tool always runs through the policy engine.** Its category
is forced to `skill_tool` (confirm-tier, denied in view-only) regardless of
what the skill's frontmatter says. Letting a skill self-declare `fs_read` would
let any dropped-in folder mark a shell command as auto-allowed — a skill is a
packaging format, not a trust boundary. The narrow escape hatch is a standing
grant (service/safety/grants.py), which is the user's decision, not the
skill's.

**Arguments are never interpolated into a shell string.** The command template
is shlex-split FIRST, then placeholders are substituted per-token, and the
result is exec'd with shell=False. So an argument containing `; rm -rf ~` stays
one argv element that the program receives as a literal string, instead of
becoming a second command. Building the string first and splitting after would
be a textbook injection hole, and the model — which is what fills these
arguments in — is exactly the untrusted input path that makes it exploitable.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import tempfile
from pathlib import Path

from service.skills import sandbox, scopes
from service.tools.registry import REGISTRY, register

# Tool names this module has registered, so a reload can retract tools whose
# skill was uninstalled or disabled instead of leaving them callable forever.
_registered: set[str] = set()

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_TIMEOUT_S = 120
_MAX_OUTPUT = 8000
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")


def _build_argv(template: str, args: dict) -> list[str] | str:
    """Split the template, then substitute. Returns argv, or an error string.

    A token that is exactly one placeholder becomes exactly one argv element
    holding the raw value. A token with a placeholder embedded in other text
    (`--out={path}`) is substituted inside that single token. Either way the
    value can never introduce a new word, an operator, or a second command.
    """
    try:
        tokens = shlex.split(template)
    except ValueError as e:
        return f"(bad command template in this skill: {e})"

    argv: list[str] = []
    for token in tokens:
        m = _PLACEHOLDER_RE.fullmatch(token)
        if m:
            value = args.get(m.group(1))
            if value is None or value == "":
                continue          # optional argument the caller omitted
            argv.append(str(value))
            continue
        argv.append(_PLACEHOLDER_RE.sub(
            lambda mm: str(args.get(mm.group(1), "")), token))
    return argv


def _make_runner(skill_dir: Path, spec: dict):
    template = str(spec.get("command") or "")
    script = str(spec.get("script") or "")
    read_scopes, _ = scopes.parse_scopes(spec.get("read_scopes"))
    write_scopes, _ = scopes.parse_scopes(spec.get("write_scopes"))

    async def run(**args) -> str:
        if script:
            path = (skill_dir / script).resolve()
            # Containment check: a skill's own folder is the only place it may
            # execute from, so `script: ../../../usr/bin/something` can't turn a
            # skill install into arbitrary-binary execution.
            try:
                path.relative_to(skill_dir.resolve())
            except ValueError:
                return f"(this skill's script path escapes its own folder: {script})"
            if not path.exists():
                return f"(this skill's script is missing: {script})"
            interpreter = {".py": "python3", ".sh": "bash", ".js": "node"}.get(
                path.suffix.lower())
            argv = ([interpreter, str(path)] if interpreter else [str(path)])
            for key, value in args.items():
                if value not in (None, ""):
                    argv += [f"--{key}", str(value)]
        else:
            built = _build_argv(template, args)
            if isinstance(built, str):
                return built
            argv = built
        if not argv:
            return "(this skill's tool has no command to run)"

        # Kernel-enforced containment. The script-path check above only proves
        # the FILE sits in the skill's folder; it says nothing about where the
        # running process reaches. Without this, a generated tool inherits the
        # user's full filesystem rights the moment it starts.
        profile_file = None
        if sandbox.available():
            profile = sandbox.build_profile(skill_dir, read_scopes, write_scopes)
            fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="wisp-skill-")
            with os.fdopen(fd, "w") as f:
                f.write(profile)
            profile_file = Path(profile_path)
            argv = sandbox.wrap_argv(argv, profile_file)

        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(skill_dir),
                    env={**os.environ, "WISP_SKILL_DIR": str(skill_dir)},
                )
            except FileNotFoundError:
                return f"(couldn't run this skill's tool — {argv[0]} isn't installed)"
            except Exception as e:  # noqa: BLE001
                return f"(couldn't run this skill's tool: {e})"

            try:
                out, _ = await asyncio.wait_for(proc.communicate(), _TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                return f"(this skill's tool timed out after {_TIMEOUT_S}s)"
        finally:
            # The profile must outlive the process, so it's removed only once
            # the run is fully done — including the early-return paths above.
            if profile_file:
                profile_file.unlink(missing_ok=True)

        text = (out or b"").decode(errors="replace").strip()
        if len(text) > _MAX_OUTPUT:
            text = text[:_MAX_OUTPUT] + "\n…[truncated]"
        if proc.returncode != 0:
            # A sandbox denial surfaces as a bare EPERM, which reads like a bug
            # in the tool. Name the real cause so the model reports "it isn't
            # allowed there" instead of retrying the same call.
            if "Operation not permitted" in text or "Errno 1" in text:
                allowed = scopes.describe(read_scopes, write_scopes)
                return (f"(blocked by the sandbox — this tool tried to reach a "
                        f"file outside what it's allowed. {allowed})\n{text}")
            return f"(exit code {proc.returncode})\n{text}"
        return text or "(done — no output)"

    return run


def register_skill_tools(skills: dict) -> list[str]:
    """Register every enabled skill's declared tools; retract stale ones."""
    for name in _registered:
        REGISTRY.pop(name, None)
    _registered.clear()

    added: list[str] = []
    for skill in skills.values():
        if not skill.enabled or skill.error:
            continue
        for spec in skill.tools:
            name = str(spec.get("name") or "").strip().lower()
            if not _NAME_RE.match(name):
                continue
            # Never let a skill shadow a built-in. Silently replacing
            # `send_email` or `run_shell` with a skill's own version would be a
            # capability swap the user never sees.
            if name in REGISTRY and name not in _registered:
                continue
            if not (spec.get("command") or spec.get("script")):
                continue

            props = spec.get("parameters") or {}
            if not isinstance(props, dict):
                props = {}
            schema = {
                "type": "object",
                "properties": {k: (v if isinstance(v, dict) else {"type": "string"})
                               for k, v in props.items()},
                "required": [k for k, v in props.items()
                             if isinstance(v, dict) and v.get("required")],
            }
            description = (str(spec.get("description") or name)
                           + f" (from the '{skill.name}' skill)")
            register(name, description, schema, category="skill_tool")(
                _make_runner(skill.path, spec))
            _registered.add(name)
            added.append(name)
    return added


@register(
    "use_skill",
    "Load the full instructions for one of the user's installed skills. The "
    "skills available are listed in your system prompt with a one-line "
    "description each; call this when a request matches one and its "
    "instructions weren't already loaded for you. Returns the skill's "
    "procedure, which you should then follow.",
    {"type": "object",
     "properties": {
         "name": {"type": "string", "description": "the skill's name"},
     },
     "required": ["name"]},
    category="assistant_read",
)
async def use_skill(name: str) -> str:
    from service.skills import all_skills, get
    skill = get((name or "").strip())
    if skill is None:
        # Case-insensitive second pass — the model reads the name off the
        # catalog, so a capitalization mismatch shouldn't be a dead end.
        wanted = (name or "").strip().lower()
        skill = next((s for s in all_skills().values() if s.name.lower() == wanted), None)
    if skill is None:
        available = ", ".join(sorted(all_skills())) or "(none installed)"
        return f"(no skill named {name!r}. Installed skills: {available})"
    if skill.error:
        return f"(the '{skill.name}' skill is broken: {skill.error})"
    if not skill.enabled:
        return f"(the '{skill.name}' skill is turned off — the user can enable it in Settings)"
    return f"Instructions from the '{skill.name}' skill:\n\n{skill.body}"
