"""Tests for plan.py (batch math, advantages, KL, guard, estimate) and for grpo.py's torch-free helpers.
Run: python -m pytest -q test_plan.py   (or python test_plan.py)"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan as P  # noqa: E402


def test_profiles_batch_math():
    a, b = P.PROFILES["a6000"]["plan"].validate(), P.PROFILES["b300x8"]["plan"].validate()
    assert (a.prompts_per_step, a.completions_per_step, a.grad_accum) == (4, 24, 8)
    assert (b.prompts_per_step, b.completions_per_step, b.grad_accum) == (8, 64, 1)
    assert a.steps(1488) == 372 and b.steps(1488) == 186
    assert 1488 % 16 == 0                                   # export_prompts cuts rows to a multiple of 16


def test_validate_rejects_bad_shapes():
    for bad in (P.Plan(1, 1, 4, 1), P.Plan(1, 6, 4, 4), P.Plan(8, 8, 1, 3)):
        try:
            bad.validate()
        except ValueError:
            continue
        raise AssertionError(bad)


def test_rows_for_partitions_every_row_once():
    for plan in (P.PROFILES["a6000"]["plan"], P.PROFILES["b300x8"]["plan"], P.Plan(8, 8, 2, 8)):
        seen = []
        for step in range(5):
            for rank in range(plan.world):
                seen += plan.rows_for(step, rank)
        assert seen == list(range(5 * plan.prompts_per_step))


def test_advantages():
    adv = P.advantages([1.0, 0.0, 0.5, 0.5], 0.1)
    assert abs(sum(adv)) < 1e-9 and adv[0] > adv[2] > adv[1]
    mean, std = 0.5, math.sqrt(sum((r - 0.5) ** 2 for r in (1, 0, .5, .5)) / 3)
    assert abs(adv[0] - 0.5 / std) < 1e-9
    assert P.advantages([1.0, 1.0, 1.0], 0.1) == [0.0, 0.0, 0.0]


def test_std_floor_damps_near_ties():
    tiny = [1.25, 1.24, 1.25, 1.25, 1.26, 1.25]
    assert max(abs(x) for x in P.advantages(tiny, 0.25)) < 0.1          # a WER of 0.01 apart barely moves the policy
    assert max(abs(x) for x in P.advantages(tiny, 1e-6)) > 1.5          # without the floor it would be blown up to +-2
    assert abs(P.advantages([1.5, 0.0, 1.5, 0.0], 0.25)[0] - 0.75 / math.sqrt(0.75)) < 1e-9


def test_kl_k3():
    assert P.kl_k3(-1.0, -1.0) == 0.0
    assert P.kl_k3(-1.0, -2.0) > 0 and P.kl_k3(-2.0, -1.0) > 0
    assert abs(P.kl_k3(-1.0, -1.5) - (math.exp(-0.5) + 0.5 - 1)) < 1e-12


def test_token_loss_pulls_towards_reference():
    # zero advantage: the loss is pure KL and is minimised at the reference
    assert P.token_loss(-1.0, -1.0, 0.0, -1.0, 0.2) == 0.0
    assert P.token_loss(-0.5, -0.5, 0.0, -1.0, 0.2) > 0
    # a stronger beta on multimodal rows costs more for the same drift
    assert P.token_loss(-0.5, -0.5, 0.0, -1.0, 0.2) > P.token_loss(-0.5, -0.5, 0.0, -1.0, 0.04)
    # positive advantage lowers the loss when the token gets more likely (ratio is exp(logp - old))
    assert P.token_loss(-0.4, -0.5, 1.0, -0.5, 0.0) < P.token_loss(-0.5, -0.5, 1.0, -0.5, 0.0)


BASE = {"n_vision": 51, "n_audio": 11, "vision_reward": 1.30, "audio_reward": 1.10, "vision_empty": 1, "vision_banned": 0,
        "vision_invalid": 0, "audio_wrong": 0, "audio_dropped": 0, "audio_banned": 0, "malformed": 0}


def test_guard_passes_small_noise_and_improvement():
    assert P.guard_problems(BASE, dict(BASE)) == []
    assert P.guard_problems(BASE, {**BASE, "vision_reward": 1.28, "audio_reward": 1.2, "vision_empty": 0}) == []


def test_guard_flags_each_regression():
    cases = {"vision_reward": 1.0, "audio_reward": 0.9, "vision_empty": 2, "vision_banned": 1, "vision_invalid": 1,
             "audio_wrong": 1, "audio_dropped": 1, "malformed": 1}
    for k, v in cases.items():
        probs = P.guard_problems(BASE, {**BASE, k: v})
        assert len(probs) == 1 and probs[0].startswith(k), (k, probs)


def test_guard_ignores_a_missing_modality():
    base = {**BASE, "n_audio": 0, "audio_reward": 0.0}
    assert P.guard_problems(base, {**base, "audio_reward": 0.0}) == []


def test_estimate_tables():
    for p in ("a6000", "b300x8"):
        h = [P.hours(p, 1488, 1, c) for c in (0, 1, 2)]
        assert h[0] < h[1] < h[2], h
    a6, b3 = P.hours("a6000", 1488, 1, 1), P.hours("b300x8", 1488, 1, 1)
    assert 8 < a6 < 25 and 0.3 < b3 < 3 and a6 / b3 > 8                # the 8-GPU node is an order of magnitude faster
    text = P.cost_table(1488, 32.0)
    assert "one epoch, all rows" in text and "$" in text
    # the table cost is hours x $32 (setup included)
    t = 186 * P.step_seconds("b300x8", 1)["step"] / 3600 + max(1, 186 // 25) * P.MM_EVAL_H + P.SETUP_H
    assert f"${t * 32:,.0f}" in text


def test_report_runs():
    assert "pessimistic" in P.report(1488, 1, 32.0)


def test_grpo_helpers_import_without_torch():
    import grpo
    keys = grpo.metric_keys()
    assert "n/vision" in keys and "g/audio/audio" in keys and len(set(keys)) == len(keys)
    p = grpo.build_parser().parse_args(["train", "--base", "x", "--prompts", "y", "--out", "z", "--profile", "b300x8"])
    assert p.beta == 0.04 and p.beta_mm > p.beta and p.mm_ref == "adapter" and p.eval_every == 25
    assert grpo.STOP_IDS == [106, 50]


def test_load_wav_checks_format(tmp=None):
    import tempfile
    import wave
    import struct
    import grpo
    try:
        import numpy  # noqa: F401
    except ImportError:
        return
    with tempfile.TemporaryDirectory() as d:
        ok, bad = Path(d) / "ok.wav", Path(d) / "bad.wav"
        for path, rate in ((ok, 16000), (bad, 44100)):
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(struct.pack("<4h", 0, 16384, -16384, 32767))
        x = grpo.load_wav(ok)
        assert x.dtype.name == "float32" and abs(x[1] - 0.5) < 1e-6 and x.shape == (4,)
        try:
            grpo.load_wav(bad)
        except ValueError:
            return
        raise AssertionError("44.1 kHz accepted")


def test_parser_has_the_untuned_floor():
    import grpo
    a = grpo.build_parser().parse_args(["train", "--base", "x", "--prompts", "y", "--out", "z", "--mm-floor", "f.json"])
    assert a.mm_floor == "f.json" and a.allow_sft_regression is False


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(exc)[:300])
    print("failed" if fails else "all passed", fails)
    sys.exit(1 if fails else 0)


def test_grpo_dev_ids_are_disjoint_from_sft_train_on_real_rows():
    """The never-trained tripwire must use the SFT split rule (meta.split first, hash only without it), so no id
    the SFT trains on is also a GRPO dev id. Runs the real vision_rows/audio_rows on the real rows files."""
    import json
    from collections import Counter
    import export_prompts as X
    R = X.load_render()
    ask = Path(__file__).resolve().parent.parent

    class Tok:                                   # just enough template for the audio shape checks
        def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=False, **kw):
            s = "<|turn>user\n<|audio|>" + msgs[0]["content"][1]["text"] + "<turn|>\n<|turn>model\n"
            return s if len(msgs) == 1 else s + msgs[1]["content"] + "<turn|>"

    class RStub:                                 # the real split rules; rendering stubbed (needs a tokenizer)
        FORCED = R.FORCED
        vision_is_dev = staticmethod(R.vision_is_dev)
        audio_is_dev = staticmethod(R.audio_is_dev)
        render_vision = staticmethod(lambda tok, row, rel: {"prompt": "p", "completion": row["target"]})

    checked = 0
    for kind, fn, path, sft_dev in (
            ("vision", X.vision_rows, ask / "vision" / "vision_rows.jsonl", lambda r: R.vision_is_dev(r["id"], r.get("meta"))),
            ("audio", X.audio_rows, ask / "audio" / "audio_rows.jsonl", R.audio_is_dev)):
        if not path.is_file():
            continue
        rows = [json.loads(l) for l in open(path) if l.strip()]
        sft_train = {r["id"] for r in rows if not sft_dev(r)}
        sft_dev_ids = {r["id"] for r in rows if sft_dev(r)}
        train, dev = fn(RStub, Tok(), path, Counter())
        dev_ids, train_ids = {i["id"] for i in dev}, {i["id"] for i in train}
        assert dev_ids, kind
        assert not dev_ids & sft_train, (kind, sorted(dev_ids & sft_train)[:5])
        assert dev_ids <= sft_dev_ids and train_ids <= sft_train, kind
        checked += 1
    assert checked, "no real rows files found"
