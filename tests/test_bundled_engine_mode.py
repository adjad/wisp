"""Engine-mode wiring: external oMLX by default, Wisp's own engine only when asked.

Mode is resolved at import, so every case boots a fresh interpreter (as
test_paths_override.py does).

    python tests/test_bundled_engine_mode.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = FAIL = 0

PROBE = r'''
import json
from service.config import OMLX_SETTINGS, OMLX_MODEL_SETTINGS, omlx_base_url
from service.config.endpoints import is_local_provider_origin
from service.identity import PRODUCTION_PORTS
from service.engine.mode import MODE
out = {"mode": MODE, "settings": str(OMLX_SETTINGS), "model_settings": str(OMLX_MODEL_SETTINGS),
       "ports": sorted(PRODUCTION_PORTS), "blocked": not is_local_provider_origin("http://127.0.0.1:8791")}
try:
    out["base_url"] = omlx_base_url()
except Exception as exc:
    out["base_url"] = "ERR " + type(exc).__name__
try:
    from service.config import omlx_api_key
    out["key"] = omlx_api_key()
except Exception as exc:
    out["key"] = "ERR " + type(exc).__name__
print(json.dumps(out))
'''


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def probe(home: Path, **env: str) -> dict:
    full = {k: v for k, v in os.environ.items() if not k.startswith(("WISP_", "MOE_"))}
    full.update({"WISP_HOME": str(home), "PYTHONPATH": str(ROOT), **env})
    done = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=full,
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise RuntimeError(done.stderr[-800:])
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_default_is_external() -> None:
    with tempfile.TemporaryDirectory() as t:
        r = probe(Path(t))
        check("default mode", r["mode"] == "external-omlx", r["mode"])
        check("uses ~/.omlx", r["settings"] == str(Path.home() / ".omlx/settings.json"), r["settings"])
        check("model settings in ~/.omlx", r["model_settings"] == str(Path.home() / ".omlx/model_settings.json"))
        check("base url is 8000", r["base_url"].endswith(":8000") or r["base_url"].startswith("ERR"), r["base_url"])


def test_bundled_is_isolated() -> None:
    with tempfile.TemporaryDirectory() as t:
        home = Path(t)
        base = home / "engine" / "base"
        base.mkdir(parents=True)
        (base / "settings.json").write_text(json.dumps({"auth": {"api_key": "k" * 64}}))
        r = probe(home, WISP_ENGINE_MODE="bundled")
        check("mode bundled", r["mode"] == "bundled")
        check("settings under engine base", r["settings"] == str(base / "settings.json"), r["settings"])
        check("model settings under engine base", r["model_settings"] == str(base / "model_settings.json"))
        check("base url is private port", r["base_url"] == "http://127.0.0.1:8791", r["base_url"])
        check("engine key from its own settings", r["key"] == "k" * 64, r["key"])
        check("nothing points at ~/.omlx", ".omlx" not in r["settings"] + r["model_settings"])


def test_config_and_bad_values() -> None:
    with tempfile.TemporaryDirectory() as t:
        home = Path(t)
        (home / "config.yaml").write_text("engine:\n  mode: bundled\n")
        check("overlay selects bundled", probe(home)["mode"] == "bundled")
        check("env beats overlay", probe(home, WISP_ENGINE_MODE="external-omlx")["mode"] == "external-omlx")
        check("junk value falls back", probe(home, WISP_ENGINE_MODE="turbo")["mode"] == "external-omlx")


def test_port_reservations() -> None:
    with tempfile.TemporaryDirectory() as t:
        r = probe(Path(t))
        check("local provider cannot claim 8791", r["blocked"] is True)
        check("sandbox cannot bind 8791", 8791 in r["ports"], str(r["ports"]))


def main() -> int:
    for fn in (test_default_is_external, test_bundled_is_isolated, test_config_and_bad_values,
               test_port_reservations):
        print(fn.__name__)
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
