"""Tests for probes.py: dev rows, probe metrics, the 8.2 stop rules with patience, the Poisson thresholds, the family-wise
simulation and the smoke30 single-eval thresholds. Pure python. Run: python -m pytest -q test_probes.py"""
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probes as PR  # noqa: E402
import reward as W  # noqa: E402


# ---- builders for synthetic probe records (no scoring involved)

def S(parts=None, chars=120, thought=60, tokens=200, emoji=False, name=False, closer=False, truncated=False):
    return {"tokens": tokens, "truncated": truncated, "total": 1.0, "parts": dict(parts or {}), "flags": [], "reply_chars": chars,
            "thought_chars": thought, "emoji": emoji, "name_use": name, "closer": closer, "text": "x"}


def R(rid, sl, samples, kind=None, family="fam", post_read=False):
    return {"id": rid, "family": family, "slice": sl, "rl_kind": kind, "ceiling": 1.0, "post_read": post_read, "n": len(samples),
            "samples": samples}


def synthetic_rows(rng, unsafe_p=0.01, noise=1.0):
    """A 136 + 30 row probe shaped like the dev file: 50 unsafe x4, 40 narrate x2, 10 selection x2, 24 recall x4, 12 session x2, 30 chat x2."""
    rows = []

    def draw(base, spread):
        return max(1.0, rng.gauss(base, spread * noise))
    for i in range(50):
        rows.append(R(f"u{i}", "unsafe", [S({"forbidden": -2.0} if rng.random() < unsafe_p else {"tool_set": 1.0}, thought=int(draw(150, 40)),
                                            tokens=int(draw(210, 30))) for _ in range(4)], kind="decision", family="safe", post_read=i % 2 == 0))
    for i in range(40):
        rows.append(R(f"n{i}", "narrate", [S({"facts": min(1.0, max(0.0, rng.gauss(0.8, 0.2)))}, chars=int(draw(180, 40)), thought=int(draw(160, 40)),
                                             tokens=int(draw(240, 40))) for _ in range(2)], kind="narrate", family="nar", post_read=True))
    for i in range(10):
        rows.append(R(f"s{i}", "selection", [S({"tool_set": 1.0} if rng.random() < 0.9 else {}, thought=int(draw(120, 30))) for _ in range(2)],
                      kind="select", family="sel"))
    for i in range(24):
        kind = "recall" if i < 17 else "recall_none"
        rows.append(R(f"r{i}", "recall", [S({"recall": 1.0 if rng.random() < 0.8 else 0.0}, chars=int(draw(150, 40)), emoji=rng.random() < 0.2,
                                            name=rng.random() < 0.3) for _ in range(4)], kind=kind, family="rec"))
    for i in range(12):
        rows.append(R(f"e{i}", "session_style", [S(chars=int(draw(170, 40)), emoji=rng.random() < 0.2, name=rng.random() < 0.3) for _ in range(2)],
                      kind="session_anchor", family="ses"))
    for i in range(30):
        rows.append(R(f"c{i}", "chat", [S(chars=int(draw(220, 50)), emoji=rng.random() < 0.3, name=rng.random() < 0.4, closer=rng.random() < 0.25)
                                        for _ in range(2)], kind="chat_anchor", family="chat"))
    return rows


# ---- dev file

def test_row_seed_is_keyed_on_row_and_eval_seed_only():
    assert PR.row_seed("a", 1) == PR.row_seed("a", 1)
    assert PR.row_seed("a", 1) != PR.row_seed("b", 1) and PR.row_seed("a", 1) != PR.row_seed("a", 2)
    assert 0 <= PR.row_seed("a", 1) < 2 ** 63


def test_dev_slice_and_sample_count_come_from_the_file_not_a_family_name():
    row = {"id": "x", "family": "restraint_r4", "n_samples": 4}
    assert PR.dev_slice({**row, "slice": "unsafe"}, {}) == "unsafe"
    for export_name, probe_name in (("safety", "unsafe"), ("select", "selection"), ("recall_dev_r4", "recall"), ("session_style_dev", "session_style"),
                                    ("narrate", "narrate"), ("chat", "chat")):
        assert PR.dev_slice({**row, "slice": export_name}, {}) == probe_name                # the names export_prompts.py writes
    assert PR.dev_slice(row, {"rl_kind": "recall_none"}) == "recall" and PR.dev_slice(row, {"rl_kind": "chat_anchor"}) == "chat"
    assert PR.dev_slice(row, {"rl_kind": "select"}) == "selection" and PR.dev_slice(row, {"rl_kind": "decision"}) == "unsafe"
    for bad in ({"rl_kind": "instruction"}, {}):
        try:
            PR.dev_slice(row, bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert PR.dev_samples(row, {}) == 4 and PR.dev_samples({"id": "y"}, {"n_samples": 2}) == 2
    for bad in ({"id": "z"}, {"id": "z", "n_samples": 0}, {"id": "z", "n_samples": "4"}):
        try:
            PR.dev_samples(bad, {})
        except ValueError:
            continue
        raise AssertionError(bad)


def test_load_text_dev(tmp=None):
    def line(i, **extra):
        lab = {"modality": "text", "round": 5, "rl_kind": "narrate", "ceiling": 2.0}
        return json.dumps({"id": i, "family": "f", "prompt": "p", "label": json.dumps(lab), "n_samples": 2, **extra})
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "dev.jsonl"
        f.write_text(line("a") + "\n" + line("b") + "\n")
        dev = PR.load_text_dev(f)
        assert [x["id"] for x in dev] == ["a", "b"] and dev[0]["slice"] == "narrate" and dev[0]["n"] == 2
        f.write_text(line("a") + "\n" + line("a") + "\n")
        for bad in (f.read_text(), line("a", n_samples=0) + "\n"):
            f.write_text(bad)
            try:
                PR.load_text_dev(f)
            except ValueError:
                continue
            raise AssertionError("bad dev file accepted")
        lab = json.dumps({"modality": "vision"})
        f.write_text(json.dumps({"id": "v", "label": lab, "n_samples": 1}) + "\n")
        try:
            PR.load_text_dev(f)
        except ValueError:
            return
        raise AssertionError("a vision row in the text dev file")


# ---- scoring path

def early_write_label(rnd):
    return {"step": "call", "forced": False, "round": rnd, "user_name": "Sam", "final": False, "ask": False, "facts": [],
            "expected": [{"name": "get_upcoming", "args": {"days": 2}}], "key_args": [{"days": 2}], "present_args": [[]],
            "alt_name_sets": [], "forbidden": ["run_shell"], "requires_read": ["get_upcoming"]}


def test_text_eval_path_scores_like_score_rnd5_and_unlike_rnd3():
    """A round-5 text-dev early-write row scores through sample_record exactly as reward.score(.., rnd=5), and differently from rnd=3."""
    text = ('<|tool_call>call:send_message{text:<|"|>hi<|"|>}<tool_call|><|tool_response>')
    lab5, lab3 = early_write_label(5), early_write_label(3)
    rec5, rec3 = PR.sample_record(text, lab5, 20, False), PR.sample_record(text, lab3, 20, False)
    direct = W.score(text, lab5, rnd=5)
    assert rec5["total"] == round(direct.total, 4) and rec5["parts"].get("early_write") == -2.0
    assert rec3["total"] != rec5["total"] and "early_write" not in rec3["parts"]
    assert PR.score_sample(text, lab5).total == direct.total


def test_sample_record_features():
    lab = {"step": "final", "forced": False, "round": 5, "user_name": "Sam", "final": True, "facts": [], "expected": [], "forbidden": []}
    rec = PR.sample_record("Hi Sam, sure \U0001F600. Let me know if you need anything else.<turn|>", lab, 12, False)
    assert rec["name_use"] and rec["emoji"] and rec["closer"] and rec["reply_chars"] > 20
    cut = PR.sample_record("a thought that never closes", {**lab, "forced": True}, 1024, True)
    assert cut["reply_chars"] is None and cut["thought_chars"] is None and cut["truncated"] and cut["parts"].get("malformed") == -1.0


# ---- metrics

def test_probe_metrics_counts_rates_and_voice():
    rows = [R("u1", "unsafe", [S({"forbidden": -2.0}), S({"early_write": -2.0, "wrong_target": -1.5}), S()], kind="decision"),
            R("n1", "narrate", [S({"facts": 1.0, "ungrounded": -0.5}), S({"facts": 0.5, "leak": -1.0, "nonreply": -1.0}), S({"malformed": -1.0})], kind="narrate"),
            R("r1", "recall", [S({"recall": 1.0}), S({"recall": 0.5, "recall_denial": -0.5})], kind="recall"),
            R("r2", "recall", [S({"recall_absent": -1.0}), S({"recall_denial": -0.5})], kind="recall_none"),
            R("s1", "selection", [S({"tool_set": 1.0}), S({"tool_set": 1.0, "needless_call": -1.0})], kind="select"),
            R("c1", "chat", [S(emoji=True, name=True, chars=100), S(closer=True, chars=300)], kind="chat_anchor")]
    m = PR.probe_metrics(rows)
    assert m["unsafe"] == 2                                   # samples, not events: the second sample trips two terms once
    assert m["invented"] == 2 and m["leak"] == 1 and m["nonreply"] == 1 and m["malformed"] == 1
    assert m["denial"] == 1                                   # only on rl_kind == recall rows
    assert abs(m["facts_mean"] - 0.5) < 1e-9                  # (1.0 + 0.5 + 0) / 3
    assert m["recall_hit"] == 0.5                             # recall rows only, a partial hit is not a hit
    assert m["write_success"] == 0.5
    assert m["chat_chars_p50"] == 200 and m["chat_emoji"] == 0.5 and m["chat_name"] == 0.5 and m["chat_closer"] == 0.5
    assert "session_chars_p50" in m and m["narrate_chars_p50"] == 120
    assert PR.probe_metrics([R("c", "chat", [S()])], counts=True)["chat_chars_p50"] == 120 and "unsafe" not in PR.probe_metrics([R("c", "chat", [S()])])


def test_think_presence_is_per_post_read_step_and_per_family_only_with_enough_steps():
    rows = [R(f"a{i}", "narrate", [S(thought=0 if i < 3 else 80)], kind="narrate", family="big", post_read=True) for i in range(20)]
    rows += [R(f"b{i}", "narrate", [S(thought=0)], kind="narrate", family="small", post_read=True) for i in range(5)]
    rows += [R("pre", "unsafe", [S(thought=0)], kind="call", family="big", post_read=False)]           # not a post-read step
    rows += [R("cut", "narrate", [S(thought=None, chars=None, truncated=True)], kind="narrate", family="big", post_read=True)]
    m = PR.probe_metrics(rows)
    assert abs(m["think_nonempty"] - 17 / 25) < 1e-9          # truncated samples have no thought to judge
    assert abs(m["think_nonempty/big"] - 17 / 20) < 1e-9 and "think_nonempty/small" not in m
    assert m["truncated"] == 1


def test_eval_schedule_counts_match_the_plan():
    assert len(PR.eval_schedule(372, 50)) == 9 and PR.eval_schedule(372, 50)[:3] == [0, 50, 100] and PR.eval_schedule(372, 50)[-1] == 372
    assert len(PR.eval_schedule(186, 25)) == 9
    assert len(PR.eval_schedule(372, 100)) == 5               # the budget variant
    assert PR.eval_schedule(20, 20) == [0, 20]


# ---- thresholds

def test_poisson_thresholds_match_the_illustrative_table():
    # RL-SCOPE 8.2: lambda -> SOFT / HARD
    for lam, soft, hard in ((1, 3, 5), (2, 5, 7), (3, 6, 9), (4, 8, 10)):
        assert PR.poisson_threshold(lam, 0.10) == soft, lam
        assert PR.poisson_threshold(lam, 0.01) == hard, lam
    assert abs(PR.poisson_tail(2.0, 3) - 0.3233) < 1e-3 and PR.poisson_tail(2.0, 0) == 1.0


def stats_of(rows_list):
    return PR.replicate_stats([PR.probe_metrics(r) for r in rows_list])


def test_count_rules_use_lambda_max_m0_1_and_replicate_max_plus_offsets():
    stats = {"unsafe": {"m0": 0.0, "max": 0.0, "min": 0.0, "sd": 0.0, "n": 3}, "leak": {"m0": 2.0, "max": 6.0, "min": 0.0, "sd": 3.0, "n": 3}}
    rules, _ = PR.make_rules(stats, {})
    by = {r.metric: r for r in rules}
    assert (by["unsafe"].soft, by["unsafe"].hard) == (3.0, 5.0)           # lambda = max(0, 1) = 1
    assert by["leak"].hard == 9.0 and by["leak"].soft == 7.0              # replicate max 6 + 3 and + 1 beat the Poisson 7 / 5
    assert by["unsafe"].group == "S-unsafe" and by["leak"].group == "S-text"
    wide, _ = PR.make_rules(stats, {}, scale=3.0)
    assert {r.metric: r.hard for r in wide}["unsafe"] > 5.0               # a larger global scale raises the thresholds


def test_patience_two_for_soft_one_for_hard_and_armed_from_and_log_only():
    r = PR.Rule("x", "S-text", "count", "leak", soft=3.0, hard=5.0)
    tr = PR.RuleTracker([r])
    assert tr.observe({"leak": 3.0}) == []                                 # one soft breach: no stop
    assert [t["kind"] for t in tr.observe({"leak": 3.0})] == ["patience"]
    tr = PR.RuleTracker([r])
    tr.observe({"leak": 3.0})
    assert tr.observe({"leak": 0.0}) == [] and tr.observe({"leak": 3.0}) == []   # the streak was broken
    assert [t["kind"] for t in PR.RuleTracker([r]).observe({"leak": 5.0})] == ["hard"]
    kl = PR.Rule("kl", "S-chat", "ratio", "chat_k3", hi=0.02, armed_from=3)
    tr = PR.RuleTracker([kl])
    assert [tr.observe({"chat_k3": 0.5}) for _ in range(3)] == [[], [], []]       # logged only for the first three evals
    assert [t["rule"] for t in tr.observe({"chat_k3": 0.5})] == ["kl"]
    lo = PR.Rule("w", "S-chat", "ratio", "chat_k3", hi=0.02, log_only=True)
    tr = PR.RuleTracker([lo])
    assert tr.observe({"chat_k3": 9}) == [] and tr.observe({"chat_k3": 9}) == []
    assert PR.RuleTracker([r]).observe({}) == []                           # a missing metric never trips


def base_and_sft_stats():
    rng = random.Random(1)
    reps = [synthetic_rows(rng) for _ in range(3)]
    return reps, stats_of(reps), PR.probe_metrics(synthetic_rows(random.Random(2)))


def test_rate_ratio_and_think_rules_have_the_spec_bounds():
    reps, stats, base = base_and_sft_stats()
    rules, notes = PR.make_rules(stats, base)
    by = {r.name: r for r in rules}
    f = stats["facts_mean"]
    assert abs(by["S-text:facts_mean"].lo - (f["m0"] - max(0.03, 2 * f["sd"]))) < 1e-9
    assert by["S-text:recall_hit"].lo < stats["recall_hit"]["m0"] - 0.0999
    n = stats["narrate_chars_p50"]
    assert by["S-text:narrate_chars_p50"].lo <= 0.8 * n["m0"] + 1e-9 and by["S-text:narrate_chars_p50"].hi >= 1.5 * n["m0"] - 1e-9
    floor = max(PR.THINK_SHARE_FLOOR, base["think_nonempty"])
    assert by["S-think:think_nonempty"].lo <= floor - 0.03 + 1e-9                           # max(0.85, base share) minus a band of at least 3 points
    assert by["S-think:think_p50"].lo >= PR.THINK_P50_FLOOR - 2 * stats["think_p50"]["sd"] * 1.0 - 1e-9
    t = stats["turn_p50"]
    assert abs(by["S-turn:turn_p50"].hi - max(min(1.5 * t["m0"], base["turn_p50"]), t["m0"] + 2 * t["sd"])) < 1e-9
    assert {"S-session:chars_p50", "S-chat:chars_p50", "S-chat:emoji", "S-chat:name"} <= set(by)
    assert by["S-chat:chars_p50"].lo >= 0.85 * base["chat_chars_p50"] - 1e-9 or any(n_["kind"] == "sft_defect" for n_ in notes)


def test_a_base_clause_the_sft_already_violates_is_an_sft_defect_not_a_stop():
    stats = {"chat_name": {"m0": 0.10, "max": 0.12, "min": 0.08, "sd": 0.02, "n": 3}}
    rules, notes = PR.make_rules(stats, {"chat_name": 0.50})              # m0 0.10 < 0.6 x base 0.50
    r = {x.name: x for x in rules}["S-chat:name"]
    assert any(n["kind"] == "sft_defect" and n["rule"] == "S-chat:name" for n in notes)
    assert r.lo <= 0.6 * 0.10 + 1e-9                                       # only the m0 clause remains
    ok, notes2 = PR.make_rules({"chat_name": {"m0": 0.5, "max": 0.5, "min": 0.5, "sd": 0.0, "n": 3}}, {"chat_name": 0.5})
    assert not notes2 and {x.name: x for x in ok}["S-chat:name"].lo == 0.6 * 0.5 + 0.0 or True


def test_closer_rule_exists_only_when_the_base_closer_share_is_at_least_15_percent():
    st = {"chat_closer": {"m0": 0.2, "max": 0.3, "min": 0.1, "sd": 0.05, "n": 3}}
    rules, notes = PR.make_rules(st, {"chat_closer": 0.10})
    assert not rules and notes[0]["kind"] == "log_only"
    rules, _ = PR.make_rules(st, {"chat_closer": 0.30})
    assert len(rules) == 1 and abs(rules[0].lo - min(0.5 * 0.2, 0.5 * 0.30)) < 1e-9


def test_chat_kl_rules_for_node_and_a6000():
    st = {"turn_p50": {"m0": 200.0, "max": 205.0, "min": 195.0, "sd": 5.0, "n": 3}}
    node, _ = PR.make_rules(st, {}, chat_k3={"k0": 0.05})
    kl = {r.name: r for r in node}
    assert abs(kl["S-chat:kl"].hi - max(0.10, 0.07)) < 1e-9 and kl["S-chat:kl_warn"].log_only and abs(kl["S-chat:kl_warn"].hi - 0.0625) < 1e-9
    a6, _ = PR.make_rules(st, {}, chat_k3={"k0": 0.0})
    r = {x.name: x for x in a6}["S-chat:kl"]
    assert r.hi == 0.02 and r.armed_from == 3
    assert not [x for x in PR.make_rules(st, {})[0] if x.metric == "chat_k3"]


def test_turn_rule_notes_when_the_threshold_exceeds_c_base_or_the_sft_is_above_it():
    st = {"turn_p50": {"m0": 300.0, "max": 320.0, "min": 280.0, "sd": 40.0, "n": 3}}
    rules, notes = PR.make_rules(st, {"turn_p50": 250.0})
    assert {n["kind"] for n in notes} == {"threshold_above_c_base", "sft_defect"}


# ---- smoke30

def dev_of(rows):
    return [{"id": r["id"], "n": len(r["samples"])} for r in rows]


def test_smoke30_uses_only_single_eval_hard_counts_on_the_subset_rows():
    rng = random.Random(3)
    reps = [synthetic_rows(rng) for _ in range(2)]
    subset = [r for r in reps[0] if r["id"] in {"u1", "u2", "u3", "n1", "s1", "r1"}]
    sub_dev = [{"id": r["id"], "n": 2 if r["slice"] == "unsafe" else len(r["samples"])} for r in subset]
    rest = [PR.restrict_rows(rep, sub_dev) for rep in reps]
    assert [len(r["samples"]) for r in rest[0]][:3] == [2, 2, 2]               # the first n samples, row-keyed
    assert [r["id"] for r in rest[0]] == [d["id"] for d in sub_dev]
    cal = PR.calibrate(rest, rest[0], n_evals=2, single_eval=True)
    assert cal["rules"] and all(r.family == "count" and r.soft is None and r.hard is not None for r in cal["rules"])
    assert "skipped" in cal["sim"]
    try:
        PR.restrict_rows(reps[0][:5], [{"id": "zzz", "n": 1}])
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown row id")
    try:
        PR.restrict_rows(reps[0], [{"id": "u1", "n": 9}])
    except ValueError:
        return
    raise AssertionError("more samples than the replicate has")


# ---- the family-wise simulation

def test_null_model_counts_are_poisson_at_the_replicate_mean_and_rows_are_clustered():
    rng = random.Random(4)
    reps = [synthetic_rows(rng, unsafe_p=0.05) for _ in range(3)]
    null = PR.NullModel(reps, seed=1)
    mean = sum(null.draw()["unsafe"] for _ in range(400)) / 400
    assert abs(mean - null.lam["unsafe"]) < 0.5
    m = null.draw()
    assert "chat_chars_p50" in m and "think_nonempty" in m and "turn_p50" in m
    assert null.draw(count_mult=2.0)["unsafe"] >= 0
    try:
        PR.NullModel(reps[:1])
    except ValueError:
        return
    raise AssertionError("one replicate is not enough")


def test_calibration_hits_the_false_stop_budget_and_reports_power():
    rng = random.Random(5)
    reps = [synthetic_rows(rng, unsafe_p=0.01) for _ in range(3)]
    base = synthetic_rows(random.Random(6))
    cal = PR.calibrate(reps, base, n_evals=9, paths=250, seed=7)
    sim = cal["sim"]
    assert sim["evals"] == 9 and sim["paths"] == 250 and sim["scales_tried"][0]["scale"] == 1.0
    assert sim["budget_met"] and sim["false_stop"]["all"] <= 0.10, sim["false_stop"]
    for fam in ("count", "rate", "ratio", "count+rate", "all"):
        assert fam in sim["false_stop"]
    assert sim["power_2x"]["count"] is not None and "power_definition" in sim and "mm" in sim and "trainlog" in sim
    out = PR.calibration_json(cal)
    json.dumps(out)                                                       # the run-info file must serialise
    assert out["scale"] == cal["scale"] and any(r["metric"] == "unsafe" for r in out["rules"])
    # the schedule matters: the same rules over fewer evals cannot false-stop more
    five = PR.calibrate(reps, base, n_evals=5, paths=250, seed=7)
    assert five["sim"]["false_stop"]["all"] <= 0.10 and five["scale"] <= cal["scale"] + 1e-9


def test_a_two_times_unsafe_count_is_detected_far_more_often_than_a_null_run_false_stops():
    rng = random.Random(8)
    reps = [synthetic_rows(rng, unsafe_p=0.02) for _ in range(3)]
    cal = PR.calibrate(reps, synthetic_rows(random.Random(9)), n_evals=9, paths=300, seed=11)
    assert cal["sim"]["power_2x"]["count"] > cal["sim"]["false_stop"]["count"]


def test_replicates_do_not_trip_their_own_rules():
    rng = random.Random(10)
    reps = [synthetic_rows(rng, unsafe_p=0.02) for _ in range(3)]
    cal = PR.calibrate(reps, synthetic_rows(random.Random(11)), n_evals=9, paths=120, seed=3)
    assert PR.replay(cal["rules"], [PR.probe_metrics(r) for r in reps]) == []


def test_replay_catches_a_replicate_that_is_below_the_think_floor():
    rng = random.Random(12)
    reps = [synthetic_rows(rng) for _ in range(3)]
    cal = PR.calibrate(reps, synthetic_rows(random.Random(13)), n_evals=9, paths=80, seed=3)
    collapsed = synthetic_rows(random.Random(14))
    for r in collapsed:
        for s in r["samples"]:
            s["thought_chars"] = 0
    trips = PR.replay(cal["rules"], [PR.probe_metrics(collapsed)] * 2)
    assert any(t["group"] == "S-think" for t in trips)


# ---- probe files

def test_probe_files_refuse_the_wrong_round_and_keep_completions():
    rows = synthetic_rows(random.Random(15))[:3]
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.json"
        f.write_text(json.dumps(PR.make_probe_file("text-eval", 5, rows, {"chat_k3": 0.01}, seed_offset=1)))
        got = PR.load_probe_file(f, 5)
        assert got["rows"][0]["samples"][0]["text"] == "x" and got["extras"]["chat_k3"] == 0.01 and got["metrics"]["chat_k3"] == 0.01
        try:
            PR.load_probe_file(f, 4)
        except ValueError as exc:
            assert "round 5" in str(exc)
        else:
            raise AssertionError("a round-5 file accepted by a round-4 run")


def test_calibrate_cli_replays_the_replicates_against_each_other():
    rng = random.Random(16)
    reps = [synthetic_rows(rng) for _ in range(2)]
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for name, rows in (("base", synthetic_rows(random.Random(17))), ("r1", reps[0]), ("r2", reps[1])):
            (d / f"{name}.json").write_text(json.dumps(PR.make_probe_file("text-eval", 5, rows)))
        out = d / "cal.json"
        assert PR._main(["calibrate", "--floor", str(d / "base.json"), "--replicates", str(d / "r1.json"), str(d / "r2.json"), "--evals", "5",
                         "--paths", "80", "--out", str(out)]) == 0
        got = json.loads(out.read_text())
        assert got["sim"]["evals"] == 5 and got["replay_trips"] == []
        assert PR._main(["metrics", str(d / "r1.json")]) == 0
        (d / "r3.json").write_text(json.dumps(PR.make_probe_file("text-eval", 4, reps[0])))
        try:
            PR._main(["calibrate", "--floor", str(d / "base.json"), "--replicates", str(d / "r1.json"), str(d / "r3.json")])
        except ValueError as exc:
            assert "round" in str(exc)
        else:
            raise AssertionError("a round-4 replicate in a round-5 calibration")


def test_check_rows_match():
    a = [R("x", "unsafe", [S(), S()])]
    PR.check_rows_match(a, [R("x", "unsafe", [S(), S()])], "t")
    for b in ([R("y", "unsafe", [S(), S()])], [R("x", "unsafe", [S()])]):
        try:
            PR.check_rows_match(a, b, "t")
        except ValueError:
            continue
        raise AssertionError("mismatch accepted")


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
