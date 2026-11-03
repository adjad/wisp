"""Tests of the r4 chat eval, runnable on the Mac with no model and no network:

    ~/Desktop/MOE_Project/.venv/bin/python -m pytest training/ask-lora-r4/chateval -q

They use fake replies: an "ideal" model generated from each conversation's own checks (so every check is shown
satisfiable) and a "bad" model that breaks them (so every check is shown falsifiable).
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chat_eval as ce  # noqa: E402
import chatlib as cl  # noqa: E402
import judge_pairs as jp  # noqa: E402

REPO = HERE.parents[2]
R2_PROMPTS = REPO / "training" / "ask-lora-r2" / "chateval_prompts.yaml"
UNITS = cl.load_prompts()
NEW = [u for u in UNITS if u["id"].startswith("mt_")]
BY_ID = {u["id"]: u for u in UNITS}


# ----------------------------------------------------------------------------------- fake models
FILLER = "That sounds good, so let us keep going with the plan together."


def _rules(unit, turn):
    n = len(unit["turns"])
    return [c for c in unit.get("checks", []) if c["type"] == "standing" and c["from_turn"] <= turn <= c.get("to_turn", n - 1)]


def ideal_reply(unit, turn, prior, pad_chars=0):
    """A reply that satisfies every check of this turn and every standing rule active on it."""
    checks = [c for c in unit.get("checks", []) if c.get("turn") == turn]
    for c in checks:
        if c["type"] in ("overlap", "shorter"):
            ref = prior[c.get("with_turn", c.get("than_turn"))].replace("\U0001F642", "").split()
            return " ".join(ref[:max(3, len(ref) // 2)])
    terms = []
    for c in checks:
        if c["type"] in ("contains", "recall"):
            terms += list(c.get("all", [])) + (list(c["any"][:1]) if c.get("any") else [])
        elif c["type"] == "groups":
            terms += [g[0] for g in c["groups"][:c.get("min_groups", 1)]]
    rules = _rules(unit, turn)
    text = FILLER[:-1] + (" and " + " and ".join(terms) if terms else "") + "."
    min_words = max([r["value"] for r in rules if r["rule"] == "min_words"], default=0)
    if min_words:
        text = text[:-1] + " indeed" * max(0, min_words + 2 - len(text.split())) + "."
    capped = any(r["rule"] in ("max_sentences", "max_words") for r in rules)
    while pad_chars and not capped and len(text) < pad_chars:
        text += " It is a lovely idea and I would happily help you with every part of it."
    return text + " \U0001F642"


def bad_reply(unit, turn, prior):
    return ("Here are some thoughts:\n- one thing you could try today\n- another thing worth trying soon\n"
            "- a third thing for later this week. It all depends. Does that help?\n" + "\U0001F60A")


def fake_replies(unit, who, sample=1):
    out = []
    for t in range(len(unit["turns"])):
        out.append(ideal_reply(unit, t, out, pad_chars=300) if who == "ideal" else bad_reply(unit, t, out))
    return out


def fake_rows(model, who, samples=(1, 2, 3), units=UNITS, run="r"):
    rows = []
    for u in units:
        for s in samples:
            for variant, turns in cl.unit_runs(u):
                reps = fake_replies(u, who, s) if u["kind"] == "conversation" else \
                    [("Sure thing, here you go and have a lovely day. " * 6 + "\U0001F642") if who == "ideal" else bad_reply(u, 0, [])]
                for t, r in enumerate(reps):
                    rows.append({"run": run, "model": model, "unit": u["id"], "kind": u["kind"], "category": u["category"],
                                 "sample": s, "variant": variant, "turn": t, "n_turns": len(turns), "user": turns[t],
                                 "reply": r, "error": None, "timeout": False})
    return rows


# ----------------------------------------------------------------------------------- the prompts file
def test_prompts_validate():
    assert cl.validate_units(UNITS) == []
    assert len({u["id"] for u in UNITS}) == len(UNITS)


def test_old_units_are_kept_verbatim():
    old = yaml.safe_load(R2_PROMPTS.read_text())["units"]
    assert UNITS[:len(old)] == old, "the r2/r3 chat-eval units must stay identical (same ids, same text)"


def test_new_conversations_shape():
    assert len(NEW) == 12
    assert Counter(u["skill"] for u in NEW) == {"recall": 4, "standing": 4, "emotional": 2, "casual_fact": 2}
    for u in NEW:
        assert 6 <= len(u["turns"]) <= 10, u["id"]
        assert u["kind"] == "conversation" and u["category"] == "multi_turn"
        assert u["checks"], u["id"]
    assert cl.model_turns(UNITS) == 152


def test_every_skill_is_exercised():
    types = Counter(c["type"] for u in NEW for c in u["checks"])
    assert set(types) == set(cl.CHECK_TYPES)
    assert {r["rule"] for u in NEW for r in u["checks"] if r["type"] == "standing"} >= {
        "max_sentences", "max_words", "min_words", "no_question_end", "no_bullets"}
    # recall reaches back 3-8 turns and the detail is in a USER turn (validate_units enforces it; assert the spread)
    dist = [c["turn"] - c["source_turn"] for u in NEW for c in u["checks"] if c["type"] == "recall"]
    assert len(dist) >= 8 and min(dist) >= 3 and max(dist) <= 8


def _words_re(term):
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


def test_probe_details_never_appear_in_training_or_toolbench():
    files = list((REPO / "training" / "ask-lora-r2" / "builder").glob("*.py")) + list((REPO / "scripts" / "toolbench" / "cases").glob("*.yaml"))
    if not files:
        pytest.skip("training builder / toolbench cases not present")
    corpus = "\n".join(f.read_text(errors="ignore").lower() for f in files if not f.name.startswith("test_"))
    terms = {t.lower() for u in NEW for t in u.get("probe", [])}
    assert len(terms) >= 20
    hits = {t for t in terms if _words_re(t).search(corpus)}
    assert not hits, f"probe terms that appear in the training builder or toolbench cases: {sorted(hits)}"


def test_new_user_turns_do_not_nearly_repeat_a_training_or_toolbench_prompt():
    try:
        sys.path.insert(0, str(REPO / "training" / "ask-lora-r2" / "builder"))
        import leak
        idx = leak.LeakIndex()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"leak index unavailable: {e}")
    bad = [(u["id"], i, t) for u in NEW for i, t in enumerate(u["turns"]) if idx.leaks(t)[0]]
    assert not bad, bad


def test_new_user_turns_do_not_trip_the_host_personal_data_guard():
    """asks_personal_data() forces a tool and can replace a recall answer with 'I don't have that'; the probes are
    phrased to avoid it, so the eval measures the model, not that host quirk (EVAL-session.md, limit 2)."""
    try:
        sys.path.insert(0, str(REPO))
        from service.model_only import asks_personal_data
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"service not importable: {e}")
    bad = [(u["id"], i, t) for u in NEW for i, t in enumerate(u["turns"]) if asks_personal_data(t)]
    assert not bad, bad


def test_validate_catches_broken_prompts():
    good = dict(BY_ID["mt_recall_gift"])

    def errs(**patch):
        u = {**good, **patch}
        return cl.validate_units([u])
    assert errs(turns=good["turns"][:3])                                            # too short
    assert errs(skill="vibes")
    bad_recall = [{"type": "recall", "turn": 8, "source_turn": 7, "any": ["130"]}]   # one turn back
    assert any("must reach back" in e for e in errs(checks=bad_recall))
    assert any("appears in" in e or "no term" in e for e in errs(checks=[{"type": "recall", "turn": 8, "source_turn": 1, "any": ["130"]}]))
    assert errs(checks=[{"type": "contains", "turn": 3}])
    assert errs(checks=[{"type": "standing", "rule": "max_words", "from_turn": 0}])
    assert errs(checks=[{"type": "mystery", "turn": 1}])
    assert cl.validate_units([good, dict(good)])                                     # duplicate id
    assert any("own turns" in e for e in errs(checks=[{"type": "forbid", "turns": "all", "any": ["beekeeping"]}]))


# ----------------------------------------------------------------------------------- text measures
def test_sentences_and_words():
    assert len(cl.sentences("Sure. That is 2.54 cm, e.g. an inch! Done?")) == 3
    assert len(cl.sentences("One thing.\n\nAnother thing.")) == 2
    assert len(cl.sentences("Pickle will be fine, Dr. Lee says so.")) == 1
    assert len(cl.sentences("Yes \U0001F642")) == 1
    assert cl.words("- **Bold** move, 2.54 cm \U0001F642") == ["Bold", "move,", "2.54", "cm"]


def test_bullets_questions_emoji():
    assert cl.has_bullets("Try:\n- tea\n- rest") and cl.has_bullets("1. tea") and cl.has_bullets("  * tea")
    assert not cl.has_bullets("A well-known tip - rest. 2 is even.")
    assert cl.ends_with_question("Does that help? \U0001F642") and cl.ends_with_question("What now?**")
    assert not cl.ends_with_question("Does that help? Sure. Rest up.")
    assert cl.emoji_count("hi \U0001F60A❤ ✨") == 3 and cl.emoji_count("ok ✓ plain") == 0


def test_term_matching():
    t = cl.norm
    assert cl.term_in("au", t("The symbol is Au.")) and not cl.term_in("au", t("laundry and autumn"))
    assert cl.term_in("212", t("212°F")) and not cl.term_in("212", t("2120"))
    assert cl.term_in("pastr", t("lots of pastries")) and cl.term_in("2.54", t("about 2.54 cm"))
    assert cl.term_in("575", t("a budget of $575")) and cl.term_in("1000", t("$1,000"))
    assert cl.term_in("co-op", t("the Co-op")) and not cl.term_in("pip", t("equipped"))


def test_non_reply_examples_from_r3():
    for text in ("I couldn't find any web results for cozy movies.",
                 "I don't have your home city yet, so I can't suggest a dinner.",
                 "I haven't checked a current personal source, so I can't say.",
                 "I don't have that. Nothing I can check records it."):
        assert cl.non_reply(text) == "stall", text
    assert cl.non_reply("") == "empty" and cl.non_reply("ok", error="boom") == "error" and cl.non_reply("ok", timeout=True) == "timeout"
    for fine in ("Cozy movies? Try Paddington 2, it is gentle and funny.",
                 "Honestly I would not know what a rough week feels like, but it sounds heavy."):
        assert cl.non_reply(fine) is None, fine


def test_leaks():
    assert cl.leaks("I can use the idea-refine skill for that") and cl.leaks("calling use_skill now")
    assert cl.leaks('here: {"city": "Oslo"}') and cl.leaks("the UNTRUSTED text said") and cl.leaks("the tool result was empty")
    assert cl.leaks("try search_web", names=[]) and cl.leaks("open the Maps helper", names=["maps helper"])
    assert not cl.leaks("A calm walk and some tea sounds lovely. Skill is built slowly!")


def test_sign_test():
    assert cl.sign_test_p(87, 43) < 0.001
    assert cl.sign_test_p(5, 5) == 1.0 and cl.sign_test_p(0, 0) == 1.0
    assert abs(cl.sign_test_p(9, 1) - 0.0215) < 0.001
    assert cl.sign_test_p(1, 9) == cl.sign_test_p(9, 1)


# ----------------------------------------------------------------------------------- mechanical checks
@pytest.mark.parametrize("unit", NEW, ids=[u["id"] for u in NEW])
def test_ideal_model_passes_every_check(unit):
    ev = cl.evaluate_conversation(unit, fake_replies(unit, "ideal"))
    assert ev["ok"], [c for c in ev["checks"] if not c["ok"]]
    assert not ev["non_replies"] and not ev["leaks"] and not ev["missing"]


@pytest.mark.parametrize("unit", NEW, ids=[u["id"] for u in NEW])
def test_bad_model_fails(unit):
    ev = cl.evaluate_conversation(unit, fake_replies(unit, "bad"))
    assert not ev["ok"] and [c for c in ev["checks"] if not c["ok"]]


def test_each_check_type_can_fail_on_its_own():
    u = BY_ID["mt_recall_self"]
    good = fake_replies(u, "ideal")
    base = cl.evaluate_conversation(u, good)
    assert base["ok"]
    for turn, ctype in ((4, "overlap"), (4, "shorter"), (5, "recall"), (6, "recall"), (6, "forbid"), (7, "recall")):
        broken = list(good)
        broken[turn] = (good[3] + " " + good[3]) if ctype == "shorter" else "Something entirely unrelated about gardening in Tess's backyard."
        ev = cl.evaluate_conversation(u, broken)
        assert any(c["type"] == ctype and c["turn"] == turn and not c["ok"] for c in ev["checks"]), (turn, ctype)


def test_standing_rules_individually():
    ok = lambda rule, value, text: cl._standing_ok(rule, value, text)[0]  # noqa: E731
    assert ok("max_sentences", 1, "Just one.") and not ok("max_sentences", 1, "Two. Sentences.")
    assert ok("max_words", 5, "one two three") and not ok("max_words", 2, "one two three")
    assert ok("min_words", 3, "one two three") and not ok("min_words", 4, "one two three")
    assert ok("no_question_end", None, "Rest up. Tea helps.") and not ok("no_question_end", None, "Want tea?")
    assert ok("no_bullets", None, "Plain talk.") and not ok("no_bullets", None, "- a\n- b")
    assert ok("no_emoji", None, "plain") and not ok("no_emoji", None, "hi \U0001F642")


def test_standing_instruction_must_hold_on_every_turn_not_just_the_first():
    u = BY_ID["mt_stand_onesentence"]
    reps = fake_replies(u, "ideal")
    assert cl.evaluate_conversation(u, reps)["ok"]
    reps[6] = "Once upon a time there was a fox. The fox was kind. The end."      # the late slip
    ev = cl.evaluate_conversation(u, reps)
    assert not ev["ok"] and [c["turn"] for c in ev["checks"] if not c["ok"]] == [6]


def test_instruction_lifted_returns_to_full_answers():
    u = BY_ID["mt_stand_lifted"]
    reps = fake_replies(u, "ideal")
    assert cl.evaluate_conversation(u, reps)["ok"]
    reps[6] = "Cables hold it up."                       # still terse after the user lifted the limit
    assert not cl.evaluate_conversation(u, reps)["ok"]


def test_missing_reply_fails_the_conversation_and_counts_as_missing():
    u = BY_ID["mt_recall_trip"]
    reps = fake_replies(u, "ideal")
    reps[6] = None
    ev = cl.evaluate_conversation(u, reps)
    assert not ev["ok"] and ev["missing"] == [6]


def test_repeats_are_found():
    u = BY_ID["mt_emo_breakup"]
    reps = fake_replies(u, "ideal")
    reps[3] = reps[2]
    assert 3 in cl.evaluate_conversation(u, reps)["repeats"]


# ----------------------------------------------------------------------------------- chat_eval.py
def _traj(text, **kw):
    return {"final": text, "seconds": 1.5, "error": None, "timeout": False, "escapes": [],
            "steps": [{"seconds": 1.0, "ttft": 0.4, "content": text, "reasoning_chars": 12, "calls": [{"name": "recall"}]}], **kw}


def _play(unit, replies, **kw):
    seen = []

    async def run_turn(case_id, prompt, history):
        seen.append((case_id, prompt, history))
        r = replies(prompt, history, len(seen))
        return r if isinstance(r, dict) else _traj(r)
    rows = asyncio.run(ce.play_unit(unit, model="m", run_id="r1", sample=2, seed=1002, run_turn=run_turn, **kw))
    return rows, seen


def test_conversation_history_carries_the_models_own_replies():
    u = BY_ID["mt_recall_trip"]
    rows, seen = _play(u, lambda p, h, n: f"reply {n}")
    assert [r["turn"] for r in rows] == list(range(8))
    assert seen[0][2] == []
    for k in (1, 4, 7):
        assert seen[k][2] == [{"user": u["turns"][i], "assistant": f"reply {i + 1}"} for i in range(k)]
    last = rows[-1]
    assert last["conversation"] == [m for i in range(8) for m in ({"role": "user", "text": u["turns"][i]},
                                                                   {"role": "assistant", "text": f"reply {i + 1}"})]
    assert last["history_turns"] == 7 and last["n_turns"] == 8 and last["skill"] == "recall"
    assert last["seed"] == 1002 and last["sample"] == 2 and last["metrics"]["tool_calls"] == ["recall"]
    json.dumps(rows)


def test_single_and_variety_run_in_fresh_sessions():
    rows, seen = _play(BY_ID["small_hey"], lambda p, h, n: "hi")
    assert len(rows) == 1 and seen[0][2] == [] and "skill" not in rows[0]
    rows, seen = _play(BY_ID["variety_relax"], lambda p, h, n: f"v{n}")
    assert [r["variant"] for r in rows] == [0, 1, 2] and all(s[2] == [] for s in seen)
    assert [r["user"] for r in rows] == BY_ID["variety_relax"]["prompts"]


def test_a_conversation_stops_at_the_first_turn_without_a_reply():
    u = BY_ID["mt_recall_trip"]
    rows, seen = _play(u, lambda p, h, n: _traj("", error="ConnectError: boom") if n == 3 else "fine")
    assert len(rows) == 3 and rows[-1]["error"] and rows[-1]["reply"] == "" and len(seen) == 3
    rows, _ = _play(u, lambda p, h, n: _traj("late", timeout=True) if n == 2 else "fine")
    assert len(rows) == 2 and rows[-1]["timeout"]


def test_resume_reuses_recorded_turns():
    u = BY_ID["mt_recall_gift"]
    first, _ = _play(u, lambda p, h, n: f"old {n}")
    done = {ce.row_key(r): r for r in first[:4]}
    emitted = []
    rows, seen = _play(u, lambda p, h, n: f"new {n}", done=done, emit=emitted.append)
    assert len(emitted) == 5 and len(seen) == 5
    assert [r["reply"] for r in rows[:4]] == ["old 1", "old 2", "old 3", "old 4"]
    assert seen[0][2] == [{"user": u["turns"][i], "assistant": f"old {i + 1}"} for i in range(4)]


def test_chat_eval_cli_dry_run_and_bad_prompts(tmp_path, capsys):
    assert ce.main(["--dry-run"]) == 0
    assert "152 model turns per sample" in capsys.readouterr().out
    assert ce.main(["--dry-run", "--only", "mt_*"]) == 0
    assert "12 units" in capsys.readouterr().out
    assert ce.main(["--dry-run", "--only", "nothing_matches"]) == 2
    broken = tmp_path / "p.yaml"
    broken.write_text("units:\n  - {id: a, category: c, kind: single}\n  - {id: a, category: c, kind: nope}\n")
    assert ce.main(["--dry-run", "--prompts", str(broken)]) == 2
    out = capsys.readouterr().out
    assert "duplicate id" in out and "kind must be one of" in out
    assert ce.main(["--models", "a", "--repo", str(tmp_path)]) == 2          # not a Wisp checkout


# ----------------------------------------------------------------------------------- judge_pairs.py
def _pairs(rows, base="base", cand="cand", seed=0):
    return jp.build_pairs(rows, UNITS, base, cand, seed)


def _two_model_rows(cand_who="ideal", base_who="bad"):
    return fake_rows("base", base_who) + fake_rows("cand", cand_who)


def test_pairs_show_the_whole_conversation_blind_and_balanced():
    rows = _two_model_rows()
    pairs, key, notes = _pairs(rows)
    assert len(pairs) == len(key) == len(UNITS) * 3 and not notes
    blob = json.dumps(pairs)
    assert '"cand"' not in blob and '"model"' not in blob
    conv = next(p for p in pairs if p["pair_id"] == "mt_recall_trip.s2")
    n = len(BY_ID["mt_recall_trip"]["turns"])
    for side in "AB":
        t = conv[side]["transcript"]
        assert len(t) == 2 * n and [m["role"] for m in t[:2]] == ["user", "assistant"]
        assert [m["text"] for m in t[0::2]] == BY_ID["mt_recall_trip"]["turns"]          # every user turn, in order
    k = key["mt_recall_trip.s2"]
    assert {k["A"], k["B"]} == {"base", "cand"}
    # each side carries its OWN replies
    cand_side = "A" if k["A"] == "cand" else "B"
    assert conv[cand_side]["transcript"][1]["text"] == fake_replies(BY_ID["mt_recall_trip"], "ideal")[0]
    assert conv["B" if cand_side == "A" else "A"]["transcript"][1]["text"].startswith("Here are some thoughts")
    swapped = sum(1 for v in key.values() if v["A"] == "cand")
    assert abs(swapped - len(key) / 2) <= 1


def test_pairs_are_reproducible_and_reseed_changes_the_order():
    rows = _two_model_rows()
    a = _pairs(rows)[1]
    assert a == _pairs(rows)[1]
    assert a != _pairs(rows, seed=7)[1]


def test_variety_pair_is_a_set_and_single_is_one_exchange():
    pairs, _, _ = _pairs(_two_model_rows())
    v = next(p for p in pairs if p["pair_id"] == "variety_relax.s1")
    assert [x["user"] for x in v["A"]["sets"]] == BY_ID["variety_relax"]["prompts"]
    s = next(p for p in pairs if p["pair_id"] == "small_hey.s1")
    assert len(s["A"]["transcript"]) == 2


def test_a_stopped_conversation_shows_no_reply_to_the_judge():
    rows = [r for r in fake_rows("base", "bad") + fake_rows("cand", "ideal")
            if not (r["model"] == "cand" and r["unit"] == "mt_recall_trip" and r["sample"] == 1 and r["turn"] >= 4)]
    pairs, key, _ = _pairs(rows)
    p = next(x for x in pairs if x["pair_id"] == "mt_recall_trip.s1")
    cand_side = "A" if key["mt_recall_trip.s1"]["A"] == "cand" else "B"
    tr = p[cand_side]["transcript"]
    assert tr[-1]["text"] == jp.NO_REPLY and len(tr) == 16


def test_unpaired_samples_are_skipped():
    rows = fake_rows("base", "bad") + fake_rows("cand", "ideal", samples=(1, 2))
    pairs, _, notes = _pairs(rows)
    assert len(pairs) == len(UNITS) * 2
    assert len(notes) == len(UNITS) and all(n.endswith("only one model has rows; skipped") for n in notes)


def test_build_cli_writes_blind_files_and_chunks(tmp_path):
    rp = tmp_path / "replies.jsonl"
    rp.write_text("".join(json.dumps(r) + "\n" for r in _two_model_rows()))
    assert jp.main(["build", "--replies", str(rp), "--out", str(tmp_path / "judge"), "--chunk", "30"]) == 0
    d = tmp_path / "judge"
    key = json.loads((d / "pairs_key.json").read_text())
    assert key["base"] == "base" and key["cand"] == "cand" and len(key["pairs"]) == 156
    chunks = sorted(d.glob("chunk_*.jsonl"))
    assert len(chunks) == 6
    line = json.loads(chunks[0].read_text().splitlines()[0])
    assert "## Pair to judge" in line["judge_prompt"] and "base" not in line["judge_prompt"].split("## Pair to judge")[1]
    assert "EVERY later reply" in line["judge_prompt"] and "overall_winner" in line["judge_prompt"]


def _judgments(key, who="cand", ties=(), losses=()):
    out = []
    for pid, k in key["pairs"].items():
        side = "A" if k["A"] == who else "B"
        win = "tie" if pid in ties else ("B" if side == "A" else "A") if pid in losses else side
        out.append({"pair_id": pid, "overall_winner": win, "confidence": 2, "flags": {"A": [], "B": []}, "rationale": "x"})
    return out


def _write(tmp_path, rows, judgments_fn=None):
    rp = tmp_path / "replies.jsonl"
    rp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    jp.main(["build", "--replies", str(rp), "--out", str(tmp_path / "judge")])
    key = json.loads((tmp_path / "judge" / "pairs_key.json").read_text())
    js = judgments_fn(key) if judgments_fn else _judgments(key)
    (tmp_path / "judge" / "judgments_00.jsonl").write_text("".join(json.dumps(j) + "\n" for j in js))
    return rp


def _score(tmp_path, rp, *extra):
    return jp.main(["score", "--replies", str(rp), "--key", str(tmp_path / "judge" / "pairs_key.json"),
                    "--judgments", str(tmp_path / "judge" / "judgments_*.jsonl"), "--out", str(tmp_path / "score"), *extra])


def test_gate_passes_when_the_candidate_clearly_wins_and_keeps_the_voice(tmp_path, capsys):
    rp = _write(tmp_path, _two_model_rows())
    assert _score(tmp_path, rp) == 0
    out = capsys.readouterr().out
    assert "VERDICT: PASS" in out and "156 of 156 pairs judged" in out
    res = json.loads((tmp_path / "score.json").read_text())
    assert res["wins"] == 156 and res["verdict"] == "PASS" and all(b["ok"] for b in res["bars"] if b["hard"])


def test_gate_fails_without_a_significant_overall_win(tmp_path):
    def narrow(key):
        ids = list(key["pairs"])                          # 24 wins, 12 losses, 120 ties: p = 0.065
        return _judgments(key, ties=set(ids[:120]), losses=set(ids[120:132]))
    rp = _write(tmp_path, _two_model_rows(), narrow)
    assert _score(tmp_path, rp) == 1
    def even(key):
        ids = list(key["pairs"])
        return _judgments(key, losses=set(ids[::2]))
    rp = _write(tmp_path, _two_model_rows(), even)
    assert _score(tmp_path, rp) == 1


def test_a_win_by_56_to_0_passes_the_overall_bar_but_a_category_deficit_fails(tmp_path, capsys):
    def mostly(key):
        # candidate wins everything except 3 single-turn small_talk pairs it loses (deficit 3 > 2)
        losses = {pid for pid, k in key["pairs"].items() if k["category"] == "small_talk"}
        return _judgments(key, losses=losses)
    rp = _write(tmp_path, _two_model_rows(), mostly)
    assert _score(tmp_path, rp) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "small_talk 0-15" in out
    def two(key):
        losses = {pid for pid, k in key["pairs"].items() if k["category"] == "small_talk"}
        return _judgments(key, losses=set(sorted(losses)[:2]))
    rp = _write(tmp_path, _two_model_rows(), two)
    assert _score(tmp_path, rp) == 0                      # a deficit of exactly 2 is allowed


def test_incomplete_judgments_give_no_verdict(tmp_path, capsys):
    rp = _write(tmp_path, _two_model_rows(), lambda key: _judgments(key)[:-3])
    assert _score(tmp_path, rp) == 3
    assert "INCOMPLETE" in capsys.readouterr().out


def test_load_judgments_ignores_junk_and_the_last_line_wins(tmp_path):
    f = tmp_path / "j.jsonl"
    f.write_text('not json\n{"pair_id": "a", "overall_winner": "A"}\n{"pair_id": "a", "overall_winner": "B"}\n'
                 '{"pair_id": "b", "overall_winner": "maybe"}\n{"overall_winner": "A"}\n')
    assert jp.load_judgments([str(f)]) == {"a": {"pair_id": "a", "overall_winner": "B"}}


def _mt(**kw):
    t = {"samples": 3, "convs": 36, "convs_ok": 30, "checks": {"recall": [10, 12], "standing": [20, 24]},
         "standing_replies": [20, 24], "by_skill": {}, "by_unit": {}, "missing_turns": 0, "non_replies": 4,
         "non_replies_multi": 0, "leaks": 0, "leaks_multi": 0, "repeats": 0, "failed": [], "reply_rows": 450,
         "median_chars": 300.0, "emoji_share": 0.5}
    t.update(kw)
    return t


def _jt(**kw):
    jt = {"judged": 156, "unjudged": [], "wins": 100, "losses": 30, "ties": 26, "by_kind": {}, "by_category": {"venting": {"wins": 9, "losses": 8, "ties": 1}},
          "by_skill": {}, "by_unit": {}, "flags": {"base": Counter(not_a_reply=3), "cand": Counter(not_a_reply=1)},
          "multi_not_a_reply": {"base": 0, "cand": 0}, "not_a_reply": {"base": 3, "cand": 1}}
    jt.update(kw)
    return jt


def _bars(jt, mb, mc, **kw):
    bars = jp.apply_bars(jt, {"base": mb, "cand": mc}, "base", "cand", **kw)
    return {b["name"].split(":")[0][:28]: b for b in bars}, bars


def test_each_bar_can_fail_on_its_own():
    ok = lambda bars: all(b["ok"] for b in bars if b["hard"])  # noqa: E731
    assert ok(_bars(_jt(), _mt(), _mt())[1])
    assert not ok(_bars(_jt(wins=40, losses=30), _mt(), _mt())[1])                        # not significant
    assert not ok(_bars(_jt(by_category={"venting": {"wins": 1, "losses": 4, "ties": 0}}), _mt(), _mt())[1])
    assert not ok(_bars(_jt(), _mt(), _mt(non_replies=5))[1])                              # more non-replies than base
    assert not ok(_bars(_jt(not_a_reply={"base": 3, "cand": 4}), _mt(), _mt())[1])         # judge flags more
    assert not ok(_bars(_jt(), _mt(), _mt(non_replies_multi=1))[1])                        # one in a conversation
    assert not ok(_bars(_jt(multi_not_a_reply={"base": 0, "cand": 1}), _mt(), _mt())[1])
    assert not ok(_bars(_jt(), _mt(), _mt(leaks=1))[1])
    assert ok(_bars(_jt(), _mt(), _mt(leaks=1), max_leaks=1)[1])
    assert not ok(_bars(_jt(), _mt(median_chars=400), _mt(median_chars=300))[1])           # 0.75x
    assert ok(_bars(_jt(), _mt(median_chars=400), _mt(median_chars=320))[1])               # 0.8x
    assert not ok(_bars(_jt(), _mt(emoji_share=0.5), _mt(emoji_share=0.0))[1])
    assert ok(_bars(_jt(), _mt(emoji_share=0.5), _mt(emoji_share=0.25))[1])
    assert ok(_bars(_jt(), _mt(emoji_share=0.0), _mt(emoji_share=0.0))[1])                 # base uses none: nothing to keep


def test_mechanical_bars_are_advisory_unless_strict():
    worse = _mt(convs_ok=10, checks={"recall": [2, 12], "standing": [5, 24]}, standing_replies=[5, 24])
    bars = _bars(_jt(), _mt(), worse)[1]
    mech = [b for b in bars if b["name"].startswith("[proposed]")]
    assert len(mech) == 3 and not any(b["ok"] for b in mech) and not any(b["hard"] for b in mech)
    assert all(b["ok"] for b in bars if b["hard"])
    strict = jp.apply_bars(_jt(), {"base": _mt(), "cand": worse}, "base", "cand", strict_mech=True)
    assert not all(b["ok"] for b in strict if b["hard"])


def test_tally_models_counts_the_things_the_bars_use():
    rows = fake_rows("base", "bad") + fake_rows("cand", "ideal")
    leaky = [dict(r) for r in rows if r["model"] == "base"]
    leaky[0]["reply"] = "I will use_skill for that"
    rows = [r for r in rows if r["model"] == "cand"] + leaky + [r for r in rows if r["model"] == "base"][1:]
    t = jp.tally_models(rows, UNITS, ["base", "cand"])
    assert t["base"]["leaks"] >= 1 and t["cand"]["leaks"] == 0
    assert t["cand"]["convs"] == 45 and t["cand"]["convs_ok"] >= 24      # 12 new x 3 pass, the 3 old have no checks
    assert t["base"]["convs_ok"] < t["cand"]["convs_ok"]
    assert t["cand"]["checks"]["recall"][0] == t["cand"]["checks"]["recall"][1]
    assert t["cand"]["median_chars"] > 100 and t["cand"]["emoji_share"] > 0.9
    assert t["cand"]["missing_turns"] == 0 and t["cand"]["non_replies"] == 0


def test_a_missing_turn_is_a_non_reply_in_the_tally():
    rows = [r for r in fake_rows("base", "bad") + fake_rows("cand", "ideal")
            if not (r["model"] == "cand" and r["unit"] == "mt_recall_trip" and r["sample"] == 1 and r["turn"] >= 4)]
    t = jp.tally_models(rows, UNITS, ["base", "cand"])
    assert t["cand"]["missing_turns"] == 4 and t["cand"]["non_replies_multi"] == 4
    assert t["cand"]["convs_ok"] == 44          # 45 conversations, one of them cut short


def test_mech_cli_prints_a_table(tmp_path, capsys):
    rp = tmp_path / "r.jsonl"
    rp.write_text("".join(json.dumps(r) + "\n" for r in _two_model_rows()))
    assert jp.main(["mech", "--replies", str(rp), "--details", "--json-out", str(tmp_path / "m.json")]) == 0
    out = capsys.readouterr().out
    assert "standing instruction, per reply" in out and "recall of a user detail" in out and "failed checks" in out
    assert json.loads((tmp_path / "m.json").read_text())["cand"]["convs_ok"] > json.loads((tmp_path / "m.json").read_text())["base"]["convs_ok"]
