"""Mac-runnable checks for gen_finals.py and choose_r5.py: no GPU, no weights, no torch. A fake generate function
stands in for the model; the real tokenizer and chat template are used when the model directory is present.

    /Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest --noconftest --rootdir=. -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401
import choose  # noqa: E402
import choose_r5 as c5  # noqa: E402
import gen  # noqa: E402
import gen_finals as G  # noqa: E402

TOKDIR = "/Users/adijain/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp"
needs_tok = pytest.mark.skipif(not os.path.isdir(TOKDIR), reason="tokenizer directory not on this machine")

TOOLS = [{"type": "function", "function": {"name": "view_emails", "description": "Read mail.",
                                           "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}]
SYS = "You are Wisp.\nIMPORTANT rules: be brief."
CALL = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
    "name": "view_emails", "arguments": "{\"query\": \"permit\"}"}}]}


def mk(i, family="fam", kind="chat", target="Scripted answer.", extra=None, meta=None):
    msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": f"question number {i}"}]
    if kind == "tool":
        msgs += [dict(CALL), {"role": "tool", "tool_call_id": "c1", "content": f"Result {i}: REF-{i}"}]
    msgs += list(extra or [])
    msgs.append({"role": "assistant", "content": target} if target is not None else dict(CALL))
    return {"meta": {"id": f"{family}-{i:03d}", "family": family, "chat_template_kwargs": {"enable_thinking": True},
                     "tool_choice": "auto", **(meta or {})}, "messages": msgs, "tools": TOOLS}


@pytest.fixture(scope="module")
def tok():
    if not os.path.isdir(TOKDIR):
        pytest.skip("no tokenizer")
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(TOKDIR, local_files_only=True)


def silent(*_a):
    pass


# ---------------------------------------------------------------- row selection
def test_select_rows_keeps_only_final_answers():
    rows = [mk(1), mk(2, kind="tool"), mk(3, target=None), mk(4, target="x", extra=None)]
    rows.append({"meta": {"id": "pic-1", "family": "v"}, "tools": None, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "x"}}]},
        {"role": "assistant", "content": "a picture"}]})
    got, skipped = G.select_rows(rows)
    assert [r["meta"]["id"] for r in got] == ["fam-001", "fam-002", "fam-004"]
    assert skipped == ["pic-1"]


# ---------------------------------------------------------------- thought parsing
def test_parse_forced_prompt_has_no_opening_tag():
    out = G.parse_output("let me think\nabout it\n<channel|>Here you go.<turn|>", prompt_forced=True)
    assert out["reasoning"] == "let me think\nabout it" and out["content"] == "Here you go."
    assert out["tool_calls"] == [] and "channel_leak" not in out


def test_parse_model_opens_the_channel_itself():
    out = G.parse_output("<|channel>thought\nhmm\n<channel|>Hello Sam!<turn|>", prompt_forced=False)
    assert out["reasoning"] == "hmm" and out["content"] == "Hello Sam!"


def test_parse_empty_thought_and_no_thought():
    out = G.parse_output("<|channel>thought\n<channel|>Short.<turn|>", prompt_forced=False)
    assert out["reasoning"] is None and out["content"] == "Short."
    out = G.parse_output("Straight answer.<turn|>", prompt_forced=False)
    assert out["reasoning"] is None and out["content"] == "Straight answer."


def test_parse_unclosed_thought_is_all_reasoning():
    out = G.parse_output("<|channel>thought\nstill thinking and cut off", prompt_forced=False)
    assert out["content"] == "" and out["reasoning"] == "still thinking and cut off"
    out = G.parse_output("forced and cut off", prompt_forced=True)
    assert out["content"] == "" and out["reasoning"] == "forced and cut off"


def test_parse_tool_call_and_stray_markers():
    out = G.parse_output("<|channel>thought\nneed data\n<channel|><|tool_call>call:view_emails{query:<|\"|>x<|\"|>}"
                         "<tool_call|><|tool_response>", prompt_forced=False)
    assert out["content"] == "" and out["tool_calls"][0]["function"]["name"] == "view_emails"
    out = G.parse_output("<|channel>thought\nA<channel|>answer<|channel>thought\nB<channel|>more<turn|>", prompt_forced=False)
    assert out.get("channel_leak") is True
    out = G.parse_output("fine<eos><pad><pad>", prompt_forced=False)
    assert out["content"] == "fine"


@needs_tok
def test_parse_roundtrips_through_the_real_tokenizer(tok):
    text = "<|channel>thought\nhi there\n<channel|>Yo **bold**.<turn|>"
    ids = tok.encode(text, add_special_tokens=False)
    assert ids[-1] == 106
    rec = G.make_record(mk(1), 0, ids + [0, 0, 0], tok, 10, False, 1.0, 1, "math")
    assert (rec["reasoning"], rec["content"], rec["finish_reason"]) == ("hi there", "Yo **bold**.", "stop")
    assert rec["usage"]["completion_tokens"] == len(ids)
    rec = G.make_record(mk(1), 0, ids[:-1], tok, 10, False, 1.0, 1, "math")
    assert rec["finish_reason"] == "length"


# ---------------------------------------------------------------- prompts, hash, resume, shards, batching
@needs_tok
def test_prompt_comes_from_render_and_forced_after_tool(tok):
    assert G.prompt_for(tok, mk(1, kind="tool")).endswith(G.FORCED)
    assert not G.prompt_for(tok, mk(1)).endswith(G.FORCED)
    assert G.prompt_for(tok, mk(1, kind="tool", meta={"think": True, "thought": "t"})).endswith(G.FORCED)


def fake_model(tok, log=None, fail_on=None):
    """A model that answers each prompt by echoing its question number, as a thought plus an answer."""
    def generate(prompts, seed):
        if log is not None:
            log.append(len(prompts))
        outs = []
        for p in prompts:
            text = tok.decode(p, skip_special_tokens=False)
            q = text.rsplit("question number ", 1)[1].split("<")[0].split("\n")[0].strip()
            if fail_on and q in fail_on:
                raise RuntimeError("out of memory")
            forced = text.endswith(G.FORCED)
            body = f"thinking {q}\n<channel|>Answer for {q}.<turn|>"
            outs.append(tok.encode(body if forced else G.OPEN + body, add_special_tokens=False) + [0] * 3)
        return outs
    return generate


def lines(path):
    return [json.loads(x) for x in open(path)]


@needs_tok
def test_records_match_gen_hash_and_format(tok, tmp_path):
    rows = [mk(1), mk(2, kind="tool")]
    G.run(rows, tok, fake_model(tok), str(tmp_path), 0, 1, 1, log=silent)
    recs = lines(tmp_path / "finals.s0of1.jsonl")
    assert len(recs) == 2
    for r, rec in zip(rows, recs):
        assert rec["request_hash"] == gen.request_hash(r["messages"][:-1], r["tools"])
        assert set(rec) >= {"id", "family", "k", "model", "request_hash", "content", "reasoning", "tool_calls",
                            "finish_reason", "usage", "seconds", "no_choices"}
    assert recs[1]["content"] == "Answer for 2." and recs[1]["reasoning"] == "thinking 2"   # forced (after a tool)
    assert recs[0]["content"] == "Answer for 1." and recs[0]["reasoning"] == "thinking 1"   # model opens the channel


@needs_tok
def test_resume_is_sample_major_and_skips_done(tok, tmp_path):
    rows = [mk(i) for i in range(4)]
    G.run(rows, tok, fake_model(tok, fail_on={"3"}), str(tmp_path), 0, 1, 2, log=silent)   # 3 fails twice: errors
    recs = lines(tmp_path / "finals.s0of1.jsonl")
    assert [r["k"] for r in recs] == [0, 0, 0, 0, 1, 1, 1, 1]          # sample 0 of every prompt first
    assert sum("error" in r for r in recs) == 2
    log = []
    G.run(rows, tok, fake_model(tok, log), str(tmp_path), 0, 1, 2, log=silent)               # only the failed pairs rerun
    assert sum(log) == 2
    ok = [r for r in lines(tmp_path / "finals.s0of1.jsonl") if "error" not in r]
    assert sorted((r["id"], r["k"]) for r in ok) == sorted((r["meta"]["id"], k) for r in rows for k in (0, 1))
    log.clear()
    G.run(rows, tok, fake_model(tok, log), str(tmp_path), 0, 1, 3, log=silent)               # a bigger k adds sample 2 only
    assert sum(log) == 4
    # a half-written last line (kill) does not break resume
    with open(tmp_path / "finals.s0of1.jsonl", "a") as f:
        f.write('{"id": "fam-0')
    assert len(G.done_pairs(str(tmp_path))) == 12


def test_shards_are_disjoint_and_cover():
    rows = [mk(i) for i in range(19)]
    parts = [G.shard_rows(rows, i, 8) for i in range(8)]
    ids = [r["meta"]["id"] for p in parts for r in p]
    assert sorted(ids) == sorted(r["meta"]["id"] for r in rows) and len(set(ids)) == 19
    assert max(map(len, parts)) - min(map(len, parts)) <= 1
    with pytest.raises(ValueError):
        G.shard_rows(rows, 8, 8)


@needs_tok
def test_shard_files_and_resume_under_another_layout(tok, tmp_path):
    rows = [mk(i) for i in range(6)]
    for s in range(3):
        G.run(rows, tok, fake_model(tok), str(tmp_path), s, 3, 1, log=silent)
    assert sorted(p.name for p in tmp_path.glob("finals.*.jsonl")) == [f"finals.s{s}of3.jsonl" for s in range(3)]
    log = []
    G.run(rows, tok, fake_model(tok, log), str(tmp_path), 0, 2, 1, log=silent)     # a 2-way layout finds all done
    assert log == []


@needs_tok
def test_batching_keeps_every_answer_with_its_prompt(tok, tmp_path):
    rows = [mk(i, extra=[{"role": "user", "content": "padding " * (i * 37 % 11)},
                         {"role": "assistant", "content": "ok"},
                         {"role": "user", "content": f"question number {i}"}]) for i in range(11)]
    log = []
    G.run(rows, tok, fake_model(tok, log), str(tmp_path), 0, 1, 2, batch=4, log=silent)
    assert max(log) <= 4 and sum(log) == 22
    recs = lines(tmp_path / "finals.s0of1.jsonl")
    assert len(recs) == 22
    for rec in recs:
        n = int(rec["id"].split("-")[1])
        assert rec["content"] == f"Answer for {n}."
    # nearly sample-major: no sample-1 line before the last sample-0 line
    ks = [r["k"] for r in recs]
    assert ks == sorted(ks)


def test_windows_sort_by_length_within_a_sample_only():
    items = [(0, mk(i), list(range(n)), False) for i, n in enumerate([9, 3, 7, 1, 8, 2])]
    items += [(1, mk(9), [1], False)]
    got = list(G.windows(items, 2))
    assert [len(it[2]) for g in got for it in g] == [1, 2, 3, 7, 8, 9, 1]
    assert all(len(g) <= 2 for g in got) and got[-1][0][0] == 1
    assert [g[0] for g in G.windows(items, 1)] == items


@needs_tok
def test_selfcheck_detects_a_batch_that_disagrees(tok):
    rows = [mk(i) for i in range(5)]
    same = lambda prompts, seed, greedy=False, max_new=0: [[7] * 30 for _ in prompts]  # noqa: E731
    assert G.selfcheck(rows, tok, same, log=silent)
    state = {"n": 0}

    def drift(prompts, seed, greedy=False, max_new=0):
        state["n"] += 1
        return [[7] * 30 if len(prompts) == 1 else [8] * 30 for _ in prompts]
    assert not G.selfcheck(rows, tok, drift, log=silent)


# ---------------------------------------------------------------- the r5 choose step
GOOD = "You have one email from Dana Whitfield about the permit. Reply is due Thursday."


def samples(path, rows, texts, reasoning="because"):
    with open(path, "w") as f:
        for r in rows:
            for k, t in enumerate(texts.get(r["meta"]["id"], [])):
                f.write(json.dumps({"id": r["meta"]["id"], "family": r["meta"]["family"], "k": k,
                                    "request_hash": gen.request_hash(r["messages"][:-1], r["tools"]), "content": t,
                                    "reasoning": reasoning, "tool_calls": [], "finish_reason": "stop"}) + "\n")


def test_choose_r5_rejects_and_reports(tmp_path):
    bw = c5.BASE_WRITES
    rows = [mk(1, "alpha", "tool", bw), mk(2, "alpha", "tool", bw), mk(3, "alpha", "tool", "scripted three"),
            mk(4, "beta", "chat", bw), mk(5, "beta", "chat", "scripted five"), mk(6, "alpha", "tool", target=None)]
    partial = tmp_path / "partial.jsonl"
    partial.write_text("".join(json.dumps(r) + "\n" for r in rows))
    samples(tmp_path / "raw.jsonl", rows, {
        "alpha-001": ["There are 7 results, and 3 more below.", GOOD],          # first cut list, second passes
        "alpha-002": ["Here are 2, and 5 more."],                               # no pass, BASE_WRITES -> drop
        "alpha-003": ["The thing is X."],                                       # passes
        "beta-004": ["Nothing was saved in your memory about that."],           # memory stall in chat -> no pass
        "beta-005": ["Nothing was saved in your memory about that."],           # no pass, scripted stays
        "alpha-006": ["a call row is not a final"],
    })
    finals, chosen, stats = c5.choose_r5(str(partial), [str(tmp_path / "raw.jsonl")])
    assert len(finals) == 5 and "alpha-006" not in chosen
    assert chosen["alpha-001"]["content"] == GOOD and chosen["alpha-001"]["k"] == 1
    r5 = stats["r5"]
    assert r5["families"]["alpha"] == {**r5["families"]["alpha"], "final_rows": 3, "passed": 2, "to_drop": 1,
                                       "drop": ["alpha-002"], "scripted_kept": []}
    assert r5["families"]["beta"]["drop"] == ["beta-004"] and r5["families"]["beta"]["scripted_kept"] == ["beta-005"]
    assert r5["drop_ids"] == ["alpha-002", "beta-004"] and r5["no_pass_ids"] == ["alpha-002", "beta-004", "beta-005"]
    assert r5["rejects_by_reason"]["final:and_n_more"] == 2 and r5["rejects_by_reason"]["final:memory_stall"] == 2
    assert "DROP 2 rows" in c5.summary(r5)
    assert choose.reject_reasons is c5.ORIG_REJECT                               # the patch is undone


def test_choose_r5_memory_stall_allowed_after_a_tool_call_and_for_recall():
    rec = {"id": "x", "k": 0, "content": "Nothing was saved in your memory yet.", "reasoning": "r",
           "tool_calls": [], "finish_reason": "stop"}
    rej = c5.make_reject()
    assert "final:memory_stall" in rej(rec, mk(1))
    assert "final:memory_stall" not in rej(rec, mk(1, kind="tool"))
    assert "final:memory_stall" not in rej(rec, mk(1, meta={"qkind": "rec_none"}))
    assert "channel_leak" in rej({**rec, "channel_leak": True}, mk(1))


def test_stall_after_tool_is_reported_and_optionally_allowed(tmp_path):
    rows = [mk(1, "alpha", "tool", "scripted")]
    partial = tmp_path / "partial.jsonl"
    partial.write_text(json.dumps(rows[0]) + "\n")
    samples(tmp_path / "raw.jsonl", rows, {"alpha-001": ["I couldn't find any emails about that."]})
    _, chosen, stats = c5.choose_r5(str(partial), [str(tmp_path / "raw.jsonl")])
    assert not chosen and stats["r5"]["families"]["alpha"]["stall_only_after_tool"] == ["alpha-001"]
    _, chosen, _ = c5.choose_r5(str(partial), [str(tmp_path / "raw.jsonl")], stall_ok_after_tool=True)
    assert "alpha-001" in chosen


@needs_tok
def test_end_to_end_samples_then_choose(tok, tmp_path):
    rows = [mk(1, "alpha", "tool", c5.BASE_WRITES), mk(2, "alpha", "chat", "scripted")]
    partial = tmp_path / "partial.jsonl"
    partial.write_text("".join(json.dumps(r) + "\n" for r in rows))
    G.run(rows, tok, fake_model(tok), str(tmp_path), 0, 1, 2, log=silent)
    rc = c5.main(["--partial", str(partial), "--raw", str(tmp_path / "finals.*.jsonl"),
                  "--out-chosen", str(tmp_path / "chosen.jsonl"), "--out-stats", str(tmp_path / "stats.json"),
                  "--out-nopass-ids", str(tmp_path / "nopass.ids"), "--out-drop-ids", str(tmp_path / "drop.ids")])
    assert rc == 0
    chosen = lines(tmp_path / "chosen.jsonl")
    assert [c["id"] for c in chosen] == ["alpha-001", "alpha-002"] and all(c["k"] == 0 for c in chosen)
    assert (tmp_path / "drop.ids").read_text() == "" and (tmp_path / "nopass.ids").read_text() == ""


def test_final_problems_copy_matches_validate():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ask-lora-r2", "builder"))
    try:
        import validate
    except Exception as exc:  # noqa: BLE001  (needs the repo's service tree and venv)
        pytest.skip(f"validate.py not importable here: {exc!r}")
    for text in ("and 3 more", "plus 12 more items", "nothing was saved", "We haven't talked about it", "all good"):
        for meta in (None, {"qkind": "rec_user"}):
            for tool in (False, True):
                assert c5.final_problems_r5(text, meta, tool) == validate.final_problems_r5(text, meta, tool)


def test_reuse_r4_takes_only_matching_base_answers():
    import reuse_r4
    row = {"meta": {"id": "a-1", "family": "web_r3"}, "tools": [],
           "messages": [{"role": "system", "content": "date"}, {"role": "user", "content": "hi there"},
                        {"role": "assistant", "content": "scripted answer"}]}
    good = {"prompt": "<|turn>user\nhi there<turn|>\n<|turn>model\n", "completion": "Base wrote this.<turn|>"}
    rec = reuse_r4.reused(row, {"a-1": good})
    assert rec["content"] == "Base wrote this." and rec["k"] == 0
    assert reuse_r4.reused(row, {}) is None                                                      # no r4 row
    assert reuse_r4.reused(row, {"a-1": {**good, "prompt": "something else"}}) is None           # different history
    assert reuse_r4.reused(row, {"a-1": {**good, "completion": "scripted answer<turn|>"}}) is None   # r4 kept the script
    other = {**row, "meta": {"id": "a-1", "family": "session_chat_r4"}}
    assert reuse_r4.reused(other, {"a-1": good}) is None                                         # the family that lost base's voice
