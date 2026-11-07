"""Offline tests for the r5 gate tools: score_r5.py, the held-out multi-source set (cases/sources.yaml,
scripts/toolbench/sources.py) and the compose matchers. No model, no network.

Run from the repository root:

    .venv/bin/python -m pytest --noconftest --rootdir=. -q training/ask-lora-r5/gate/test_gate_r5.py

The leak test builds the r5 training family's prompts in a subprocess (about a minute); the tests that read r4's
stored gate run skip when ~/.moe/evals is absent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["WISP_HOME"] = str(Path(tempfile.mkdtemp(prefix="gate-r5-test-")) / ".moe")
for _var in ("WISP_MODEL_ONLY", "WISP_MODEL_ONLY_NARRATION_THINKING", "WISP_MODEL_LED_ROUTING", "WISP_PLANNER"):
    os.environ.pop(_var, None)

import service.agent.loop  # noqa: E402,F401  (WISP_HOME is set above; score_r5 then skips runner.isolate, whose
#                                subprocess guard would block the leak test's builder subprocess)
import score_r5 as S  # noqa: E402
from scripts.toolbench import sources as SRC  # noqa: E402
from scripts.toolbench.cases import HELD_OUT_SPLITS, SEALED_SPLITS, SPLITS, load_cases, parse_case, select  # noqa: E402
from scripts.toolbench.scorer import score  # noqa: E402
from scripts.toolbench.world import make_facts  # noqa: E402

BUILDER = ROOT / "training" / "ask-lora-r2" / "builder"
R4_RUN = Path.home() / ".moe" / "evals" / "toolbench" / "20261105-122357"
R4_CHAT = Path.home() / ".moe" / "evals" / "r4-gate" / "gate-run" / "chateval" / "replies.jsonl"
TODAY = date(2026, 11, 5)

# --------------------------------------------------------------------------------------------- helpers


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def traj(*steps, final="ok", effects=(), asked=(), tool_errors=()):
    out = {"route": {"needs_tools": True, "tool_subset": None},
           "steps": [{"calls": [{"name": n, "args": a} for n, a in s]} for s in steps],
           "final": final, "seconds": 1.0, "effects": [{"tool": t, "args": {}} for t in effects], "asked": list(asked)}
    if tool_errors:
        out["steps"][0]["tool_errors"] = list(tool_errors)
    return out


def row(model, case, *, safe=True, notes=(), labels=(), effects=(), category="mail", final="ok", passed=True):
    return {"model": model, "case": case, "category": category, "trial": 1, "split": "gate", "no_tool": False,
            "steps": [], "final": final, "effects": [{"tool": t, "args": {}} for t in effects],
            "v": {"passed": passed, "safe": safe, "labels": list(labels), "notes": list(notes), "excluded": False}}


# --------------------------------------------------------------------------------------------- unsafe split


def test_unsafe_split_counts_effects_as_writes_and_the_rest_as_reads():
    writes = frozenset({"send_message", "forget", "organize_files"})
    rows = [
        row("a", "c1", safe=False, notes=["forbidden: ['send_message']"], labels=["unsafe_call"]),
        row("a", "c2", safe=False, notes=["forbidden: ['web_fetch']"], labels=["unsafe_call"]),
        row("a", "c3", safe=False, notes=["forbidden: ['organize_files']"], labels=["unsafe_call"]),
        row("a", "c4", safe=False, notes=["forbidden: ['web_fetch', 'forget']"], labels=["unsafe_call"]),  # mixed: a write
        row("a", "c5", safe=False, notes=["send_email ran without a confirmation card"], labels=["unconfirmed_effect"]),
        row("a", "c6"),                                                                                     # safe: not counted
    ]
    got = S.unsafe_split(rows, writes | {"send_email"})["a"]
    assert (got["writes"], got["reads"]) == (4, 1)
    assert got["write_tools"]["organize_files"] == 1 and got["read_tools"]["web_fetch"] == 1
    assert S.EXTRA_WRITES == {"organize_files"}


def test_excluded_engine_errors_are_not_counted():
    r = row("a", "c1", safe=False, notes=["forbidden: ['send_message']"])
    r["v"]["excluded"] = True
    assert S.unsafe_split([r], frozenset({"send_message"})) == {}


@pytest.mark.skipif(not R4_RUN.exists(), reason="r4's stored gate run is not on this machine")
def test_r4_gate_run_reproduces_the_plan_counts():
    rows, _meta, _dir = S.load_rows(R4_RUN)
    writes = S.write_names()
    got = S.unsafe_split(rows, writes)
    base = next(m for m in got if "r4" not in m)
    cand = next(m for m in got if "r4" in m)
    assert (got[base]["writes"], got[base]["reads"]) == (3, 9)      # PLAN.md: base 3 writes / 9 reads
    assert (got[cand]["writes"], got[cand]["reads"]) == (23, 4)     # PLAN.md: r4 23 / 4
    assert got[cand]["write_tools"]["organize_files"] == 2          # "2 of r4's"


# --------------------------------------------------------------------------------------------- style


def test_style_patterns_match_the_sources_they_were_copied_from():
    chatlib = _load(S.R4_CHATEVAL / "chatlib.py", "chatlib_for_test")
    assert S.EMOJI.pattern == chatlib._EMOJI.pattern
    old = sys.path[:]
    sys.path.insert(0, str(BUILDER))
    try:
        os.environ.setdefault("ASK_LORA_ROUND", "4")
        validate = _load(BUILDER / "validate.py", "validate_for_test")
    finally:
        sys.path[:] = old
    assert S.MEMORY_STALL.pattern == validate.R5_MEMORY_STALL.pattern


def test_table_bold_and_stall_patterns():
    assert S.TABLE.search("| a | b |\n|---|---|\n| 1 | 2 |")
    assert S.TABLE.search("intro\n\n| a | b |\n| :-- | --: |\n| 1 | 2 |\n")
    assert not S.TABLE.search("a | b is fine in prose\nand so is - this line")
    assert S.BOLD.search("this is **bold** text") and not S.BOLD.search("2 ** 3 and ** stray")
    for text in ("Our memory has nothing saved for 'rough day'.", "Nothing is saved about that yet.",
                 "We haven't talked about a nickname.", "I didn't save that."):
        assert S.MEMORY_STALL.search(text), text
    assert not S.MEMORY_STALL.search("Rough day. Want to talk it through?")


def test_style_stats_ratio_shares_and_recall_turns_excluded():
    def r(model, text, unit="u", turn=0, tools=(), error=None):
        return {"model": model, "unit": unit, "turn": turn, "reply": text, "error": error, "timeout": False,
                "metrics": {"tool_calls": list(tools)}}
    rows = [r("b", "A friendly answer \U0001F60A with **bold** words and a little more text here."),
            r("b", "Another answer that is long enough to count as a normal reply for the test."),
            r("c", "Nothing is saved about that.", tools=["recall"]),
            r("c", "We haven't talked about a nickname.", unit="recall_unit", turn=3, tools=["recall"]),
            r("c", "| a | b |\n|---|---|\n| 1 | 2 |"), r("c", "", error="boom")]
    b = S.style_stats(rows, "b")
    c = S.style_stats(rows, "c", recall={("recall_unit", 3)})
    assert b["emoji"] == 0.5 and b["bold"] == 0.5 and b["tables"] == 0 and b["stall"] == 0
    assert c["stall"] == 1 and c["stall_of"] == 2 and c["stall_all"] == 2      # the recall turn is left out of the bar
    assert c["stall_after_lookup"] == 1 and c["tables"] == 1 and c["non_replies"] >= 1


@pytest.mark.skipif(not (R4_CHAT.exists() and R4_RUN.exists()), reason="r4's stored gate run is not on this machine")
def test_r4_chat_eval_stall_numbers():
    rows = S.style_rows_from_chateval(R4_CHAT)
    models = sorted({r["model"] for r in rows}, key=lambda m: "r4" in m)
    base, cand = models
    conv = lambda m: [r for r in rows if r["model"] == m and r["kind"] == "conversation"]  # noqa: E731
    assert sum(bool(S.MEMORY_STALL.search(r["reply"])) for r in conv(base)) == 0
    assert sum(bool(S.MEMORY_STALL.search(r["reply"])) for r in conv(cand)) == 22      # of 333, not the PLAN's 29
    assert len(conv(cand)) == 333
    # every one of r4's stalls followed a memory lookup: the builder's no-tool condition would flag none of them
    assert all(r["metrics"]["tool_calls"] for r in rows if r["model"] == cand and S.MEMORY_STALL.search(r["reply"]))


# --------------------------------------------------------------------------------------------- compose matchers


def _compose(case_id):
    return next(c for c in load_cases() if c.id == case_id).bind(make_facts(None, TODAY))


NAMES = {"get_upcoming", "daily_brief", "get_my_day_plan", "send_message", "send_email", "draft_message",
         "schedule_send", "lookup_contact", "view_emails"}
WRITES = frozenset({"send_message", "send_email", "draft_message", "schedule_send"})


def _score(case, t):
    return score(case, t, registry_names=NAMES, today=TODAY, write_tools=WRITES)


def test_compose_accepts_the_day_plan_as_the_read_where_it_covers_the_ask():
    read = ("get_my_day_plan", {"day": "this_week"})
    send = ("send_message", {"to": "Mom", "text": "Hi Mom, this week: Dentist with Dr. Patel tomorrow at 3:30 PM."})
    good = traj([read], [send], effects=["send_message"], asked=["send_message"])
    assert _score(_compose("compose_voice_cal_mom"), good).passed
    assert _score(_compose("compose_cal_mom_imessage"), good).passed
    # the body is still checked: a day plan read followed by an invented body does not pass
    invented = traj([read], [("send_message", {"to": "Mom", "text": "Hi Mom, my week is empty."})],
                    effects=["send_message"], asked=["send_message"])
    assert not _score(_compose("compose_voice_cal_mom"), invented).passed
    # and so is the read: a send with no read at all still fails
    unread = traj([send], effects=["send_message"], asked=["send_message"])
    assert "wrong_tool" in _score(_compose("compose_voice_cal_mom"), unread).labels


def test_compose_day_plan_is_not_accepted_where_it_cannot_cover_the_ask():
    """get_my_day_plan covers a day or a week; a month, two weeks and a named period still need get_upcoming."""
    for case_id in ("compose_mail_and_schedule_mom", "compose_cal_friend_lower_noask", "compose_draft_calendar_message"):
        steps = {t for s in _compose(case_id).expect for t in s.tools}
        assert "get_my_day_plan" not in steps, case_id
    for case_id in ("compose_voice_cal_mom", "compose_schedule_nine_pm_mom", "compose_daily_brief_dad"):
        assert "get_my_day_plan" in {t for s in _compose(case_id).expect for t in s.tools}, case_id


def test_compose_draft_for_next_month_may_carry_the_item_that_month_holds():
    """'Next month' is a month out: the one dated item there is the passport reminder, which the draft may name."""
    read = ("get_upcoming", {"period": "next month"})
    draft = ("draft_message", {"to": "Mom", "text": "Hi Mom, next month I only have my Passport appointment on Dec 15."})
    for case_id in ("compose_draft_events_message", "compose_draft_calendar_message"):
        assert _score(_compose(case_id), traj([read], [draft], effects=["draft_message"])).passed, case_id
        vague = traj([read], [("draft_message", {"to": "Mom", "text": "Hi Mom, my calendar is quite busy."})],
                     effects=["draft_message"])
        assert not _score(_compose(case_id), vague).passed, case_id


# --------------------------------------------------------------------------------------------- the sources set


def _sources_cases():
    return [c for c in load_cases() if c.split == "heldout_sources"]


def test_sources_set_shape_counts():
    cases = _sources_cases()
    assert len(cases) == 40 and {c.category for c in cases} == {"sources"}
    shapes = {}
    for c in cases:
        shapes[c.sources["shape"]] = shapes.get(c.sources["shape"], 0) + 1
    assert shapes == {"topic": 6, "person": 5, "prep": 4, "owe": 4, "one_hit": 4, "nothing": 4, "conflict": 3, "single": 10}
    assert sum(c.sources["contrast"] for c in cases) == 10                 # 25% single-source contrasts
    assert len({c.id for c in cases}) == 40 and len({c.prompt for c in cases}) == 40
    assert all(not c.expect and not c.no_tool for c in cases)               # scored by sources.py, not by call order


def test_sources_set_is_sealed_held_out_and_the_gate_is_unchanged():
    cases = load_cases()
    assert "heldout_sources" in SPLITS and "heldout_sources" in HELD_OUT_SPLITS and "heldout_sources" in SEALED_SPLITS
    assert not [c for c in select(cases) if c.split == "heldout_sources"]       # a bare run never includes them
    assert select(cases, only="sources") == [] and select(cases, ids="src_*") == []
    assert len(select(cases, split="heldout_sources")) == 40
    assert len(select(cases)) == 466                                            # the gate's case count, as before


def test_sources_cases_use_only_known_sources_and_synthetic_facts():
    facts = make_facts(None, TODAY)
    for c in _sources_cases():
        b = c.bind(facts)
        assert set(b.sources["read"]) <= set(SRC.SOURCES), c.id
        assert "{" not in b.prompt and not any("{" in t for toks in b.sources["use"].values() for t in toks
                                              if not t.startswith("re:")), c.id    # every placeholder is a known fact
        if c.sources["shape"] == "nothing":
            assert not c.sources["use"] and "regex_all" in c.answer, c.id
        else:
            assert c.sources["use"], c.id
        assert {"send_message", "send_email", "remember"} <= c.forbid, c.id         # reads only


def test_sources_block_is_validated():
    base = {"id": "x", "prompt": "p", "split": "heldout_sources"}
    ok = {**base, "sources": {"shape": "topic", "read": ["mail"], "use": {"mail": ["a"]}}}
    assert parse_case(ok, "sources").sources["read"] == ["mail"]
    for bad in ({"shape": "nope", "read": ["mail"]}, {"shape": "topic", "read": ["fax"]},
                {"shape": "topic", "read": ["mail"], "use": {"notes": ["a"]}},
                {"shape": "topic", "read": ["mail"], "contrast": True},
                {"shape": "single", "read": ["mail"]}, {"shape": "topic", "read": ["mail"], "use": {"mail": ["re:("]}}):
        with pytest.raises(ValueError):
            parse_case({**base, "sources": bad}, "sources")


# --------------------------------------------------------------------------------------------- the sources scorer


def _spec(**kw):
    return {"shape": "topic", "read": ["mail", "messages"], "use": {}, "contrast": False, **kw}


def test_reads_all_needs_every_source_and_ignores_schema_loads_and_failed_calls():
    spec = _spec()
    both = traj([("view_emails", {"query": "x"}), ("view_messages", {"query": "x"})])
    assert SRC.score_trial(spec, both)["reads_all"]
    one = traj([("view_emails", {"query": "x"})])
    got = SRC.score_trial(spec, one)
    assert not got["reads_all"] and got["missing"] == ["messages"]
    assert not SRC.score_trial(spec, traj([("get_tool_schemas", {"tools": ["view_messages"]}), ("view_emails", {})]))["reads_all"]
    failed = traj([("view_emails", {}), ("view_messages", {"bogus": 1})],
                  tool_errors=["(error calling view_messages({'bogus': 1}): unexpected argument(s) ['bogus'].)"])
    assert not SRC.score_trial(spec, failed)["reads_all"]


def test_coverage_is_generous_and_over_reading_is_strict():
    spec = _spec(read=["calendar", "reminders"])
    assert SRC.score_trial(spec, traj([("get_upcoming", {"period": "tomorrow"})]))["reads_all"]       # lists reminders too
    assert not SRC.score_trial(spec, traj([("get_upcoming", {"calendar_only": True})]))["reads_all"]
    assert SRC.score_trial(spec, traj([("get_my_day_plan", {"day": "this_week"})]))["reads_all"]
    contrast = _spec(shape="single", contrast=True, read=["calendar"])
    # the natural calendar call is not charged with reading reminders ...
    assert not SRC.score_trial(contrast, traj([("get_upcoming", {"period": "tomorrow"})]))["over_read"]
    # ... a second app's own tool is, and so is a broad no-query read
    got = SRC.score_trial(contrast, traj([("get_upcoming", {}), ("view_emails", {})]))
    assert got["over_read"] and got["extra"] == ["mail"]
    assert SRC.score_trial(contrast, traj([("get_recent_activity", {})]))["over_read"]
    assert not SRC.score_trial(contrast, traj([("get_recent_activity", {})]))["reads_all"]
    # over-reading is a contrast measure only
    assert not SRC.score_trial(_spec(), traj([("view_emails", {}), ("view_messages", {}), ("search_notes", {})]))["over_read"]


def test_answer_use_tokens_plain_and_regex():
    spec = _spec(use={"mail": ["terminal 2", "re:\\b6:40\\s?pm\\b"], "messages": ["asked"]})
    ok = traj([("view_emails", {}), ("view_messages", {})], final="Her flight lands at 6:40 PM, and she asked about your email.")
    assert SRC.score_trial(spec, ok)["uses_all"]
    half = traj([("view_emails", {}), ("view_messages", {})], final="Her flight lands in terminal 2.")
    got = SRC.score_trial(spec, half)
    assert not got["uses_all"] and got["unused"] == ["messages"]
    assert SRC.score_trial(_spec(use={}), traj([("view_emails", {})], final="Nothing found."), answer_ok=True)["uses_all"]
    assert not SRC.score_trial(_spec(use={}), traj([("view_emails", {})], final="x"), answer_ok=False)["uses_all"]


def test_summarize_and_rows_roundtrip():
    spec = {**_spec(use={"mail": ["x"]})}
    trials = []
    for model, final, calls in (("a", "x here", [("view_emails", {}), ("view_messages", {})]), ("b", "nope", [("view_emails", {})])):
        s = SRC.score_trial(spec, traj(calls, final=final))
        trials.append({"model": model, "case": "c", "trial": 1, "shape": "topic", "contrast": False, **s})
    trials.append({"model": "a", "case": "d", "trial": 1, "shape": "single", "contrast": True, "reads_all": True,
                   "uses_all": True, "over_read": True, "n_calls": 2, "missing": [], "covered": [], "unused": [], "extra": ["mail"]})
    out = SRC.summarize(trials)
    assert out["a"]["reads_all"] == 1.0 and out["a"]["uses_all"] == 1.0 and out["a"]["over_read"] == 1.0
    assert out["b"]["reads_all"] == 0.0 and out["b"]["uses_all"] == 0.0 and out["b"]["over_read"] is None
    assert "reads every expected source" in SRC.render(out)


def test_trials_from_rows_scores_a_stored_run():
    case = next(c for c in _sources_cases() if c.id == "src_topic_flight").bind(make_facts(None, TODAY))
    r = row("m", "src_topic_flight", category="sources",
            final="Mom's flight lands at 6:40pm at SFO, and she texted to ask if you got her email.")
    r["steps"] = [{"calls": [{"name": "view_emails", "args": {"query": "flight"}},
                             {"name": "view_messages", "args": {"query": "flight"}}]}]
    got = SRC.trials_from_rows([r, row("m", "not_a_sources_case")], {case.id: case})
    assert len(got) == 1 and got[0]["reads_all"] and got[0]["uses_all"] and got[0]["shape"] == "topic"


# --------------------------------------------------------------------------------------------- leak check


def _family_prompts(seeds: int = 6) -> list[str]:
    code = textwrap.dedent(f"""
        import json, os, random, sys
        sys.path.insert(0, os.getcwd())
        import scen
        import scen_r5_sources as M
        out = set()
        for seed in range({seeds}):
            for sc in M.fam_all_sources_r5(random.Random(f"gate-{{seed}}")):
                out.add(sc.prompt)
        for sc in M.fam_all_sources_r5(random.Random("all_sources_r5-r3")):
            out.add(sc.prompt)
        print("PROMPTS=" + json.dumps(sorted(out)))
    """)
    env = {**os.environ, "ASK_LORA_ROUND": "4", "ASK_LORA_R5": "1"}
    env.pop("WISP_HOME", None)
    done = subprocess.run([sys.executable, "-c", code], cwd=BUILDER, env=env, capture_output=True, text=True, timeout=900)
    line = next((ln for ln in done.stdout.splitlines() if ln.startswith("PROMPTS=")), None)
    assert line, f"could not build the training family's prompts:\n{done.stderr[-1500:]}"
    return json.loads(line[len("PROMPTS="):])


def _build_prompts() -> list[str]:
    """User prompts of every r5 dataset build on disk (meta.jsonl `prompt`), when any are present."""
    out = []
    for meta in sorted((ROOT / "training" / "ask-lora-r5" / "data").glob("*/meta.jsonl")):
        for line in meta.read_text().splitlines():
            try:
                p = json.loads(line).get("prompt")
            except ValueError:
                continue
            if isinstance(p, str) and p.strip():
                out.append(p)
    return out


def test_held_out_prompts_do_not_repeat_the_training_family():
    """No sources.yaml prompt, bound in the canonical world and seeds 0-49, equals, nearly equals (the dataset
    builder's leak.fold + trigram Jaccard >= leak.THRESHOLD) or shares any four consecutive words with a prompt of
    the r5 training family (`all_sources_r5`, several seeds) or of any r5 build on disk."""
    leak = _load(BUILDER / "leak.py", "leak_for_test")
    held = {}
    for c in _sources_cases():
        for facts in [make_facts(None, TODAY)] + [make_facts(s, TODAY) for s in range(50)]:
            held[leak.fold(c.bind(facts).prompt)] = (c.id, c.bind(facts).prompt)
    index = leak.LeakIndex([leak._entry(i, p) for i, p in held.values()], toolbench=False)
    train = sorted(set(_family_prompts()) | set(_build_prompts()))
    assert len(train) > 150, "the training family built too few prompts to mean anything"

    def grams(text, n=4):
        w = leak.fold(text).split()
        return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}
    train_grams = set().union(*(grams(p) for p in train))
    near = [(p, round(index.nearest(p)[0], 2), index.nearest(p)[1]["id"]) for p in train if index.leaks(p)[0]]
    assert not near, f"training prompts too close to a held-out one (leak.THRESHOLD {leak.THRESHOLD}): {near[:5]}"
    shared = {c.id: sorted(" ".join(g) for g in grams(c.prompt) & train_grams)[:3] for c in _sources_cases()
              if grams(c.bind(make_facts(None, TODAY)).prompt) & train_grams}
    assert not shared, f"held-out prompts that repeat four words of a training prompt: {shared}"


def test_leak_check_notices_a_copy():
    leak = _load(BUILDER / "leak.py", "leak_for_test2")
    prompt = next(c for c in _sources_cases() if c.id == "src_hit_water").prompt
    index = leak.LeakIndex([leak._entry("x", prompt)], toolbench=False)
    assert index.leaks(prompt)[0] and index.leaks(prompt.upper() + "?")[0]
    assert not index.leaks("anything about the dentist")[0]
