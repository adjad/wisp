"""Offline tests for --init-adapter's config check (r5.1 weights-only start). No torch needed."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import train  # noqa: E402

R5_1800 = Path.home() / "Desktop/r5-pilot/box-final/ask-lora-out/step-01800/adapter_config.json"
A = argparse.Namespace(rank=16, alpha=16)
GOOD = {"peft_type": "LORA", "r": 16, "lora_alpha": 16, "lora_dropout": 0.05, "target_modules": train.TARGET_RE,
        "bias": "none", "use_dora": False, "use_rslora": False, "modules_to_save": None, "rank_pattern": {}, "alpha_pattern": {}}


def test_matching_config_has_no_problems():
    assert train.init_adapter_problems(GOOD, A) == []


def test_each_mismatch_is_reported():
    for k, v in (("r", 8), ("lora_alpha", 32), ("lora_dropout", 0.1), ("target_modules", ".*q_proj"), ("use_dora", True),
                 ("use_rslora", True), ("modules_to_save", ["lm_head"]), ("peft_type", "IA3")):
        bad = train.init_adapter_problems({**GOOD, k: v}, A)
        assert len(bad) == 1 and bad[0].startswith(k), (k, bad)
    assert train.init_adapter_problems({**GOOD, "rank_pattern": {"x": 4}}, A)[0].startswith("rank_pattern")


def test_r5_step1800_config_matches_when_present():
    if R5_1800.exists():
        assert train.init_adapter_problems(json.loads(R5_1800.read_text()), A) == []
