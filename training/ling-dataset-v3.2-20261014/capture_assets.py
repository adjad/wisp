#!/usr/bin/env python3
"""Capture assets/tools.json, assets/system_parts.json and assets/short_descriptions.json from Wisp SOURCE.

Unlike V3/V3.1 (whose assets were copied from an eval run's captured prompts), everything here is produced by
importing Wisp's own prompt and registry builders from READ-ONLY checkouts. It loads no model, contacts no server,
executes no tool, and redirects HOME / WISP_HOME into a temp dir before importing anything, so the real ~/.moe,
~/.omlx and ~/Library are never read or written. Needs the Wisp virtualenv (the service imports its deps).

    .venv/bin/python training/ling-dataset-v3.2-20261014/capture_assets.py \
        --main <git archive of origin/main> --main-sha <sha> \
        --skill-root <git archive of the mac-controls skill commit> --skill-sha <sha> \
        --short-export <scripts/export_short_descriptions.py output> --short-sha <sha of that checkout>

What is captured (and how the generator uses it):
  * every built-in tool's model-facing schema (registry.tool_schemas, full mode), plus the runtime-registered
    skill tools: use_skill and the mac-controls skill's five mac_* tools, registered by service.skills.load()
    exactly as the app does at startup (bundled skills only; no user skills);
  * the capability catalog (families in FAMILIES order, the preamble lines of CapabilityCatalog.prompt()) and the
    get_tool_schemas discovery schema (family enum in catalog insertion order);
  * loop.SYSTEM split into blocks with the tool names that gate each block (loop._system_blocks), so the
    generator can reproduce build_system(offered) without a hand-written trigger map;
  * the identity block (identity_prompt_block, verbatim shape) rendered for the synthetic Test User;
  * the installed-skills catalog inputs (skills_context_block: header, each enabled skill's name, description,
    triggers and body) so the per-turn skills block is reproduced from the user's text;
  * the model-led context note inserted by service/main.py.
"""
import argparse, json, os, re, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_NAME, TEST_EMAIL = "Test User", "test.user@example.invalid"
SKILL_TOOLS = ["mac_get_volume", "mac_set_volume", "mac_mute", "mac_lock_screen", "mac_media"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--main", required=True, type=Path)
    ap.add_argument("--main-sha", required=True)
    ap.add_argument("--skill-root", required=True, type=Path)
    ap.add_argument("--skill-sha", required=True)
    ap.add_argument("--short-export", required=True, type=Path)
    ap.add_argument("--short-sha", required=True)
    ap.add_argument("--terse-registry", required=True, type=Path,
                    help="service/tools/registry.py from the short-description commit (its _strip_parameter_descriptions is used)")
    ap.add_argument("--out", type=Path, default=HERE / "assets")
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="wisp-capture-"))
    os.environ["HOME"] = str(tmp / "home")
    os.environ["WISP_HOME"] = str(tmp / "home" / ".moe")
    os.environ.pop("WISP_TERSE_TOOL_SCHEMAS", None)   # capture FULL schemas; terse is rendered by the generator
    Path(os.environ["WISP_HOME"]).mkdir(parents=True)
    sys.path.insert(0, str(a.main.resolve()))

    import service.tools  # noqa: F401  registers the built-ins
    from service.tools.registry import REGISTRY, tool_schemas, private_shell_target, private_shell_implementation
    builtin = sorted(n for n, t in REGISTRY.items() if t.func.__module__.startswith("service.tools."))
    import service.skills as skills
    bundled = tmp / "bundled"
    shutil.copytree(skills.BUNDLED_SKILLS_DIR, bundled)
    shutil.copytree(a.skill_root / "service/skills/bundled/mac-controls", bundled / "mac-controls")
    skills.BUNDLED_SKILLS_DIR = bundled
    found = skills.load()
    from service.router import model_led as ML
    import service.agent.loop as L
    from service.memory import identity as ID

    for n in SKILL_TOOLS + ["use_skill"]:
        assert n in REGISTRY, n
    shell_blocked = sorted(t.name for t in REGISTRY.values() if private_shell_target(t) and not private_shell_implementation(t))
    specs = ML.registry_specs(REGISTRY)
    # A synthetic identity is always present, so loop.run_agent sets private_shell_read_only and blocks these.
    cat = ML.CapabilityCatalog.build(specs, blocked=frozenset(shell_blocked))
    unavailable = {s.name: s.unavailable_reason for s in specs if s.unavailable_reason}
    names = sorted(set(cat.tools) | set(shell_blocked))   # blocked schemas kept for reference, never offered
    schemas = tool_schemas(names)
    assert [s["function"]["name"] for s in schemas] == names
    fam_of = {t: f for f, ts in cat.families.items() for t in ts}
    fam_of.update({t: ML.family_for(next(s for s in specs if s.name == t)) for t in shell_blocked})
    catalog_tools = {f: list(ts) for f, ts in cat.families.items()}
    prompt = cat.prompt()
    head = prompt[:prompt.index("Available capabilities")].rstrip("\n").split("\n")
    noweb = ML.CapabilityCatalog.build(specs, blocked=frozenset(ML.PRIVATE_WEB_TOOLS) | frozenset(shell_blocked))
    tools = {
        "source": {"wisp_main_sha": a.main_sha, "mac_controls_skill_sha": a.skill_sha,
                   "captured_by": "capture_assets.py (registry.tool_schemas, full mode; skills.load() for skill tools)",
                   "builtin_registered": len(builtin), "unavailable_not_offered": unavailable,
                   "private_shell_blocked": shell_blocked},
        "discovery_schema_step0": [ML.discovery_schema(cat)],
        "discovery_schema_noweb": [ML.discovery_schema(noweb)],
        "full_registry_schemas": schemas,
        "tool_family": fam_of,
        "catalog_families": catalog_tools,
        "catalog_families_noweb": {f: list(ts) for f, ts in noweb.families.items()},
        "tool_category": {n: REGISTRY[n].category for n in names},
        "skill_tools": SKILL_TOOLS + ["use_skill"],
    }

    blocks = [{"named": sorted(n), "text": t} for n, t in L._system_blocks()]
    ID.user_name = lambda: TEST_NAME
    ID.user_emails = lambda: [TEST_EMAIL]
    identity = ID.identity_prompt_block(with_date=False, shape="verbatim", messages=True)
    assert TEST_EMAIL in identity and TEST_NAME in identity
    main_src = (a.main / "service/main.py").read_text()
    m = re.search(r'messages\.insert\(0, \{"role": "system", "content": \((.*?)\)\}\)', main_src, re.S)
    context_note = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))
    enabled = [s for s in found.values() if s.enabled and not s.error]
    hdr_src = (a.main / "service/skills/__init__.py").read_text()
    hm = re.search(r'out\.append\(("\\nInstalled skills.*?)\)\n', hdr_src, re.S)
    header = "".join(json.loads(x) for x in re.findall(r'"(?:[^"\\]|\\.)*"', hm.group(1)))
    system_parts = {
        "source": {"wisp_main_sha": a.main_sha, "mac_controls_skill_sha": a.skill_sha,
                   "builders": ["service/agent/loop.py SYSTEM + _system_blocks + build_system",
                                "service/router/model_led.py CapabilityCatalog.prompt / FAMILIES",
                                "service/memory/identity.py identity_prompt_block(with_date=False, shape='verbatim')",
                                "service/skills/__init__.py skills_context_block (catalog=True)",
                                "service/main.py model-led context note"]},
        "system_blocks": blocks,
        "identity_block": identity,
        "catalog": {"family_order": list(ML.FAMILIES), "descriptions": dict(ML.FAMILIES), "preamble_lines": head,
                    "capabilities_header": "Available capabilities (only currently admitted tools are listed):"},
        "skills_block": {"header": header,
                         "skills": [{"name": s.name, "description": s.description, "triggers": s.triggers,
                                     "body": s.body} for s in enabled]},
        "context_note": context_note,
        "now_line": "The current date and time is {when}. Resolve relative dates/times against this.",
        "catalog_prompt_full": prompt,
    }
    short = json.loads(a.short_export.read_text())
    # Reference renderings computed by WISP's own code, so validate.py can prove the stdlib mirrors in generate.py match.
    import ast, hashlib
    tree = ast.parse(a.terse_registry.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_strip_parameter_descriptions")
    ns = {"Any": object}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "terse_registry", "exec"), ns)
    strip = ns["_strip_parameter_descriptions"]
    h = lambda o: hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    terse_ref = {n: h({"type": "function", "function": {"name": n, "description": short["tools"].get(n, REGISTRY[n].description),
                                                       "parameters": strip(REGISTRY[n].parameters)}}) for n in names}
    L._BLOCKS_CACHE = None
    sets = {"(none)": set(), "(all)": set(names), **{n: {n} for n in names}}
    build_ref = {k: hashlib.sha256(L.build_system(v).encode()).hexdigest() for k, v in sets.items()}
    sample_texts = ["turn the volume down a bit", "pause the music", "help me refine this idea for a startup",
                    "what's on my calendar tomorrow", "@interview-me about my plan", "lock my screen please"]
    skills_ref = {t: skills.skills_context_block(t, "", True) for t in sample_texts}
    verification = {"terse_schema_sha256": terse_ref, "build_system_sha256": build_ref, "skills_context_block": skills_ref}
    short_asset = {"source_sha": a.short_sha, "export_reported_sha": short.get("source_sha"),
                   "placeholder": False, "tools": short["tools"],
                   "note": "scripts/export_short_descriptions.py run on a read-only git archive of source_sha (not a git "
                           "checkout, so the script itself reports 'unknown'); tools without an entry (skill tools, "
                           "use_skill) keep their full description, as service.tools.short_descriptions does."}
    a.out.mkdir(exist_ok=True)
    (a.out / "tools.json").write_text(json.dumps(tools, indent=1, ensure_ascii=False) + "\n")
    (a.out / "system_parts.json").write_text(json.dumps(system_parts, indent=1, ensure_ascii=False) + "\n")
    (a.out / "capture_verification.json").write_text(json.dumps(verification, indent=1, ensure_ascii=False) + "\n")
    (a.out / "short_descriptions.json").write_text(json.dumps(short_asset, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({"schemas": len(schemas), "builtin_registered": len(builtin), "families": list(cat.families),
                      "blocks": len(blocks), "skills": [s.name for s in enabled], "unavailable": list(unavailable),
                      "shell_blocked": shell_blocked}, indent=1))
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
