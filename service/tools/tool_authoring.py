"""Self-authored tools: the agent model hits a capability gap, writes a tool for it.

Before this, a request with no matching tool either got refused outright or —
worse, observed live — got a confident, false answer from a model that had
nothing to actually call ("generate a secure password" produced invented text
with the reasoning "Use tool? No tool needed. Just output."). This closes that
gap the same way the skills system closes it for user-authored capabilities: a
new tool becomes a skill folder under ~/.moe/skills/, reusing that system's
execution machinery (shlex-contained argv, path containment, timeout,
confirm-tier policy) rather than inventing a second, less-reviewed way to run
generated code.

Deliberately LOCAL-ONLY: code is written by the on-device model, never a cloud
API. Nothing about the task, the generated code, or the user's request leaves
the machine — a considered choice (confirmed with the user 2026-07-30), not an
oversight.

**Exactly one tool call, on purpose.** The obvious design is two tools —
draft, show the code, then install — and it was built and tested that way
first. It does not work here, for a reason worth recording: under gpt-oss,
oMLX's harmony adapter could not parse a response that opens directly with a
tool call and no `analysis` block. A same-turn follow-up call is precisely
when the model has nothing left to reason about, so it emitted a bare call,
the adapter rejected it ("unexpected tokens remaining in message header"),
and the SDK saw an entirely empty response. Verified repeatedly in oMLX's own
log, at two temperatures, with and without the code in context — the turn
silently died mid-flow every time, which looked exactly like "the model
ignores its tools." gpt-oss is no longer rostered; the agent role is
Agents-A1 (qwen3 lineage, qwen3_coder tool-call format, no harmony adapter
involved), and this specific failure has not been reverified against it. Keep
the single-call design regardless — it removes the two-tool race
(draft-then-install) on its own merits, independent of which parser bug
originally forced the question.

So `create_tool` is a single call carrying the full spec. The code is generated
by `prepare_draft`, which the agent loop runs *before* raising the confirmation
card (the policy engine decides confirm/deny before any tool executes, so the
code has to exist by then). The card shows that exact code, read server-side
from the cached draft — so what the user reviews and what gets written are the
same bytes, and the model never has a chance to show one thing and install
another.
"""
from __future__ import annotations

import re
import time

import yaml

from service.config import role_to_model
from service.inference.omlx_client import OMLXClient
from service.skills import SKILLS_DIR, load as reload_skills
from service.skills.sandbox import available as sandbox_available
from service.skills.scopes import describe as describe_scopes, parse_scopes
from service.tools.registry import REGISTRY, register

_client: OMLXClient | None = None


def _c() -> OMLXClient:
    global _client
    if _client is None:
        _client = OMLXClient()
    return _client


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.S)

# Drafts are cached only between the loop's prepare_draft call and the tool
# actually running a moment later, so this TTL is generous by a wide margin.
_DRAFT_TTL_S = 30 * 60
_pending: dict[str, dict] = {}

_CODER_SYS = (
    "You write small, standalone Python 3 scripts to be run from the command "
    "line. Rules, no exceptions:\n"
    "- Standard library ONLY. There is no install step, so any import that "
    "isn't in the Python stdlib will fail every time this runs.\n"
    "- Parse arguments with argparse, using EXACTLY the flags given — one "
    "--<name> per parameter, matching the required/optional list precisely.\n"
    "- Print the result to stdout as plain human-readable text (not JSON, not "
    "a Python repr).\n"
    "- On failure, print a short reason to stderr and exit with a non-zero "
    "status. Never raise an unhandled traceback as the visible failure.\n"
    "- No interactive input() — this always runs non-interactively.\n"
    "- NEVER invent data. Do not write sample, placeholder, mock, example, or "
    "hard-coded stand-in values ('John Doe', '555-1234', 'user@example.com', a "
    "literal list of fake results) and do not fall back to them when real data "
    "isn't reachable. The output of this script is shown to the user as fact, "
    "so invented data is read as real and is far worse than no tool at all.\n"
    "- If the task CANNOT be done under these rules — it needs a third-party "
    "package, a network service you have no credentials for, or private data "
    "the script has no legitimate way to read — do NOT write a script that "
    "fakes it. Instead reply with exactly one line:\n"
    "  CANNOT_IMPLEMENT: <short reason>\n"
    "- Return ONLY the code. No markdown fences, no explanation before or "
    "after."
)


def _strip_fences(code: str) -> str:
    m = _FENCE_RE.match(code.strip())
    return m.group(1) if m else code.strip()


# Personal data Wisp ALREADY reaches through a real, permissioned path — the
# Swift app holds the TCC grants and pushes the data in; a bare Python script
# spawned by the skill runner holds nothing and cannot read any of it.
#
# Without this gate the model cheerfully "wrote a tool" for these, and because
# it couldn't actually read anything it invented the output. Observed live: a
# requested `list_contacts` printed a hard-coded 'John Doe / 555-1234 /
# john@example.com', which Wisp then presented to the user as their real
# address book. Redirecting to the tool that genuinely has the data fixes the
# capability gap AND the fabrication in one move.
_NATIVE_CAPABILITY: list[tuple[str, str]] = [
    (r"\bcontacts?\b|\baddress book\b|\bphone ?numbers?\b",
     "list_contacts (list/browse everyone) or lookup_contact (one person's number/email)"),
    (r"\bimessages?\b|\btexts?\b|\bsms\b|\bchat\.db\b",
     "view_messages / summarize_messages"),
    (r"\bemails?\b|\binbox\b|\bmail\.app\b|\bmailbox\b",
     "view_emails / summarize_emails"),
    (r"\bcalendars?\b|\bupcoming events?\b|\bmy schedule\b",
     "get_upcoming / get_past_events"),
    (r"\bnotes\.app\b|\bmy notes\b",
     "search_notes"),
    (r"\breminders?\b",
     "add_reminder / get_upcoming"),
]

# Capabilities that need an OS permission or privilege the script cannot grant
# itself. These are NOT blocked — it's the user's own machine — but the user is
# told plainly what's being requested before they approve, rather than finding
# out when the tool silently fails or quietly returns nothing.
_SENSITIVE_CAPABILITY: list[tuple[str, str]] = [
    (r"\bkeychain\b|\bpasswords?\b|\bcredentials?\b|\bsecrets?\b|\bapi[_ ]?keys?\b|"
     r"\btokens?\b|\bssh keys?\b|\b\.env\b",
     "reads stored credentials"),
    (r"\bsudo\b|\broot\b|\badmin(istrator)? (rights|access|privileges)\b|"
     r"\bprivilege escalation\b|\belevate\b",
     "runs with administrator privileges"),
    (r"\bcsrutil\b|\bdisable sip\b|\bsystem integrity\b|\bgatekeeper\b|\bspctl\b|"
     r"\btcc\.db\b|\bbypass\b|\bcircumvent\b|\bdisable .*(protection|security)\b",
     "bypasses a macOS security protection"),
    (r"\bcamera\b|\bmicrophone\b|\bscreen recording\b|\bkeylog\b|\brecord (the )?screen\b",
     "accesses the camera, microphone, or screen"),
    (r"\bphotos?\.app\b|\bphoto library\b",
     "reads the Photos library"),
    (r"\blocation\b|\bgps\b|\bwhere i am\b",
     "reads device location"),
    (r"\bbrowser history\b|\bsafari history\b|\bchrome history\b|\bcookies?\b",
     "reads browser history or cookies"),
]

# Hard-coded stand-ins that mean the model invented output instead of computing
# it. Checked against generated code, not against the user's task.
_FABRICATION_MARKERS = [
    r"john\s+doe", r"jane\s+(smith|doe)", r"alice\s+johnson", r"bob\s+smith",
    r"555-?\d{4}", r"\bexample\.(com|org|net)\b", r"\bfoo@bar\b",
    r"\b(sample|placeholder|dummy|mock|fake)_(data|contacts|results|list|values)\b",
    r"#\s*(sample|placeholder|dummy|mock|fake|example) data",
]


def _capability_check(name: str, description: str, task: str) -> tuple[str, str]:
    """(redirect_error, sensitivity_warning) for a proposed tool.

    A redirect is fatal — Wisp already has a real path to that data and a
    generated script would only be able to fake it. A warning is not fatal:
    it's surfaced on the confirmation card so the user approves knowing what
    the tool will reach for.
    """
    blob = f"{name} {description} {task}".lower()
    for pattern, native in _NATIVE_CAPABILITY:
        if re.search(pattern, blob):
            return (f"Wisp already reads this through {native} — and a generated "
                    "script can't reach it anyway, because the TCC permission "
                    "belongs to the Wisp app, not to a script it spawns. Use "
                    "that tool instead of building one. If the existing tool "
                    "genuinely doesn't cover what was asked, say so plainly "
                    "rather than creating a tool that would invent the data."), ""
    hits = [label for pattern, label in _SENSITIVE_CAPABILITY if re.search(pattern, blob)]
    return "", "; ".join(dict.fromkeys(hits))


def _fabrication_check(code: str) -> str:
    """A reason to reject generated code that ships invented data, or ""."""
    lowered = code.lower()
    for marker in _FABRICATION_MARKERS:
        if re.search(marker, lowered):
            return (f"the generated code contains invented placeholder data "
                    f"(matched {marker!r}) instead of computing a real result")
    return ""


def _validate_params(parameters: list) -> tuple[list[dict], str]:
    """Normalize and sanity-check the parameter list. Returns (clean, error)."""
    clean: list[dict] = []
    seen = set()
    for p in parameters or []:
        if not isinstance(p, dict):
            return [], f"each parameter must be an object, got {p!r}"
        name = str(p.get("name") or "").strip()
        if not re.match(r"^[a-z][a-z0-9_]{0,32}$", name):
            return [], (f"parameter name {name!r} must be lowercase "
                        "letters/digits/underscore, starting with a letter")
        if name in seen:
            return [], f"duplicate parameter name: {name}"
        seen.add(name)
        clean.append({
            "name": name,
            "type": str(p.get("type") or "string"),
            "description": str(p.get("description") or ""),
            "required": bool(p.get("required", False)),
        })
    return clean, ""


def _param_flags(parameters: list[dict]) -> str:
    lines = [
        f"  --{p['name']} ({p['type']}, {'REQUIRED' if p['required'] else 'optional'}): "
        f"{p['description'] or '(no description given)'}"
        for p in parameters
    ]
    return "\n".join(lines) if lines else "  (no parameters — the script takes no flags)"


async def _generate(name: str, task: str, params: list[dict],
                    read_scopes: list | None = None,
                    write_scopes: list | None = None) -> tuple[str, str]:
    """(code, error). Runs the on-device model; never raises."""
    prompt = (f"Write a script named for the tool '{name}'.\n"
              f"What it does: {task}\n\n"
              f"Command-line flags it must accept:\n{_param_flags(params)}\n")
    if read_scopes or write_scopes:
        # Told to the model so it writes code that stays inside the boundary
        # rather than code the kernel kills halfway through — a partial run
        # that already deleted something is worse than a clean refusal.
        allowed = ", ".join(str(p) for p in (write_scopes or []))
        readable = ", ".join(str(p) for p in (read_scopes or []))
        prompt += ("\nThis script runs INSIDE a sandbox enforced by the OS.\n"
                   + (f"It may read: {readable}\n" if readable else "")
                   + (f"It may create/modify/delete in: {allowed}\n" if allowed else "")
                   + "Any access outside those folders will be refused by the "
                     "kernel. Do not attempt it, and validate paths before "
                     "acting so a refusal can't leave work half-finished.\n")
    # Pinned to the agent model (the agent model) rather than the `coding` role's
    # super-model branch: it's the model that will CALL the resulting tool, so
    # it should shape the argument list, and it's already resident mid-turn so
    # this costs no model swap.
    coder = role_to_model("agent")
    c = _c()
    await c.ensure_only(coder)
    messages = [{"role": "system", "content": _CODER_SYS},
                {"role": "user", "content": prompt}]
    try:
        resp = await c.chat(
            coder, messages,
            max_tokens=6000,
        )
        # A previously-uninstrumented code-generation path, so this gets
        # the exact same debug_capture treatment: recorded BEFORE any of the
        # validation below can bail out early, so even a rejected/failed
        # draft still shows what the model actually produced. See
        # service/debug_capture.py and its call sites in
        # email_tools.py/imessage_tools.py.
        from service import debug_capture
        debug_capture.record("model_call", model=coder,
                             request={"messages": messages, "max_tokens": 6000},
                             response=resp)
        code = (resp["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:  # noqa: BLE001
        return "", f"the local coding model failed: {e}"

    code = _strip_fences(code)
    if not code:
        return "", "the coding model returned no code"
    # The model's own escape hatch (see _CODER_SYS) — it judged the task
    # impossible under the rules rather than faking a result. Surface its
    # reason verbatim; that's far more useful than a generic failure.
    if code.startswith("CANNOT_IMPLEMENT:"):
        return "", code.split(":", 1)[1].strip() or "the model judged this task impossible"
    try:
        compile(code, f"{name}.py", "exec")
    except SyntaxError as e:
        return "", f"the generated code has a syntax error ({e})"
    if (fake := _fabrication_check(code)):
        return "", fake
    return code, ""


async def prepare_draft(args: dict) -> str:
    """Generate and cache the code for a proposed tool, WITHOUT installing it.

    Run by the agent loop immediately before create_tool's confirmation card,
    so the card can display the real code. Returns "" on success, or a
    human-readable reason the loop should surface instead of asking for
    approval — there's no point prompting the user to approve a tool that
    couldn't be written.
    """
    name = str(args.get("name") or "").strip().lower()
    if not _NAME_RE.match(name):
        return (f"bad tool name {name!r} — use lowercase letters, digits, and "
                "underscores, starting with a letter")
    if name in REGISTRY and not (SKILLS_DIR / name).exists():
        return f"a built-in tool is already called {name!r} — pick another name"

    clean_params, err = _validate_params(args.get("parameters") or [])
    if err:
        return err

    description = str(args.get("description") or "").strip()
    task = str(args.get("task") or "")
    redirect, warning = _capability_check(name, description, task)
    if redirect:
        return redirect

    read_scopes, scope_err = parse_scopes(args.get("read_scopes"))
    if scope_err:
        return f"read_scopes: {scope_err}"
    write_scopes, scope_err = parse_scopes(args.get("write_scopes"))
    if scope_err:
        return f"write_scopes: {scope_err}"

    code, gen_err = await _generate(name, task, clean_params,
                                    read_scopes, write_scopes)
    if gen_err:
        return gen_err

    _pending[name] = {
        "code": code,
        "description": description,
        "parameters": clean_params,
        "warning": warning,
        "read_scopes": read_scopes,
        "write_scopes": write_scopes,
        "ts": time.time(),
    }
    return ""


def draft_code(name: str) -> str:
    """The exact code a cached draft will install — for the confirmation card."""
    draft = _pending.get((name or "").strip().lower())
    return draft["code"] if draft else ""


def draft_warning(name: str) -> str:
    """What privileged capability this draft reaches for, or "". Shown on the
    confirmation card so approving it is an informed decision rather than a
    surprise discovered later."""
    draft = _pending.get((name or "").strip().lower())
    return (draft or {}).get("warning", "")


def draft_scope_line(name: str) -> str:
    """How far this draft can reach on disk, for the confirmation card."""
    draft = _pending.get((name or "").strip().lower())
    if not draft:
        return ""
    return describe_scopes(draft.get("read_scopes") or [],
                           draft.get("write_scopes") or [],
                           sandboxed=sandbox_available())


@register(
    "create_tool",
    "Build a NEW tool for a capability you don't already have, using the "
    "on-device coding model — entirely local, nothing leaves the machine. Use "
    "this when a request needs something no existing tool covers (check your "
    "tool list first) AND the user would plausibly want it again; for a "
    "one-off calculation just use run_shell instead. Describe the tool "
    "precisely in `task` — it's written from that description alone, must be "
    "Python standard-library only, and takes one command-line flag per "
    "parameter you declare. The user sees the actual generated code on a "
    "confirmation card and approves or rejects it there, so don't ask "
    "permission in text first. Say what you're building in one line, call this "
    "once, then USE the tool you just made to finish answering — it becomes "
    "callable immediately, in this same turn. Never end your turn telling the "
    "user to ask again.",
    {"type": "object",
     "properties": {
         "name": {"type": "string",
                  "description": "lowercase_with_underscores, e.g. 'business_days_between'"},
         "description": {"type": "string",
                         "description": "one sentence: what the tool does"},
         "task": {"type": "string",
                  "description": "precise spec of the behavior to implement"},
         "parameters": {
             "type": "array",
             "description": "the tool's arguments; may be empty",
             "items": {"type": "object",
                       "properties": {
                           "name": {"type": "string"},
                           "type": {"type": "string"},
                           "description": {"type": "string"},
                           "required": {"type": "boolean"},
                       }},
         },
         "read_scopes": {
             "type": "array",
             "items": {"type": "string"},
             "description": "folders the tool may READ, e.g. ['~/Downloads']. "
                            "Omit unless it genuinely needs the user's files — "
                            "with none, it can't read anything of theirs.",
         },
         "write_scopes": {
             "type": "array",
             "items": {"type": "string"},
             "description": "folders the tool may CREATE, MODIFY, or DELETE in. "
                            "Request the narrowest folder that does the job; "
                            "'~' and '/' are refused. Implies read access too.",
         },
     },
     "required": ["name", "description", "task", "parameters"]},
    category="tool_authoring",
)
async def create_tool(name: str, description: str = "", task: str = "",
                      parameters: list | None = None,
                      read_scopes: list | None = None,
                      write_scopes: list | None = None) -> str:
    name = (name or "").strip().lower()
    draft = _pending.get(name)
    if draft and time.time() - draft["ts"] > _DRAFT_TTL_S:
        _pending.pop(name, None)
        draft = None
    if not draft:
        # No cached draft — the loop normally fills this via prepare_draft, so
        # this path only runs if that was skipped. Generate now rather than
        # fail; the cost is that the confirmation card had no code to show.
        err = await prepare_draft({"name": name, "description": description,
                                   "task": task, "parameters": parameters or [],
                                   "read_scopes": read_scopes or [],
                                   "write_scopes": write_scopes or []})
        if err:
            return f"(couldn't create this tool: {err})"
        draft = _pending[name]

    skill_dir = SKILLS_DIR / name
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "tool.py").write_text(draft["code"])
        # Built as a dict and dumped with yaml.safe_dump. A hand-rolled
        # f-string of repr()'d text parsed fine in testing by luck of Python's
        # quote-picking, but breaks the moment a description mixes quote styles
        # or starts with a YAML-special character (@, %, !, *, &, …).
        meta = {
            "name": name,
            "description": draft["description"] or name,
            "enabled": True,
            "tools": [{
                "name": name,
                "description": draft["description"] or name,
                "script": "tool.py",
                "parameters": {
                    p["name"]: {"type": p["type"], "description": p["description"],
                                "required": p["required"]}
                    for p in draft["parameters"]
                },
                # Persisted so the sandbox is rebuilt identically on every
                # later run, not just the one the user approved.
                "read_scopes": [str(p) for p in draft.get("read_scopes") or []],
                "write_scopes": [str(p) for p in draft.get("write_scopes") or []],
            }],
        }
        skill_md = ("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n"
                    f"Self-authored tool. Generated locally, {time.strftime('%Y-%m-%d')}.\n")
        (skill_dir / "SKILL.md").write_text(skill_md)
    except Exception as e:  # noqa: BLE001
        return f"(couldn't write the new tool to disk: {e})"

    reload_skills()
    _pending.pop(name, None)

    if name not in REGISTRY:
        # Written but not registered — most likely skills/tools.py's
        # shadow-guard declined it. Report that honestly rather than claim
        # success for something that isn't actually callable.
        return (f"(wrote the tool but it did NOT register — {name!r} likely "
                "collides with an existing tool. Suggest a different name.)")
    return (f"Created '{name}' and it is available RIGHT NOW, in this same turn. "
            f"If the user's request needs it, call {name} immediately and answer "
            "them — do not tell them to ask again. If they only asked you to "
            "build it and there's nothing to run it on yet, just confirm it's "
            "ready and say what it does.")
