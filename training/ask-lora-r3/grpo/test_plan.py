"""Tests for plan.py (batch math, advantages, KL, guard, estimate) and for grpo.py's torch-free helpers.
Run: python -m pytest -q test_plan.py   (or python test_plan.py)"""
import json
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


def test_r4_a6000_settings_and_estimate():
    c = P.R4_A6000
    assert (c["G"], c["beta"], c["lr"], c["epochs"], c["max_completion"]) == (6, 0.04, 5e-6, 1, 512)
    assert 1400 <= c["prompts"] <= 1600 and c["prompts"] % 16 == 0
    plan = P.PROFILES[c["profile"]]["plan"].validate()
    assert (plan.G, plan.ppr, plan.micro_bs, P.PROFILES["a6000"]["lr"]) == (c["G"], c["ppr"], c["micro_bs"], c["lr"])
    est = P.a6000_r4_estimate()
    assert est["steps"] == plan.steps(c["prompts"]) == 372 and est["completions_per_step"] == 24
    assert est["optimistic"]["total_h"] < est["middle"]["total_h"] < est["pessimistic"]["total_h"]
    # longer round-4 completions cost more than round 3's, but the same order of magnitude (one epoch: tens of hours)
    r3 = P.hours("a6000", c["prompts"], 1, 1)
    r4 = P.hours("a6000", c["prompts"], 1, 1, rnd=4)
    assert r3 < r4 < 2 * r3
    assert 10 < est["optimistic"]["train_h"] and est["pessimistic"]["total_h"] < 40


def test_r4_changes_nothing_in_round_3_estimates():
    assert P.step_seconds("a6000", 1)["step"] == P.step_seconds("a6000", 1, 3)["step"]
    assert P.step_seconds("b300x8", 0, 4)["step"] > P.step_seconds("b300x8", 0, 3)["step"]
    assert "Round 4 GRPO on one" in P.report(1488, 1, 32.0, 4) and "Round 4 GRPO" not in P.report(1488, 1, 32.0)
    assert "beta 0.04" in P.report_r4() and "1488 prompts" in P.report_r4()


def test_grpo_round_switch(tmp=None):
    import json
    import tempfile
    import grpo
    import reward as W
    old = W.ROUND

    def run(rounds, flag=0):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.jsonl"
            f.write_text("".join(json.dumps({"id": str(i), "prompt": "x", "label": json.dumps({"round": r} if r else {})}) + "\n"
                                 for i, r in enumerate(rounds)))
            a = grpo.build_parser().parse_args(["check", "--base", "x", "--prompts", str(f), "--round", str(flag)])
            return grpo.resolve_round(a), a.max_completion
    try:
        W.ROUND = 3
        assert run([0, 0]) == (3, 384)                       # unlabelled rows: round 3, the old cap
        assert run([4, 4]) == (4, 512) and W.ROUND == 4      # labelled by export_prompts.py --round 4
        W.ROUND = 3
        assert run([0, 0], flag=4) == (4, 512)               # the flag alone
        for bad in (lambda: run([4, 4], flag=3), lambda: run([4, 0])):
            try:
                bad()
            except SystemExit:
                continue
            raise AssertionError("a round mix-up must exit")
    finally:
        W.ROUND = old


# ====================================================================== round 5: trainer helpers (RL-SCOPE C1 grpo side, C10, C13, C22)

def _args(**kw):
    import grpo
    base = ["train", "--base", "x", "--prompts", "y", "--out", "z"]
    extra = []
    for k, v in kw.items():
        extra += [f"--{k.replace('_', '-')}"] + ([] if v is True else v if isinstance(v, list) else [str(v)])
    return grpo.build_parser().parse_args(base + extra)


def test_round_flag_accepts_5_and_defaults_follow_the_round():
    import json
    import tempfile
    import grpo
    import reward as W
    old = W.ROUND
    W.ROUND = 3                    # the module-wide switch may have been left at another round by an earlier test file
    assert _args(round=5).round == 5
    try:
        for rnd, cap, pat, margin in ((3, 384, 2, 0.05), (4, 512, 2, 0.05), (5, 1024, 1, 0.03)):
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "p.jsonl"
                f.write_text(json.dumps({"id": "1", "prompt": "x", "label": json.dumps({"round": rnd} if rnd > 3 else {})}) + "\n")
                a = grpo.build_parser().parse_args(["check", "--base", "x", "--prompts", str(f)])
                assert grpo.resolve_round(a) == rnd and (a.max_completion, a.guard_patience, a.guard_margin) == (cap, pat, margin)
        a = _args(round=5, guard_patience=2, guard_margin=0.04, max_completion=700)
        grpo.resolve_round(a)
        assert (a.max_completion, a.guard_patience, a.guard_margin) == (700, 2, 0.04)           # explicit flags win
    finally:
        W.ROUND = old


def _write_rows(path, rounds, **row_extra):
    import json
    path.write_text("".join(json.dumps({"id": str(i), "prompt": "x", "n_samples": 2, "label": json.dumps({"round": r, "modality": "text", "rl_kind": "narrate"} if r else {})}) + "\n"
                            for i, r in enumerate(rounds)))


def test_resolve_round_reads_prompts_and_text_dev_and_rejects_unstamped_mixed_or_disagreeing():
    import tempfile
    import grpo
    import reward as W
    old = W.ROUND
    W.ROUND = 3

    def run(prompts, dev, flag=0):
        with tempfile.TemporaryDirectory() as d:
            pf, df = Path(d) / "p.jsonl", Path(d) / "d.jsonl"
            args = ["check", "--base", "x", "--round", str(flag)]
            if prompts is not None:
                _write_rows(pf, prompts)
                args += ["--prompts", str(pf)]
            if dev is not None:
                _write_rows(df, dev)
                args += ["--text-dev", str(df)]
            return grpo.resolve_round(grpo.build_parser().parse_args(args))
    try:
        assert run([5, 5], [5, 5]) == 5 and run(None, [5]) == 5 and run([5], [5], flag=5) == 5      # text-eval has no prompts file
        for bad in (lambda: run([5], [0]), lambda: run([5], [5, 0]), lambda: run(None, [0]), lambda: run([5], [4]),
                    lambda: run([0], [5]), lambda: run(None, [5], flag=4), lambda: run([5, 0], [5])):
            try:
                bad()
            except SystemExit:
                continue
            raise AssertionError("a round mix-up between --prompts, --text-dev and --round must exit")
    finally:
        W.ROUND = old


def test_check_train_args_requires_the_floor_before_torch_loads():
    import grpo
    a = _args()
    assert P.train_arg_problems(a, dev_rows_exist=True)[0].startswith("--mm-floor is required")
    assert P.train_arg_problems(a, dev_rows_exist=False) == []                       # no multimodal dev rows: nothing to floor
    assert P.train_arg_problems(_args(no_guard=True), True) == []
    assert P.train_arg_problems(_args(mm_floor="f.json"), True) == []
    assert P.train_arg_problems(_args(dry_run_grad=True), True) == []
    try:
        grpo.check_train_args(a, True, rnd=3)
    except SystemExit as exc:
        assert "--mm-floor" in str(exc)
    else:
        raise AssertionError("check_train_args must exit")
    grpo.check_train_args(_args(mm_floor="f.json"), True, rnd=3)
    # round 5 also needs its text-probe files, and a floor that carries the C13 counts
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "floor.json"
        good.write_text(json.dumps({"vision_reward": 1.0, "proposals_right": 3.0, "proposals_spurious": 0.0, "vision_facts_total": 9.0}))
        old_floor = Path(d) / "old.json"
        old_floor.write_text(json.dumps({"vision_reward": 1.0}))
        g, o = str(good), str(old_floor)
        probs = P.train_arg_problems(_args(mm_floor=g), True, rnd=5)
        assert len(probs) == 1 and "--text-dev" in probs[0]
        probs = P.train_arg_problems(_args(mm_floor=g, text_dev="d.jsonl"), True, rnd=5)
        assert len(probs) == 2 and any("--text-floor" in p for p in probs) and any("--text-replicates" in p for p in probs)
        assert any("at least two" in p for p in P.train_arg_problems(_args(mm_floor=g, text_dev="d", text_floor="b", text_replicates=["r1"]), True, rnd=5))
        assert P.train_arg_problems(_args(mm_floor=g, text_dev="d", text_floor="b", text_replicates=["r1", "r2"]), True, rnd=5) == []
        assert P.train_arg_problems(_args(mm_floor=g, no_text_probes=True), True, rnd=5) == []
        # a floor from before C13 is an error in round 5 (and in the launch check, before any GPU work), fine in round 4
        probs = P.train_arg_problems(_args(mm_floor=o, no_text_probes=True), True, rnd=5)
        assert len(probs) == 1 and "proposals_right" in probs[0] and "re-run" in probs[0]
        assert P.train_arg_problems(_args(mm_floor=o), True, rnd=4) == []
        assert any("not readable" in p for p in P.train_arg_problems(_args(mm_floor=str(Path(d) / "none.json"), no_text_probes=True), True, rnd=5))
    assert not P.train_arg_problems(_args(), False, rnd=4)                           # rounds 3 and 4 never need the text files


def test_main_checks_the_arguments_before_cmd_train():
    import tempfile
    import grpo
    import reward as W
    old_round, W.ROUND = W.ROUND, 3
    with tempfile.TemporaryDirectory() as d:
        pf, mf = Path(d) / "p.jsonl", Path(d) / "grpo_mm_dev.jsonl"
        pf.write_text("")
        mf.write_text('{"id": "v", "label": "{}"}\n')
        called = []
        old = grpo.cmd_train
        grpo.cmd_train = lambda a: called.append(a) or 0
        old_argv = sys.argv
        try:
            sys.argv = ["grpo.py", "train", "--base", "x", "--prompts", str(pf), "--out", str(Path(d) / "o")]
            try:
                grpo.main()
            except SystemExit as exc:
                assert "--mm-floor" in str(exc) and not called
            else:
                raise AssertionError("main() must exit before cmd_train")
            sys.argv += ["--mm-floor", "f.json"]
            assert grpo.main() == 0 and called
        finally:
            grpo.cmd_train, sys.argv, W.ROUND = old, old_argv, old_round


def test_small_n_counts_one_row_tolerated_two_trip_repeat_trips():
    base = {"proposals_right": 20.0, "proposals_spurious": 3.0, "vision_facts_total": 40.0}
    ok = P.small_n_problems(base, dict(base))
    assert ok == ([], set())
    # one row the wrong way: tolerated on a single eval, remembered
    probs, soft = P.small_n_problems(base, {**base, "proposals_right": 19.0})
    assert probs == [] and soft == {"proposals_right"}
    # ... and the same breach at the next cadence checkpoint trips
    probs, soft = P.small_n_problems(base, {**base, "proposals_right": 19.0}, soft)
    assert len(probs) == 1 and "two consecutive" in probs[0]
    # a recovery in between clears the memory
    assert P.small_n_problems(base, dict(base), {"proposals_right"})[1] == set()
    # two rows trip at once, in the right direction for each key
    assert P.small_n_problems(base, {**base, "proposals_right": 18.0})[0]
    assert P.small_n_problems(base, {**base, "proposals_spurious": 5.0})[0]
    assert P.small_n_problems(base, {**base, "vision_facts_total": 38.0})[0]
    assert P.small_n_problems(base, {**base, "proposals_right": 25.0, "proposals_spurious": 0.0, "vision_facts_total": 45.0}) == ([], set())
    assert P.small_n_problems(base, {**base, "proposals_spurious": 4.0})[1] == {"proposals_spurious"}     # spurious UP is bad, by 1 row
    # a baseline from before C13 has no keys: skipped
    assert P.small_n_problems({"vision_reward": 1.0}, base) == ([], set())


def test_small_n_floor_is_zero_tolerance_and_old_floor_files_are_skipped():
    floor = {"proposals_right": 20.0, "proposals_spurious": 3.0, "vision_facts_total": 40.0}
    assert P.small_n_floor_problems(floor, dict(floor)) == []
    assert len(P.small_n_floor_problems(floor, {**floor, "proposals_right": 19.0})) == 1       # no 1-row slack against the untuned floor
    assert len(P.small_n_floor_problems(floor, {**floor, "proposals_spurious": 4.0})) == 1
    assert P.small_n_floor_problems({"vision_reward": 1.0}, floor) == []


def test_guard_problems_unchanged_by_the_new_counts():
    # the existing zero-tolerance counts and means behave exactly as before; new keys in `now` are ignored by guard_problems
    assert P.guard_problems(BASE, {**BASE, "proposals_right": 0.0, "vision_facts_total": 0.0}) == []


def test_mm_small_n_counts_per_vision_row():
    due = {"modality": "vision", "proposal": {"title": "x"}}
    assert P.mm_small_n(due, {"v_proposal": 0.5, "v_facts": 0.75}) == {"proposals_right": 1.0, "proposals_spurious": 0.0, "vision_facts_total": 0.75}
    assert P.mm_small_n(due, {"v_proposal": 0.25, "v_facts": 1.0})["proposals_right"] == 0.0                # some fields wrong
    assert P.mm_small_n(due, {"v_proposal": -1.0})["proposals_spurious"] == 0.0                              # a missing due proposal is not spurious
    none_due = {"modality": "vision", "proposal": None}
    assert P.mm_small_n(none_due, {"v_proposal": -1.0})["proposals_spurious"] == 1.0
    assert P.mm_small_n(none_due, {"v_proposal": 0.5})["proposals_spurious"] == 0.0
    assert P.mm_small_n({"modality": "vision", "task": "picture_read"}, {"v_facts": 1.0}) == {"proposals_right": 0.0, "proposals_spurious": 0.0, "vision_facts_total": 0.0}
    assert P.mm_small_n({"modality": "audio"}, {"v_facts": 1.0})["vision_facts_total"] == 0.0


def test_the_ideal_trip_bar_is_an_exact_one_percent_per_rl_kind():
    """RL-SCOPE: at most 1% of the rows of any rl_kind. The old max(1, n // 100) let 1 row in 14 (7%) through."""
    assert P.kind_trip_problems({"recall_none": 1}, {"recall_none": 14}) == {"recall_none": "1/14"}
    assert P.kind_trip_problems({"narrate": 1}, {"narrate": 100}) == {}                    # exactly 1%
    assert P.kind_trip_problems({"narrate": 2}, {"narrate": 100}) == {"narrate": "2/100"}
    assert P.kind_trip_problems({"narrate": 14}, {"narrate": 1479}) == {}                  # 0.95%
    assert P.kind_trip_problems({"narrate": 15}, {"narrate": 1479}) == {"narrate": "15/1479"}
    assert P.kind_trip_problems({}, {"narrate": 10}) == {} and P.kind_trip_problems({"decision": 0}, {"decision": 3}) == {}


def test_ideal_top_is_the_old_table_in_rounds_3_and_4_and_the_ceiling_in_round_5():
    for lab, r3, r4 in (({"modality": "vision"}, 1.5, 1.5), ({"modality": "audio"}, 1.25, 1.25), ({"step": "call"}, 1.5, 1.5),
                        ({"step": "load"}, 1.5, 1.5), ({"step": "final"}, 1.0, 1.0),
                        ({"step": "final", "narrate": True, "facts": ["x"]}, 1.0, 2.0), ({"step": "final", "narrate": True, "facts": []}, 1.0, 1.0)):
        assert P.ideal_top(lab, 3) == (r3, 0.31) and P.ideal_top(lab, 4) == (r4, 0.31), lab
    assert P.ideal_top({"step": "final", "ceiling": 2.0, "modality": "text"}, 5) == (2.0, 0.01)
    assert P.ideal_top({"step": "final", "ceiling": 1.5, "narrate": True, "facts": ["x"]}, 5) == (1.5, 0.01)
    assert P.ideal_top({"step": "call", "ceiling": 1.5, "modality": "text"}, 5) == (1.5, 0.01)         # a call ideal must reach its ceiling too
    assert P.ideal_top({"modality": "vision", "ceiling": None, "step": "mm_vision"}, 5) == (1.5, 0.31)      # mm rows keep today's check
    try:
        P.ideal_top({"step": "final", "modality": "text"}, 5)
    except ValueError:
        pass
    else:
        raise AssertionError("a round-5 text row without a ceiling")


def test_truncated_is_decided_from_the_token_list():
    assert P.truncated([5, 6, 7], [106, 50]) and not P.truncated([5, 6, 106], [106, 50]) and P.truncated([], [106])
    assert not P.truncated([1], [106, 50, 1]) and P.truncated([1], [106, 50])
    assert not P.truncated([7] * 1023 + [106], [106, 50])           # a stop id as the last allowed token is not truncation


def test_group_flags_and_bucket_log():
    # all-pass zero variance at the ceiling, all-fail zero variance below it, informative = some but not all fire a verifier penalty
    a = P.group_flags([1.5] * 4, [{}] * 4, 1.5, [False] * 4, [0.0] * 4)
    assert (a["zv"], a["zv_pass"], a["zv_fail"], a["informative"]) == (1, 1, 0, 0)
    b = P.group_flags([0.0] * 4, [{"forbidden": -2.0}] * 4, 1.5, [False] * 4, [0.0] * 4)
    assert (b["zv"], b["zv_pass"], b["zv_fail"], b["informative"]) == (1, 0, 1, 0)          # all fire: not informative
    c = P.group_flags([1.5, 1.5, -0.5, 1.5], [{}, {}, {"early_write": -2.0}, {}], 1.5, [False, False, True, False], [0.4, 0.4, -1.2, 0.4])
    assert (c["zv"], c["informative"], c["trunc"]) == (0, 1, 1) and abs(c["abs_adv"] - 2.4) < 1e-9
    d = P.group_flags([1.0, 0.8, 1.0, 1.0], [{"args": -0.2}] * 4, 1.0, [False] * 4, [0.1] * 4)
    assert d["informative"] == 0                                   # reward variance from args noise alone is not a verifier penalty
    tot = {k: 0.0 for k in P.bucket_keys()}
    for flags, bucket in ((a, "narrate"), (b, "narrate"), (c, "narrate"), (d, "call")):
        for k, v in flags.items():
            tot[f"{bucket}/{k}"] += v
    log = P.bucket_log(tot)
    assert set(log) == {"narrate", "call"} and log["narrate"]["groups"] == 3
    assert abs(log["narrate"]["zv_share"] - 2 / 3) < 1e-3 and log["narrate"]["zv_pass"] == round(1 / 3, 4) and log["narrate"]["informative_share"] == round(1 / 3, 4)
    assert log["narrate"]["trunc_share"] == round(1 / 12, 4)
    tot["narrate/pg_norm"], tot["narrate/kl_norm"], tot["narrate/norm_n"] = 6.0, 2.0, 2.0
    assert P.bucket_log(tot)["narrate"]["pg_kl_ratio"] == 3.0
    assert P.bucket_of({"rl_kind": "recall"}) == "recall" and P.bucket_of({}) == "text" and P.bucket_of({"rl_kind": "recall"}, "vision") == "mm"
    assert len(set(P.bucket_keys())) == len(P.bucket_keys())


def test_s_trunc_needs_a_full_window_and_more_than_5_percent():
    assert P.trunc_problem([(4, 24, 24)]) is None                  # 4 prompts seen: no window yet
    ok = [(4, 0, 24)] * 10
    assert P.trunc_problem(ok) is None
    bad = [(4, 0, 24)] * 8 + [(4, 8, 24)] * 2                       # 16 of 240 = 6.7 %
    assert "6.7%" in P.trunc_problem(bad)
    edge = [(4, 0, 24)] * 9 + [(4, 12, 24)]                          # 12 of 240 = exactly 5 %: not above
    assert P.trunc_problem(edge) is None
    node = [(8, 0, 64)] * 4 + [(8, 17, 64)]                          # the node's window is 5 steps of 8 prompts
    assert P.trunc_problem(node) is not None


def test_adjusted_reward_ignores_which_prompts_were_drawn():
    w = {"call": 0.5, "narrate": 0.5}
    easy = [{"kind": "call", "ceiling": 1.5, "reward": 1.5}] * 9 + [{"kind": "narrate", "ceiling": 2.0, "reward": 1.0}]
    mixed = [{"kind": "call", "ceiling": 1.5, "reward": 1.5}] + [{"kind": "narrate", "ceiling": 2.0, "reward": 1.0}] * 9
    assert abs(P.adjusted_reward(easy, w) - P.adjusted_reward(mixed, w)) < 1e-9 and abs(P.adjusted_reward(easy, w) - 0.75) < 1e-9
    assert P.adjusted_reward([{"kind": "mm", "ceiling": None, "reward": 1.0}], w) is None
    assert P.adjusted_reward([{"kind": "other", "ceiling": 1.0, "reward": 1.0}], w) is None                  # a kind with no weight


def test_s_learn_and_s_grader_rules():
    # both conditions needed: no rise AND a worse zero-variance picture
    assert P.s_learn_hard(0.60, 0.61, 0.05, zv_fail=0.50, zv0_fail=0.30, zv_total=0.7, zv0_total=0.6)
    assert not P.s_learn_hard(0.60, 0.70, 0.05, 0.50, 0.30, 0.7, 0.6)                                       # reward rose
    assert not P.s_learn_hard(0.60, 0.61, 0.05, 0.40, 0.30, 0.65, 0.6)                                       # within 15 points: saturation / noise
    assert P.s_learn_hard(0.60, 0.61, 0.05, 0.20, 0.30, 0.80, 0.6)                                           # total zero-variance share up 20 points
    assert not P.s_learn_hard(None, 0.6, 0.05, 1, 0, 1, 0)
    assert P.s_grader_trips(0.9, 0.7, 0.1, facts_gain=0.0, facts_floor=0.01, recall_gain_points=0.0, unsafe_now=2, unsafe_m0=1)
    assert not P.s_grader_trips(0.9, 0.7, 0.1, 0.05, 0.01, 0.0, 2, 1)                                        # facts really improved
    assert not P.s_grader_trips(0.9, 0.7, 0.1, 0.0, 0.01, 0.0, 0, 1)                                         # unsafe below m0
    assert not P.s_grader_trips(0.71, 0.7, 0.1, 0.0, 0.01, 0.0, 2, 1)                                        # train reward not rising


def _adapter_names(n_layers=42, kv_layers=24, drop=(), extra=()):
    names = []
    for i in range(n_layers):
        for proj, where in (("q_proj", "self_attn"), ("k_proj", "self_attn"), ("v_proj", "self_attn"), ("o_proj", "self_attn"),
                            ("gate_proj", "mlp"), ("up_proj", "mlp"), ("down_proj", "mlp")):
            if proj in ("k_proj", "v_proj") and i >= kv_layers:
                continue
            if (i, proj) in drop:
                continue
            names.append(f"base_model.model.model.language_model.layers.{i}.{where}.{proj}.lora_A")
    return names + list(extra)


def test_lora_layout_counts_kv_adapters_per_layer_and_flags_dead_modules():
    ok = P.lora_layout(_adapter_names(), 42, kv_first=24)
    assert not ok["problems"] and "k_proj adapters on 24/42" in ok["summary"] and ok["kv_layers"]["k_proj"][-1] == 23
    assert ok["per_layer"][0] == sorted(["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]) and "k_proj" not in ok["per_layer"][30]
    assert not P.lora_layout(_adapter_names(), 42, kv_first=None)["problems"]                  # no config: nothing to hold the k/v layers against
    # an adapter on a KV-shared layer could never get a gradient (DDP find_unused_parameters=False would stop on it)
    shared = P.lora_layout(_adapter_names(kv_layers=30), 42, kv_first=24)
    assert any("KV-shared layers" in p for p in shared["problems"])
    # q_proj missing on one layer; stray adapter outside the language model; no k/v at all
    assert any("q_proj adapters on 41 of 42" in p for p in P.lora_layout(_adapter_names(drop={(7, "q_proj")}), 42)["problems"])
    assert any("outside" in p for p in P.lora_layout(_adapter_names(extra=["base_model.model.model.vision_tower.layers.0.self_attn.q_proj.lora_A"]), 42)["problems"])
    assert any("no k_proj/v_proj" in p for p in P.lora_layout(_adapter_names(kv_layers=0), 42)["problems"])
    assert P.lora_layout(_adapter_names(drop={(3, "v_proj")}), 42, kv_first=24)["warnings"]     # k without its v: a warning (a k=v layer), not a stop
    assert P.kv_shared_first({"text_config": {"num_hidden_layers": 42, "num_kv_shared_layers": 18}}) == 24
    assert P.kv_shared_first({"num_hidden_layers": 42}) is None and P.kv_shared_first({}) is None


def test_grad_problems_after_one_backward():
    def mod(name, a=(True, 0.0), b=(True, 1.0)):
        return {f"{name}.lora_A.default.weight": {"grad": a[0], "norm": a[1], "finite": True},
                f"{name}.lora_B.default.weight": {"grad": b[0], "norm": b[1], "finite": True}}
    good = {**mod("m.layers.0.self_attn.q_proj"), **mod("m.layers.0.mlp.up_proj")}
    assert P.grad_problems(good) == []                              # lora_A's gradient is exactly zero at init (B = 0): fine
    dead = {**good, **mod("m.layers.30.self_attn.k_proj", a=(False, 0.0), b=(False, 0.0))}
    assert len(P.grad_problems(dead)) == 2 and all("no gradient" in p for p in P.grad_problems(dead))
    zero_b = {**good, **mod("m.layers.9.mlp.down_proj", b=(True, 0.0))}
    assert any("lora_B gradient is exactly zero" in p for p in P.grad_problems(zero_b))
    nan = {**good, **mod("m.layers.5.self_attn.o_proj")}
    nan["m.layers.5.self_attn.o_proj.lora_B.default.weight"]["finite"] = False
    assert any("non-finite" in p for p in P.grad_problems(nan))
    assert any("missing its lora_B" in p for p in P.grad_problems({"m.q_proj.lora_A.default.weight": {"grad": True, "norm": 0.0, "finite": True}}))


def _probe_file(path, rows, rnd=5, sha=None, extras=None):
    import json
    import probes as PR
    f = PR.make_probe_file("text-eval", rnd, rows, extras or {})
    if sha:
        f["dev_sha256"] = sha
    path.write_text(json.dumps(f))


def test_text_probe_inputs_full_dev_must_match_and_subset_is_row_keyed():
    import json
    import tempfile
    import random
    import grpo
    import probes as PR
    import test_probes as T
    rows = T.synthetic_rows(random.Random(1))
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        lines = []
        for r in rows:
            lab = {"modality": "text", "round": 5, "rl_kind": r["rl_kind"] or "decision", "ceiling": 1.0, "slice": r["slice"], "post_read": r["post_read"]}
            lines.append(json.dumps({"id": r["id"], "family": r["family"], "prompt": "p", "label": json.dumps(lab), "n_samples": r["n"]}))
        full = d / "dev.jsonl"
        full.write_text("\n".join(lines) + "\n")
        sha = PR.dev_sha256(full)
        _probe_file(d / "base.json", rows, sha=sha)
        for i in (1, 2):
            _probe_file(d / f"r{i}.json", rows, sha=sha)

        def args(dev, **kw):
            return _args(text_dev=str(dev), text_floor=str(d / "base.json"), text_replicates=[str(d / "r1.json"), str(d / "r2.json")], **kw)
        got = grpo.text_probe_inputs(args(full), 5)
        assert not got["single"] and len(got["dev"]) == 166 and got["dev_sha256"] == sha and len(got["reps_rows"]) == 2
        # the smoke30 subset: a smaller dev file is matched row by row, the first n samples of each row
        sub = d / "smoke.jsonl"
        sub_lines = [l for l, r in zip(lines, rows) if r["id"] in {"u1", "u2", "n1", "r1"}]
        sub_lines = [json.dumps({**json.loads(l), "n_samples": 2 if json.loads(l)["id"] == "u1" else json.loads(l)["n_samples"]}) for l in sub_lines]
        sub.write_text("\n".join(sub_lines) + "\n")
        s = grpo.text_probe_inputs(args(sub), 5)
        assert s["single"] and [len(r["samples"]) for r in s["reps_rows"][0]][0] == 2 and len(s["reps_rows"][0]) == 4
        # a full-size dev file with a wrong sample count, a file of another round, a replicate scored on another dev file
        bad = d / "bad.jsonl"
        bad.write_text(full.read_text().replace('"n_samples": 4', '"n_samples": 3', 1))
        for dev, rnd in ((bad, 5), (full, 4)):
            try:
                grpo.text_probe_inputs(args(dev), rnd)
            except SystemExit:
                continue
            raise AssertionError((dev, rnd))
        _probe_file(d / "r2.json", rows, sha="deadbeef")
        try:
            grpo.text_probe_inputs(args(full), 5)
        except SystemExit as exc:
            assert "different dev file" in str(exc)
        else:
            raise AssertionError("a replicate from another dev file")


def test_the_trainer_leaves_rounds_3_and_4_alone():
    """What the trainer shows rounds 3 and 4: the same metric keys, the same eval_mm keys in the same order, the old guard rules."""
    import grpo
    keys = grpo.metric_keys()
    assert keys[0] == "n/text" and "g/text/selection" in keys and not any(k.startswith("narrate/") for k in keys)
    assert grpo.STOP_IDS[:2] == [106, 50]
    assert P.bucket_of({"rl_kind": "call"}, "text") == "call"
    for lab in ({}, {"round": 4}):
        assert P.bucket_of(lab, "text") == "text"                   # no rl_kind on r3/r4 exports: one 'text' bucket, as before
    a3 = grpo.build_parser().parse_args(["train", "--base", "x", "--prompts", "y", "--out", "z"])
    assert (a3.beta, a3.beta_mm, a3.adv_floor, a3.adv_floor_mm, a3.eval_every, a3.save_every, a3.max_grad_norm, a3.alpha) == (0.04, 0.2, 0.1, 0.25, 25, 25, 0.3, 32)
    assert a3.text_dev is None and a3.dry_run_grad is False and a3.grad_ratio_every == 0 and a3.no_text_probes is False


def test_run_text_probe_keys_the_draws_per_row_and_eval_seed_whatever_the_sharding():
    """run_text_probe with stubbed torch/accelerate: each row seeds torch with probes.row_seed(id, eval_seed), asks for its own
    per-row sample count, the training RNG is forked around the probe, and 2 ranks reproduce the 1-rank result in dev-file order."""
    import contextlib
    import types
    import grpo
    import probes as PR
    seeds, forked = [], []

    @contextlib.contextmanager
    def fork_rng(devices=None):
        forked.append(devices)
        yield
    torch = types.ModuleType("torch")
    torch.random = types.SimpleNamespace(fork_rng=fork_rng)
    torch.manual_seed = lambda s: seeds.append(s)
    torch.no_grad = contextlib.nullcontext
    torch.cuda = types.SimpleNamespace(current_device=lambda: 0)
    acc_utils = types.ModuleType("accelerate.utils")
    state = {"other": []}
    acc_utils.gather_object = lambda mine: list(mine) + state["other"]
    accelerate = types.ModuleType("accelerate")
    accelerate.utils = acc_utils
    saved = {k: sys.modules.get(k) for k in ("torch", "accelerate", "accelerate.utils")}
    sys.modules.update({"torch": torch, "accelerate": accelerate, "accelerate.utils": acc_utils})
    calls = []
    orig = (grpo.rollout, grpo.build_inputs, grpo.decode_all)
    grpo.build_inputs = lambda row, tok, proc, root: {"id": row["id"]}
    grpo.rollout = lambda model, tok, inp, G, max_new, sample, device, dtype: (calls.append((inp["id"], G, max_new)) or
                                                                                 [[seeds[-1] % 900 + j, 106] for j in range(G)])
    grpo.decode_all = lambda tok, comps: [f"answer {c[0]}<turn|>" for c in comps]
    lab = {"step": "final", "forced": False, "round": 5, "final": True, "facts": [], "expected": [], "forbidden": [], "user_name": "Sam",
           "rl_kind": "narrate", "ceiling": 1.0, "modality": "text"}
    dev = [{"id": f"r{i}", "family": "f", "row": {"id": f"r{i}", "prompt": "p"}, "lab": dict(lab, max_tokens=300 if i == 1 else None),
            "slice": "narrate", "n": 4 if i % 2 == 0 else 2} for i in range(5)]
    model = types.SimpleNamespace(training=True, eval=lambda: None, train=lambda: None)
    dev_obj = types.SimpleNamespace(type="cpu", index=None)

    def run(rank, world, eval_seed):
        acc = types.SimpleNamespace(process_index=rank, num_processes=world)
        return grpo.run_text_probe(acc, model, None, dev, {"temperature": 1.0, "top_p": 0.95, "top_k": 64}, eval_seed, 1024, dev_obj, None)[0]
    try:
        solo = run(0, 1, 77)
        assert [r["id"] for r in solo] == [f"r{i}" for i in range(5)] and [len(r["samples"]) for r in solo] == [4, 2, 4, 2, 4]
        assert seeds[:5] == [PR.row_seed(f"r{i}", 77) for i in range(5)] and [c[1] for c in calls] == [4, 2, 4, 2, 4]
        assert [c[2] for c in calls] == [1024, 300, 1024, 1024, 1024]                  # the row's own label.max_tokens wins
        assert forked and model.training is True
        # two ranks: rank 1 decodes the odd rows, rank 0 the even ones and gathers; same records in dev-file order
        state["other"] = run(1, 2, 77)
        both = run(0, 2, 77)
        assert [r["id"] for r in both] == [r["id"] for r in solo]
        assert [[s["text"] for s in r["samples"]] for r in both] == [[s["text"] for s in r["samples"]] for r in solo]
        # another eval seed gives other draws; the record carries the scored features
        other = run(0, 1, 78)
        state["other"] = []
        assert [s["text"] for s in other[0]["samples"]] != [s["text"] for s in solo[0]["samples"]]
        s0 = solo[0]["samples"][0]
        assert s0["truncated"] is False and s0["tokens"] == 2 and s0["reply_chars"] > 0 and solo[0]["slice"] == "narrate"
    finally:
        grpo.rollout, grpo.build_inputs, grpo.decode_all = orig
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ---- verifier findings: windows, per-class logs, layout gaps, floors

def test_mm_counters_are_split_by_modality_and_k3_is_logged_per_bucket():
    assert P.stat_bucket_of({"kind": "image"}, "vision") == "mm_vision" and P.stat_bucket_of({"kind": "picture_read"}, "vision") == "mm_picture_read"
    assert P.stat_bucket_of({"task": "picture_read"}, "vision") == "mm_picture_read" and P.stat_bucket_of({}, "audio") == "mm_audio"
    assert P.stat_bucket_of({"rl_kind": "call"}, "text") == "call" and P.stat_bucket_of({}, "text") == "text"
    assert P.bucket_of({"kind": "picture_read"}, "vision") == "mm"                              # the per-prompt log row keeps one 'mm' kind
    keys = P.bucket_keys()
    assert len(set(keys)) == len(keys) and "mm_vision/abs_adv" in keys and "mm_audio/kl_sum" in keys and "mm/groups" not in keys
    tot = {k: 0.0 for k in keys}
    for b, adv in (("mm_vision", 0.1), ("mm_audio", 0.5), ("mm_picture_read", 0.9)):
        tot.update({f"{b}/groups": 1, f"{b}/samples": 4, f"{b}/abs_adv": adv * 4, f"{b}/adv_n": 4, f"{b}/kl_sum": 2.0, f"{b}/tok": 400.0})
    tot.update({"narrate/groups": 2, "narrate/samples": 8, "narrate/adv_n": 8, "narrate/kl_sum": 0.8, "narrate/tok": 200.0})
    log = P.bucket_log(tot)
    assert [log[b]["mean_abs_adv"] for b in ("mm_vision", "mm_audio", "mm_picture_read")] == [0.1, 0.5, 0.9]
    assert log["narrate"]["k3"] == 0.004 and log["mm_audio"]["k3"] == 0.005 and "mm" not in log


def test_the_ratio_steps_default_to_1_5_20_and_the_budget_flag_is_off_by_default():
    import grpo
    a = grpo.build_parser().parse_args(["train", "--base", "x", "--prompts", "y", "--out", "z"])
    assert P.parse_steps(a.grad_ratio_steps) == {1, 5, 20} and a.grad_ratio_every == 0 and a.allow_unmet_budget is False
    assert P.parse_steps("") == set() and P.parse_steps(None) == set() and P.parse_steps(" 3, 7 ") == {3, 7}


def test_window_steps_are_wholly_inside_the_prompt_window():
    for ppst, want in ((8, (38, 50)), (4, (75, 100)), (12, (25, 33)), (100, (3, 4)), (7, (43, 57))):
        first, last = P.window_steps(300, 400, ppst)
        assert (first, last) == want, ppst
        assert first * ppst >= 300 and last * ppst <= 400          # every step in [first, last) lies inside prompts 301-400
    assert P.window_steps(0, 100, 8) == (0, 12)


def test_weighted_zero_variance_uses_the_export_mix_not_the_pooled_groups():
    hist = [{"narrate": (8, 4, 6), "select": (1, 0, 0)}, {"narrate": (8, 4, 6), "select": (1, 0, 0)}]
    w = {"narrate": 0.5, "select": 0.5}
    fail, total = P.weighted_zv(hist, w)
    assert abs(fail - 0.25) < 1e-9 and abs(total - 0.375) < 1e-9
    pooled_fail = 8 / 18                                            # what the unweighted pooled share would have said
    assert abs(fail - pooled_fail) > 0.1
    assert P.weighted_zv([{"narrate": (0, 0, 0)}], w) == (0.0, 0.0) and P.weighted_zv([], w) == (0.0, 0.0)
    f2, _ = P.weighted_zv([{"narrate": (4, 2, 2)}], {"narrate": 0.3, "select": 0.7})           # an absent bucket renormalises
    assert abs(f2 - 0.5) < 1e-9


def test_the_s_learn_windows_index_prompts_whatever_the_modality_mix():
    """prompt_recs holds every prompt: records 0..99 are prompts 1-100 and 300..399 are 301-400, mm rows included (no ceiling, so
    adjusted_reward skips them). With ~15% multimodal prompts the old text-only list made [300:400] the wrong prompts."""
    w = {"call": 1.0}
    recs = []
    for i in range(600):
        mm = i % 7 == 0
        recs.append({"kind": "mm" if mm else "call", "ceiling": None if mm else 1.0, "reward": 0.0 if mm else (0.2 if i < 100 else 0.9 if 300 <= i < 400 else 0.5)})
    assert abs(P.adjusted_reward(recs[:100], w) - 0.2) < 1e-9 and abs(P.adjusted_reward(recs[300:400], w) - 0.9) < 1e-9
    text_only = [r for r in recs if r["kind"] != "mm"]
    assert abs(P.adjusted_reward(text_only[300:400], w) - 0.9) > 0.05                       # the old indexing was off


def test_s_learn_soft_reports_targets_against_their_bands_and_never_stops():
    soft = P.s_learn_soft(0.60, 0.63, {"facts_mean": (0.70, 0.05), "recall_hit": (0.80, 0.10)}, {"facts_mean": 0.78, "recall_hit": 0.85, "unsafe": 1.0})
    assert soft["targets"]["facts_mean"]["up"] and not soft["targets"]["recall_hit"]["up"] and soft["any_target_up"]
    assert abs(soft["adj_gain"] - 0.03) < 1e-9 and set(soft["targets"]) == {"facts_mean", "recall_hit"}
    none = P.s_learn_soft(None, 0.5, {"facts_mean": (0.7, 0.05)}, {})
    assert none["adj_gain"] is None and none["targets"] == {} and not none["any_target_up"]


def test_lora_layout_requires_k_proj_on_every_non_shared_layer_and_shows_gaps():
    gap = P.lora_layout(_adapter_names(drop={(5, "k_proj"), (6, "k_proj"), (9, "k_proj")}), 42, kv_first=24)
    assert any("k_proj adapter missing on non-shared layers 5-6,9" in p for p in gap["problems"]) and gap["remedy"] and "TARGET_RE" in gap["remedy"]
    assert not any("k_proj adapter missing" in p for p in P.lora_layout(_adapter_names(drop={(5, "k_proj")}), 42, kv_first=None)["problems"])
    ok = P.lora_layout(_adapter_names(), 42, kv_first=24)
    assert ok["remedy"] is None and "layers 0-23" in ok["summary"]
    partial = P.lora_layout(_adapter_names(drop={(7, "k_proj"), (7, "v_proj")}), 42, kv_first=24)
    assert any("missing" in p for p in partial["problems"])
    assert P._ranges([0, 1, 2, 4, 5, 9]) == "0-2,4-5,9" and P._ranges([]) == "" and P._ranges([3]) == "3"


def test_small_n_floor_strict_errors_on_a_floor_without_the_c13_keys():
    floor = {"vision_reward": 1.0}
    now = {"proposals_right": 20.0, "proposals_spurious": 3.0, "vision_facts_total": 60.0}
    assert P.small_n_floor_problems(floor, now) == []                                   # rounds 3 and 4: skipped
    strict = P.small_n_floor_problems(floor, now, strict=True)
    assert len(strict) == 3 and all("predates C13" in p for p in strict)
    full = {**floor, **now}
    assert P.small_n_floor_problems(full, now, strict=True) == [] and len(P.small_n_floor_problems(full, {**now, "proposals_right": 19.0}, strict=True)) == 1


def test_l40_profile_matches_the_a6000_batch_and_sits_between_no_gain_and_spec():
    a6, l4 = P.PROFILES["a6000"], P.PROFILES["l40"]
    assert (a6["plan"], a6["lr"], a6["accelerate"]) == (l4["plan"], l4["lr"], l4["accelerate"])
    h_a6 = [P.hours("a6000", 1488, 1, c, 4) for c in (0, 1, 2)]
    h_l4 = [P.hours("l40", 1488, 1, c, 4) for c in (0, 1, 2)]
    assert all(l <= a + 1e-6 for l, a in zip(h_l4, h_a6)) and h_l4[0] < h_a6[0]
    assert abs(h_l4[2] - h_a6[2]) / h_a6[2] < 0.02          # pessimistic: no gain over the A6000
    assert P.HW["l40"]["usd_h"] == 1.06


def test_arch_ok_accepts_same_major_lower_minor_cubins():
    import importlib.util
    import sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("grpo_for_arch", Path(__file__).with_name("grpo.py"))
    g = importlib.util.module_from_spec(spec)
    sys.modules["grpo_for_arch"] = g
    try:
        spec.loader.exec_module(g)
    except ImportError as e:                                  # torch-free Mac: the helper is pure, but grpo.py may not import
        import pytest
        pytest.skip(f"grpo.py does not import here: {e}")
    cu128 = ["sm_75", "sm_80", "sm_86", "sm_90", "sm_100", "sm_120"]
    assert g.arch_ok((8, 9), cu128) and g.arch_ok((8, 6), cu128) and g.arch_ok((10, 3), cu128)
    assert not g.arch_ok((8, 9), ["sm_75", "sm_90"]) and not g.arch_ok((8, 0), ["sm_86"])
    assert g.arch_ok((10, 3), ["sm_90", "compute_100"]) and not g.arch_ok((9, 0), ["compute_100"])
