"""train.py's pause / snapshot / resume decisions as pure functions (no torch needed)."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import train as t  # noqa: E402


def test_parse_steps():
    assert t.parse_steps("50,100, 200") == {50, 100, 200}
    assert t.parse_steps("") == frozenset() and t.parse_steps(None) == frozenset()
    for bad in ("0", "-5", "x", "50,abc"):
        with pytest.raises(ValueError):
            t.parse_steps(bad)


def test_snapshot_due_adds_early_steps_to_the_cadence():
    extra = t.parse_steps("50,100,200")
    assert [s for s in (50, 100, 150, 190, 200, 380, 400) if t.snapshot_due(s, 190, extra)] == [50, 100, 190, 200, 380]
    assert not t.snapshot_due(50, 190, frozenset())
    assert t.snapshot_due(50, 0, extra) and not t.snapshot_due(60, 0, extra)      # --save-every 0 still honours the list


def test_control_action():
    ca = t.control_action
    assert ca(10, 400, False, False) is None
    assert ca(400, 400, False, False) == "pause"
    assert ca(401, 400, False, False) is None            # a run resumed at 400 does not pause again
    assert ca(10, 0, True, False) == "pause"
    assert ca(0, 0, False, False) is None                # pause_at 0 = off
    assert ca(10, 0, False, True) == "deadline"
    assert ca(400, 400, False, True) == "pause" and ca(10, 0, True, True) == "pause"


def test_resume_position_matches_an_uninterrupted_walk():
    spe, gb = 7, 8
    walk = [(s // spe, (s % spe) * gb) for s in range(20)]
    assert [t.resume_position(s, spe, gb) for s in range(20)] == walk
    assert t.resume_position(400, 755, 8) == (0, 3200) and t.resume_position(755, 755, 8) == (1, 0)


def test_pause_record_and_resumable_dir(tmp_path):
    rec = t.pause_record(400, "pause-at-step", 1_700_000_000.0, "/o/step-00400", "/o")
    assert rec["step"] == 400 and rec["reason"] == "pause-at-step" and rec["resume_dir"] == "/o/step-00400"
    assert "resume" in rec["resume"] and json.loads(json.dumps(rec)) == rec
    d = tmp_path / "step-00400"
    d.mkdir()
    assert "adapter_model.safetensors" in t.resumable_dir(d)
    (d / "adapter_model.safetensors").write_text("x")
    assert "trainer_state.pt" in t.resumable_dir(d)
    (d / "trainer_state.pt").write_text("x")
    assert t.resumable_dir(d) is None
