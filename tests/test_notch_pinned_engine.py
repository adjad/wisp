"""Engine pinning for the contained Notch evaluation, outside the latch.

Nothing here contacts an engine, runs the attribution or starts a process:
the import refusal, the host's zero-exec profile, the attestation's use of the
production classes and the driver's stop are checked as code and data.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.notch_evaluation import driver, host  # noqa: E402


def test_the_pinned_transport_imports_only_under_the_latch():
    from service import evaluation_boundary
    assert not evaluation_boundary.active()
    sys.modules.pop("scripts.notch_evaluation.pinned_engine", None)
    with pytest.raises(ImportError, match="evaluation-only"):
        importlib.import_module("scripts.notch_evaluation.pinned_engine")
    assert "scripts.notch_evaluation.pinned_engine" not in sys.modules


def test_the_host_still_runs_no_process(tmp_path):
    config = host.HostConfig(run_id="pin-1", owned_root=str(tmp_path), listen_port=18891, token="t" * 40,
                             nonce="n" * 24, inference_url=host.PINNED_ENGINE_URL, allowed_models=("SYNTH",),
                             engine_pin={"pid": 1})
    config.validate()
    profile = host.sandbox_profile(config, sys.executable)
    images = re.search(r"\(deny process-exec \(require-not \(require-any (.*?)\)\)\)", profile).group(1)
    assert "lsof" not in profile and "/bin/ps" not in profile
    assert all("ython" in literal for literal in re.findall(r'\(literal "([^"]+)"\)', images))
    assert "(deny process-fork)" in profile
    assert host.AuditGuard(config).exec_allow == frozenset()


def test_attestation_uses_the_production_attribution_unchanged():
    source = (ROOT / "scripts" / "notch_evaluation" / "engine_attest.py").read_text(encoding="utf-8")
    assert "RuntimeAuthority().load()" in source and "isinstance(authority, DesktopOmlx)" in source
    assert "authority.binding()" in source and 'ENGINE_HOST, ENGINE_PORT = "127.0.0.1", 8000' in source
    pinned = (ROOT / "scripts" / "notch_evaluation" / "pinned_engine.py").read_text(encoding="utf-8")
    assert "subprocess" not in pinned and "lsof" not in pinned.split('"""', 2)[2]
    assert "RuntimeAuthority" not in pinned.split('"""', 2)[2]          # never loads the ~/.moe manifest
    build = (ROOT / "scripts" / "build_notch_evaluation.sh").read_text(encoding="utf-8")
    assert 'attest-engine --model "${models[0]}"' in build and '"engine_pin\\":$pin' in build


def test_a_lost_pin_stops_the_run_as_engine_not_ready(tmp_path, monkeypatch):
    (tmp_path / "evidence").mkdir()
    assert not driver._pin_lost(tmp_path)
    (tmp_path / "evidence" / driver.PIN_LOST).write_text(json.dumps({"reason": "SYNTHETIC"}))
    assert driver._pin_lost(tmp_path)
    monkeypatch.setattr(driver, "engine_resident", lambda engine, model: True)
    from scripts.notch_evaluation import corpus
    case = corpus.Case(case_id=7, risk_tag="R", text="SYNTHETIC", segments=("SYNTHETIC",), section="S")
    result = driver.run_case(case, owned=tmp_path, token="t", engine="http://127.0.0.1:1", model="SYNTH",
                             timeout=1, stopping=lambda: False)
    assert result["failure_class"] == "ENGINE_NOT_READY" and "ENGINE_NOT_READY" in driver.STOPPING
