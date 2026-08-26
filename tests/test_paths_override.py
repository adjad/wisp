"""service/paths.py — WISP_HOME redirection, regression tests.

The 10 `.moe` constants (assistant.db, cache/, sessions.db, facts.db,
config.yaml, mcp.json, grants.json, audit dir, skills/) are all resolved at
IMPORT time from service.paths.MOE_DIR, which reads WISP_HOME once at import.
That makes them impossible to test by just re-importing in-process (the
module is already cached) — each case has to boot a fresh interpreter.

What must keep holding:
  * every one of the 10 constants lands under WISP_HOME when it's set;
  * with WISP_HOME unset, every one still resolves under the real ~/.moe
    (byte-identical to pre-sandbox behavior);
  * the two ~/.omlx constants (model roster, favorites) are NEVER redirected
    by WISP_HOME — only $HOME itself would move those, and the sandbox must
    not do that or it boots with no models.

    .venv/bin/python tests/test_paths_override.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "bin" / "python")

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


# Every constant the sandbox needs isolated, and where it lives.
MOE_CONSTANTS = {
    "service.assistant.store": ["DB_PATH"],
    "service.assistant.outbound_queue": ["DB_PATH"],
    "service.tools.cache_store": ["CACHE_DIR"],
    "service.memory.store": ["DB_PATH"],
    "service.memory.facts": ["DB_PATH"],
    "service.config": ["USER_CONFIG"],
    "service.mcp": ["CONFIG_PATH"],
    "service.safety.grants": ["GRANTS_PATH"],
    "service.safety.audit": ["AUDIT_DIR"],
    "service.skills": ["SKILLS_DIR"],
}

# Never redirected by WISP_HOME — only real ~/.omlx.
OMLX_CONSTANTS = {"service.config": ["OMLX_SETTINGS", "OMLX_MODEL_SETTINGS"]}


def _resolve(constants: dict[str, list[str]], env: dict[str, str]) -> dict[str, str]:
    """Boot a fresh interpreter, import each module, print its constants as
    JSON. Import-time state means this can't be done any other way.

    Pulls modules from sys.modules rather than `import a.b.c as x` — the
    latter walks attribute access down the dotted path, and some __init__.py
    files (e.g. service/memory: `from service.memory.store import store`)
    shadow the submodule attribute with an instance of the same name, so the
    attribute-walk resolves to the instance instead of the module."""
    lines = ["import json, sys, importlib", "out = {}"]
    for mod, names in constants.items():
        lines.append(f"importlib.import_module({mod!r})")
        lines.append(f"_m = sys.modules[{mod!r}]")
        for name in names:
            lines.append(f"out[{mod!r} + '.' + {name!r}] = str(_m.{name})")
    lines.append("print(json.dumps(out))")
    proc = subprocess.run([PYTHON, "-c", "\n".join(lines)],
                          cwd=str(ROOT), env=env, capture_output=True, text=True,
                          timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"subprocess failed: {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_wisp_home_redirects_all_twelve() -> None:
    print("\nWISP_HOME set: all 12 .moe constants land under it")
    scratch = "/tmp/wisp-paths-test-scratch"
    env = dict(os.environ)
    env["WISP_HOME"] = scratch
    resolved = _resolve(MOE_CONSTANTS, env)
    for key, value in resolved.items():
        check(f"{key} under WISP_HOME", value.startswith(scratch),
              f"got {value}")


def test_wisp_home_unset_stays_on_real_home() -> None:
    print("\nWISP_HOME unset: constants resolve under the real ~/.moe")
    env = dict(os.environ)
    env.pop("WISP_HOME", None)
    real_home = env.get("HOME", str(Path.home()))
    resolved = _resolve(MOE_CONSTANTS, env)
    expected_prefix = str(Path(real_home) / ".moe")
    for key, value in resolved.items():
        check(f"{key} under real ~/.moe", value.startswith(expected_prefix),
              f"got {value}, expected prefix {expected_prefix}")


def test_omlx_constants_never_redirected() -> None:
    print("\nWISP_HOME set: ~/.omlx constants are untouched")
    env = dict(os.environ)
    env["WISP_HOME"] = "/tmp/wisp-paths-test-scratch"
    real_home = env.get("HOME", str(Path.home()))
    resolved = _resolve(OMLX_CONSTANTS, env)
    expected_prefix = str(Path(real_home) / ".omlx")
    for key, value in resolved.items():
        check(f"{key} still under real ~/.omlx", value.startswith(expected_prefix),
              f"got {value}, expected prefix {expected_prefix}")


def test_wisp_home_expands_user() -> None:
    print("\nWISP_HOME with ~ expands correctly")
    env = dict(os.environ)
    env["WISP_HOME"] = "~/.wisp-paths-test-tilde"
    resolved = _resolve({"service.tools.cache_store": ["CACHE_DIR"]}, env)
    value = resolved["service.tools.cache_store.CACHE_DIR"]
    check("no literal tilde left in the resolved path", "~" not in value,
          f"got {value}")


def main() -> int:
    test_wisp_home_redirects_all_twelve()
    test_wisp_home_unset_stays_on_real_home()
    test_omlx_constants_never_redirected()
    test_wisp_home_expands_user()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
