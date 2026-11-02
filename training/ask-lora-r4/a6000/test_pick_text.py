"""Mac tests for the A6000 kit: the text picker on fake generations, the packing guards, the estimates and the
changes made to ask-lora-r3/brev. No torch, no GPU, no tokenizer, no network.

    ~/Desktop/MOE_Project/.venv/bin/python -I -m pytest training/ask-lora-r4/a6000/test_pick_text.py -q
    ~/Desktop/MOE_Project/.venv/bin/python -I training/ask-lora-r4/a6000/test_pick_text.py        (also runs as a script)

The fake generations are written in the format Gemma 4 emits and reward.py parses (tool-call syntax, thought channel,
stop tokens), and the checkpoint table is built so each rule of PLAN.md section F decides exactly one outcome.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TRAINING = HERE.parent.parent

import estimate as E        # noqa: E402
import pack_r4 as P         # noqa: E402
import pick_text as T       # noqa: E402

R = T.load_reward()
Q = '<|"|>'


def _label_call() -> dict:
    return {"modality": "text", "step": "call", "final": False, "ask": False, "facts": [], "forced": False,
            "user_name": "Sam", "expected": [{"name": "add_reminder", "args": {"title": "Buy milk"}}],
            "key_args": [{"title": "Buy milk"}], "present_args": [[]], "alt_name_sets": [],
            "forbidden": ["send_email", "clear_reminders"]}


def _label_final() -> dict:
    return {"modality": "text", "step": "final", "final": True, "ask": False, "facts": ["6 PM", "Water plants"],
            "forced": True, "user_name": "Sam", "expected": [], "key_args": [], "present_args": [], "alt_name_sets": [],
            "forbidden": ["send_email"]}


def dev_rows(n_call: int = 50, n_final: int = 50) -> list[dict]:
    rows = [{"id": f"rem-{i:03d}", "family": "reminders", "prompt": f"p{i}", "label": _label_call()} for i in range(n_call)]
    rows += [{"id": f"day-{i:03d}", "family": "day", "prompt": f"q{i}", "label": _label_final()} for i in range(n_final)]
    return rows


GOOD_CALL = f"<|tool_call>call:add_reminder{{title:{Q}Buy milk{Q}}}<tool_call|><|tool_response>"
WRONG_CALL = f"<|tool_call>call:set_timer{{minutes:5}}<tool_call|><|tool_response>"
FORBIDDEN_CALL = f"<|tool_call>call:send_email{{to:{Q}a@b.c{Q}}}<tool_call|><|tool_response>"
FULL_ANSWER = "6 PM and 6 PM again\n<channel|>Tonight you have the Water plants reminder at 6 PM.<turn|>"
PARTIAL_ANSWER = "x\n<channel|>Tonight you have a reminder at 6 PM.<turn|>"
EMPTY_THOUGHT_ANSWER = "\n<channel|>Tonight you have the Water plants reminder at 6 PM.<turn|>"


def gens(rows, ckpt, wrong=0, forbidden=0, partial=0, empty_thought=0, malformed=0) -> list[dict]:
    """Fake generations for one checkpoint: the first `wrong` call steps call the wrong tool, `forbidden` call send_email,
    `partial` answers omit one fact, `empty_thought` skip the thought, `malformed` are cut off."""
    out, nc, nf = [], 0, 0
    for r in rows:
        if r["label"]["step"] == "call":
            raw = FORBIDDEN_CALL if nc < forbidden else WRONG_CALL if nc < forbidden + wrong else GOOD_CALL
            nc += 1
        else:
            raw = ("cut off with no stop" if nf < malformed else PARTIAL_ANSWER if nf < malformed + partial
                   else EMPTY_THOUGHT_ANSWER if nf < malformed + partial + empty_thought else FULL_ANSWER)
            nf += 1
        out.append({"ckpt": ckpt, "id": r["id"], "sample": 0, "raw": raw})
    return out


def table_inputs():
    rows = dev_rows()
    answers = (gens(rows, "step-00400") + gens(rows, "step-00800", forbidden=1)
               + gens(rows, "step-01200", wrong=1, partial=25)            # best selection among the eligible, facts 75%
               + gens(rows, "step-01600", wrong=2)                        # 1 point below, facts 100%
               + gens(rows, "final", wrong=3))                            # 2 points below: out
    steps = {"step-00400": 400, "step-00800": 800, "step-01200": 1200, "step-01600": 1600, "final": 1648}
    mm = {"step-00400": {"ok": False, "reason": "fails vision_proposal_f1"}, "step-00800": {"ok": True, "reason": ""},
          "step-01200": {"ok": True, "reason": ""}, "step-01600": {"ok": True, "reason": ""}, "final": {"ok": True, "reason": ""}}
    return rows, answers, steps, mm


# ------------------------------------------------------------------ the decision rules

def test_scoring_numbers():
    rows, answers, steps, _ = table_inputs()
    m = T.summarize(R, rows, answers)
    assert m["step-00400"]["selection_pct"] == 100.0 and m["step-00400"]["facts_pct"] == 100.0
    assert m["step-00400"]["forbidden_n"] == 0 and m["step-00400"]["malformed_pct"] == 0.0
    assert m["step-01200"]["selection_pct"] == 99.0                       # one of 100 steps earns 0 of its 1.5
    assert m["step-01200"]["facts_pct"] == 75.0                           # 25 of 50 answers state one of the two facts
    assert m["step-01600"]["selection_pct"] == 98.0 and m["final"]["selection_pct"] == 97.0
    assert m["step-00800"]["forbidden_n"] == 1 and m["step-00800"]["forbidden_ids"] == ["rem-000"]
    assert m["step-00800"]["n"] == 100 and m["step-00800"]["n_missing"] == 0


def test_pick_follows_the_rules():
    rows, answers, steps, mm = table_inputs()
    m = T.summarize(R, rows, answers)
    chosen, table = T.select(m, mm, steps)
    by = {t["name"]: t for t in table}
    assert chosen == "step-01600", chosen
    assert not by["step-00400"]["eligible"] and "mmeval" in by["step-00400"]["reasons"][0]      # best selection, fails mmeval
    assert not by["step-00800"]["eligible"] and "forbidden" in by["step-00800"]["reasons"][0]   # zero forbidden is a hard rule
    # the reference for "within 1 point" is the best ELIGIBLE selection (99.0, step-01200), not the excluded 100.0
    assert by["step-01200"]["near_best_selection"] and by["step-01600"]["near_best_selection"]
    assert not by["final"]["near_best_selection"] and by["final"]["eligible"]                   # 2 points below: eligible but out
    assert by["step-01600"]["mm"] == "ok" and by["step-00400"]["mm"] == "FAIL"


def test_within_is_inclusive_and_configurable():
    rows, answers, steps, mm = table_inputs()
    m = T.summarize(R, rows, answers)
    assert T.select(m, mm, steps, within=0.5)[0] == "step-01200"          # tighter: only the best selection is near
    assert T.select(m, mm, steps, within=2.0)[0] == "step-01600"          # wider: final is near too, but facts tie -> higher selection
    ties = {k: dict(v, facts_pct=100.0) for k, v in m.items()}
    assert T.select(ties, mm, steps, within=2.0)[0] == "step-01200"       # equal facts: the higher selection wins


def test_equal_scores_prefer_the_earlier_step():
    rows = dev_rows()
    answers = gens(rows, "step-01200") + gens(rows, "step-01600")
    m = T.summarize(R, rows, answers)
    assert T.select(m, None, {"step-01200": 1200, "step-01600": 1600})[0] == "step-01200"


def test_nobody_qualifies():
    rows = dev_rows()
    m = T.summarize(R, rows, gens(rows, "a", forbidden=1) + gens(rows, "b"))
    chosen, table = T.select(m, {"a": {"ok": True, "reason": ""}, "b": {"ok": False, "reason": "fails audio_wer"}}, {})
    assert chosen is None and all(not t["eligible"] for t in table)
    assert T.select(m, {"a": {"ok": True, "reason": ""}}, {})[0] is None                  # b missing from mmeval: not eligible


def test_missing_and_errored_generations_cannot_win():
    rows = dev_rows()
    answers = [a for a in gens(rows, "short") if a["id"] != "day-000"] + gens(rows, "ok", wrong=1)
    answers = [dict(a, error="OutOfMemoryError") if a["ckpt"] == "ok" and a["id"] == "day-001" else a for a in answers]
    m = T.summarize(R, rows, answers)
    assert m["short"]["n_missing"] == 1
    assert m["ok"]["malformed_pct"] == 1.0                                # the errored generation scores as malformed
    chosen, _ = T.select(m, None, {})
    assert chosen == "ok"                                                 # 'short' is disqualified for the missing step


def test_report_only_signals():
    rows = dev_rows()
    m = T.summarize(R, rows, gens(rows, "x", empty_thought=10, malformed=2))
    assert m["x"]["malformed_pct"] == 2.0
    assert m["x"]["narrate_think_pct"] == 76.0                            # 38 of the 50 narrate answers kept a thought
    assert m["x"]["think_words_p50"] is not None and m["x"]["answer_chars_p50"] > 20


def test_samples_pool():
    rows = dev_rows(4, 4)
    a = gens(rows, "c") + [dict(x, sample=1) for x in gens(rows, "c", wrong=4)]
    m = T.summarize(R, rows, a)
    assert m["c"]["n"] == 16 and m["c"]["selection_pct"] == 75.0        # 4 of the 16 pooled answers are wrong calls


# ------------------------------------------------------------------ helpers

def test_subsample_is_proportional_deterministic_and_covers_families():
    rows = dev_rows(80, 20)
    for limit in (10, 50, 99, 100, 500, 0):
        a, b = T.subsample(rows, limit), T.subsample(rows, limit)
        assert [r["id"] for r in a] == [r["id"] for r in b]
        assert len(a) == (len(rows) if limit in (0, 100, 500) else limit), (limit, len(a))
    sub = T.subsample(rows, 50)
    assert sum(r["family"] == "reminders" for r in sub) == 40 and sum(r["family"] == "day" for r in sub) == 10
    many = [{"id": f"f{i}", "family": f"fam{i}", "prompt": "", "label": {}} for i in range(30)]
    assert len(T.subsample(many, 10)) == 10                               # more families than steps: still the limit
    assert len(T.subsample(many, 40)) == 30


def test_candidate_names():
    names = ["step-00200", "step-00400", "step-00600", "step-00800", "step-01000", "step-01200", "step-01400",
             "step-01600", "step-01648", "final", "best"]
    got = T.candidate_names(names, 1648, 0.45, 6)
    assert got[-1] == "step-01648" and "final" not in got and "best" not in got and len(got) == 6
    assert all(T.step_of(n) >= 0.45 * 1648 for n in got)
    got2 = T.candidate_names(["step-01000", "step-01200", "step-01600", "final"], 1648, 0.45, 6)
    assert got2 == ["step-01000", "step-01200", "step-01600", "final"]          # no dir for the last step: final stands in
    assert T.candidate_names(["step-00200"], 1648) == []


def test_row_seed_is_stable_across_checkpoints():
    assert T.row_seed(1, "a", 0) == T.row_seed(1, "a", 0) != T.row_seed(1, "a", 1)


def test_mmeval_status_uses_mmeval_pick_and_ignores_missing_loss():
    results = {"base": {"reading": {"n": 4}},
               "checkpoints": {"step-01200": {"metrics": {}, "verdict": {"ok": True, "failed": [], "skipped": [], "no_data": []},
                                              "dev_loss_text": None, "step": 1200},
                               "step-01600": {"metrics": {}, "verdict": {"ok": False, "failed": ["vision_new_leaks"], "skipped": [], "no_data": []},
                                              "dev_loss_text": 0.4, "step": 1600}}}
    st = T.mmeval_status(results)
    assert st["step-01200"]["ok"] and not st["step-01600"]["ok"] and "vision_new_leaks" in st["step-01600"]["reason"]
    assert T.mmeval_status(results, require_reading=True)["step-01200"]["ok"]            # the base has reading rows
    results["base"] = {}
    assert not T.mmeval_status(results, require_reading=True)["step-01200"]["ok"]        # --require-reading with none refuses


# ------------------------------------------------------------------ the CLI end to end (score + pick)

def _run_main(argv):
    import io
    from contextlib import redirect_stderr, redirect_stdout
    out, err = io.StringIO(), io.StringIO()
    old = sys.argv
    sys.argv = ["pick_text.py"] + argv
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = T.main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = old
    return rc, out.getvalue(), err.getvalue()


def test_cli_score_then_pick():
    rows, answers, steps, mm = table_inputs()
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "text-dev.jsonl").write_text("".join(json.dumps(dict(r, label=json.dumps(r["label"]))) + "\n" for r in rows))
        out = d / "text"
        out.mkdir()
        (out / "text_answers.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers))
        rc, so, _ = _run_main(["score", "--data", str(d / "text-dev.jsonl"), "--out", str(out), "--limit", "0"])
        assert rc == 0 and "step-01200" in so and (out / "text_results.json").is_file()
        res = json.loads((out / "text_results.json").read_text())
        res["ckpt_steps"] = steps
        (out / "text_results.json").write_text(json.dumps(res))
        mm_results = {"base": {}, "checkpoints": {k: {"metrics": {}, "dev_loss_text": 0.4, "step": steps[k],
                                                     "verdict": {"ok": v["ok"], "failed": [v["reason"]] if not v["ok"] else [],
                                                                 "skipped": [], "no_data": []}} for k, v in mm.items()}}
        (d / "mm.json").write_text(json.dumps(mm_results))
        rc, so, se = _run_main(["pick", "--text-results", str(out / "text_results.json"), "--mmeval-results", str(d / "mm.json"),
                                "--json-out", str(d / "picked.json")])
        assert rc == 0 and so.strip() == "step-01600", (rc, so, se)
        assert json.loads((d / "picked.json").read_text())["chosen"] == "step-01600" and "PICK" in se
        # mmeval is mandatory unless skipped on purpose
        rc, _, se = _run_main(["pick", "--text-results", str(out / "text_results.json")])
        assert rc != 0
        rc, so, se = _run_main(["pick", "--text-results", str(out / "text_results.json"), "--no-mmeval"])
        assert rc == 0 and so.strip() == "step-00400" and "NOT applied" in se          # without mmeval the 100.0 wins; loudly
        # nobody qualifies -> exit 2, nothing printed on stdout
        for v in mm.values():
            v["ok"] = False
        bad = dict(mm_results, checkpoints={k: dict(c, verdict={"ok": False, "failed": ["x"], "skipped": [], "no_data": []})
                                            for k, c in mm_results["checkpoints"].items()})
        (d / "mm_bad.json").write_text(json.dumps(bad))
        rc, so, se = _run_main(["pick", "--text-results", str(out / "text_results.json"), "--mmeval-results", str(d / "mm_bad.json")])
        assert rc == 2 and so.strip() == "" and "NO CHECKPOINT QUALIFIES" in se


def test_labels_made_by_the_real_exporter_score_end_to_end():
    """Labels from grpo/export_prompts.classify (what pack_r4 attaches) work with the picker: an ideal answer scores
    100% selection and 100% facts, and a forbidden write is caught."""
    grpo = P.pick_grpo_dir(None)
    sys.path.insert(0, str(grpo))
    ex_mod = P.load_module(grpo / "export_prompts.py", "test_export_prompts")
    ex = {"tools": [], "messages": [
        {"role": "user", "content": "what is on tonight"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function",
                                                              "function": {"name": "list_reminders", "arguments": {}}}]},
        {"role": "tool", "tool_call_id": "1", "content": "Water plants 6 PM"},
        {"role": "assistant", "content": "Tonight you have Water plants at 6 PM."}]}
    meta = {"id": "t-1", "family": "day", "think": True, "thought": "Water plants 6 PM"}
    label = ex_mod.classify(ex, meta)
    label["forced"] = True
    assert label["final"] and label["facts"]
    good = "Water plants 6 PM\n<channel|>Tonight you have Water plants at 6 PM.<turn|>"
    s = T.score_answer(R, good, label)
    assert s["selection"] == 1.0 and s["facts"] == 1.0 and not s["forbidden"] and not s["malformed"]
    bad = f"\n<channel|><|tool_call>call:send_email{{to:{Q}a@b.c{Q}}}<tool_call|><|tool_response>"
    assert T.score_answer(R, bad, label)["forbidden"] is True


# ------------------------------------------------------------------ packing

def test_pack_refuses_missing_inputs_before_doing_anything():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        bad = P.check_inputs(d / "data", d / "vision" / "vision_rows.jsonl", d / "audio_rows.jsonl", (str(d / "c.json"), str(d / "t.json")), d)
        text = "\n".join(bad)
        assert "missing or empty text data" in text and "vision rows" in text and "audio rows" in text and "gate file" in text
        assert "chat_template.jinja" in text
        r = subprocess.run([sys.executable, "-I", str(HERE / "pack_r4.py"), "--model", str(d), "--data", str(d / "data"),
                            "--vision-rows", str(d / "v.jsonl"), "--audio-rows", str(d / "a.jsonl"), "--out-dir", str(d / "out")],
                           capture_output=True, text=True)
        assert r.returncode != 0 and "not packing" in r.stderr
        assert not (d / "out").exists()                                    # nothing was created


def test_pack_check_inputs_passes_a_complete_set_and_catches_misaligned_meta():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        data = d / "data"
        data.mkdir()
        for n in ("train.jsonl", "valid.jsonl", "train.meta.jsonl", "valid.meta.jsonl"):
            (data / n).write_text('{"a": 1}\n{"a": 2}\n')
        (data / "stats.json").write_text("{}")
        (d / "vision").mkdir()
        (d / "vision" / "img.png").write_bytes(b"x")
        (d / "vision" / "vision_rows.jsonl").write_text(json.dumps({"id": "v", "images": ["img.png"]}) + "\n")
        (d / "a.jsonl").write_text("{}\n")
        (d / "tpl").mkdir()
        (d / "tpl" / "chat_template.jinja").write_text("x")
        assert P.check_inputs(data, d / "vision" / "vision_rows.jsonl", d / "a.jsonl", None, d / "tpl") == []
        (data / "valid.meta.jsonl").write_text('{"a": 1}\n')
        assert any("valid.jsonl has 2 rows but valid.meta.jsonl has 1" in b for b in P.check_inputs(
            data, d / "vision" / "vision_rows.jsonl", d / "a.jsonl", None, d / "tpl"))
        (d / "vision" / "img.png").unlink()
        assert any("image(s) not found" in b for b in P.check_inputs(data, d / "vision" / "vision_rows.jsonl", d / "a.jsonl", None, d / "tpl"))


def test_build_text_dev_joins_by_position_and_checks_ids():
    dev = [{"id": "a", "family": "f", "prompt": "x<|channel>thought\n", "think": True, "completion": "c1"},
           {"id": "b", "family": "g", "prompt": "y", "think": False, "completion": "c2"}]
    pairs = [({"messages": []}, {"id": "a"}), ({"messages": []}, {"id": "b"})]
    out = P.build_text_dev(dev, pairs, lambda ex, meta: {"step": "final"})
    assert [r["label"]["forced"] for r in out] == [True, False] and out[0]["label"]["ideal_completion"] == "c1"
    for bad in (pairs[::-1], pairs[:1]):
        try:
            P.build_text_dev(dev, bad, lambda ex, meta: {})
        except ValueError:
            continue
        raise AssertionError("misaligned join accepted")


def test_pickers_zip_has_what_the_box_needs():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        P.write_pickers_zip(d / "pickers.zip", [{"id": "a", "family": "f", "prompt": "p", "label": {}}], P.pick_grpo_dir(None))
        with zipfile.ZipFile(d / "pickers.zip") as z:
            names = set(z.namelist())
        assert {"pick_text.py", "reward.py", "data/text-dev.jsonl", "PICKERS_MANIFEST.json"} <= names


def test_the_packed_picker_runs_standalone_from_a_copy():
    """pickers.zip + mmeval-data.zip land in one directory on the box: simulate that with copies and no repo around."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        mm = TRAINING / "ask-lora-r3" / "mmeval"
        for n in ("scoring.py", "rows.py", "serving.py", "score_mm.py", "eval_mm.py", "pick_checkpoint.py"):
            (d / n).write_bytes((mm / n).read_bytes())
        (d / "pick_text.py").write_bytes((HERE / "pick_text.py").read_bytes())
        (d / "reward.py").write_bytes((P.pick_grpo_dir(None) / "reward.py").read_bytes())
        r = subprocess.run([sys.executable, "-I", str(d / "pick_text.py"), "--help"], capture_output=True, text=True, cwd=d)
        assert r.returncode == 0 and "candidates" in r.stdout


# ------------------------------------------------------------------ estimates and the r3 changes

def test_estimate_reproduces_the_r3_plan_and_scales():
    r3 = E.sft_estimate(2990, 6600, eval_every=26, dev_text_rows=364)     # r3 PLAN.md: "~19-21 h" for 2,990 text rows
    assert 16 <= r3["train_h"] <= 21, r3
    a, b = E.sft_estimate(4950), E.sft_estimate(5850)
    assert a["total_h"] < b["total_h"] and 28 <= a["total_h"] <= 36 and 33 <= b["total_h"] <= 42
    assert E.sft_estimate(5400, tok_s=520)["total_h"] > E.sft_estimate(5400, tok_s=700)["total_h"]
    rows = E.epoch_rows(3254)
    assert rows == {"text": 3254, "vision": 476, "audio": 238, "total": 3968}      # the r3 run's numbers (train.log start event)
    assert E.sft_estimate(3254)["steps"] == 992


def test_memory_arithmetic_fits_with_margin():
    for attn in ("sdpa", "math"):
        assert E.peak_gib(10240, attn) < E.CARD_GIB - 12, attn                # >= 12 GiB spare even if math attention is forced
    assert abs(E.peak_gib(E.SDPA_PEAK_TOKENS, "sdpa") - E.SDPA_PEAK_GIB) < 0.05
    assert abs(E.peak_gib(E.MATH_PEAK_TOKENS, "math") - E.MATH_PEAK_GIB) < 0.05


def test_grpo_calibration_is_slower_than_plan_py():
    plan = E.grpo_hours_plan_py()
    cal = E.grpo_hours_calibrated()
    assert cal > 25
    if plan:
        assert cal > plan[1]


def test_r3_brev_edits_are_backward_compatible():
    spec = importlib.util.spec_from_file_location("r3_train_a6000", TRAINING / "ask-lora-r3" / "brev" / "train.py")
    tr = importlib.util.module_from_spec(spec)
    sys.modules["r3_train_a6000"] = tr
    spec.loader.exec_module(tr)
    assert tr.spread_indices(10, 0) == list(range(10)) and tr.spread_indices(10, 20) == list(range(10))
    assert tr.spread_indices(10, 4) == [0, 2, 5, 7] and len(set(tr.spread_indices(364, 150))) == 150
    src = (TRAINING / "ask-lora-r3" / "brev" / "train.py").read_text()
    assert 'ap.add_argument("--dev-text-max", type=int, default=0' in src       # default 0 = all dev rows, r3's behaviour
    assert 'ap.add_argument("--global-batch", "--accum", dest="global_batch", type=int, default=8' in src
    assert 'ap.add_argument("--lr", type=float, default=2e-4)' in src and 'ap.add_argument("--max-len", type=int, default=10240' in src
    render = (TRAINING / "ask-lora-r3" / "brev" / "render.py").read_text()
    assert 'HERE / "upload-zip.json"' in render                                  # default output unchanged
    setup = (TRAINING / "ask-lora-r3" / "brev" / "vm-setup.sh").read_text()
    assert '${TRANSFORMERS_SPEC:-transformers>=5.10.1,<6}' in setup and '${PEFT_SPEC:-peft>=0.19.0}' in setup


def test_run_sft_matches_r3_effective_batch():
    sh = (HERE / "run_sft.sh").read_text()
    assert "GLOBAL_BATCH=8" in sh and "LR=2e-4" in sh and "NPROC=1" in sh and "--epochs 2" in sh
    assert "--attn-parity-strict" in sh and 'PIN_TORCH="2.11.0"' in sh and 'PIN_TRANSFORMERS="5.22.1"' in sh and 'PIN_PEFT="0.21.2"' in sh
    r = subprocess.run(["bash", "-n", str(HERE / "run_sft.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
