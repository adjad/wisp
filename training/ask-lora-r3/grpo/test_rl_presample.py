"""CPU tests for rl_presample.py (no GPU, no model, no Wisp, no network beyond an optional loopback stub).
Run: python -m pytest -q test_rl_presample.py   (or python test_rl_presample.py)

The stub engine is an in-process transport by default, so the suite runs where sockets are blocked; two tests also put
the same stub behind a real loopback HTTP server and are skipped if the sandbox forbids binding a port.
"""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reward as W  # noqa: E402
import rl_presample as S  # noqa: E402

MODEL = "r4-sft-oQ4e-mtp"
Q = '<|"|>'
BOS = "<bos>"
FORCED = "<|channel>thought\n"


# ------------------------------------------------------------------ fixtures

def call(name, **kw):
    def v(x):
        if isinstance(x, bool):
            return "true" if x else "false"
        if isinstance(x, (int, float)):
            return str(x)
        if isinstance(x, list):
            return "[" + ",".join(v(i) for i in x) + "]"
        return f"{Q}{x}{Q}"
    return f"<|tool_call>call:{name}{{" + ",".join(f"{k}:{v(x)}" for k, x in sorted(kw.items())) + "}<tool_call|>"


def lab(**kw):
    base = {"step": "call", "forced": False, "expected": [], "key_args": [], "present_args": [], "alt_name_sets": [],
            "forbidden": ["send_email", "run_shell"], "final": False, "ask": False, "facts": [], "user_name": "Sam",
            "round": 5}
    base.update(kw)
    return base


def mkrow(rid, kind, family, prompt, ideal, label_kw=None, **row_kw):
    label = lab(rl_kind=kind, ideal_completion=ideal, **(label_kw or {}))
    label["ceiling"] = W.score(ideal, label, rnd=5).total
    row = {"id": rid, "family": family, "prompt": prompt, "label": json.dumps(label), "modality": "text"}
    row.update(row_kw)
    return row


def call_row(rid, family="gather_then_send_r4", kind="call", **kw):
    ideal = call("get_upcoming", days=2) + "<|tool_response>"
    return mkrow(rid, kind, family, f"{BOS}<|turn>user\nwhat is on {rid}?<turn|>\n<|turn>model\n", ideal,
                 {"expected": [{"name": "get_upcoming", "args": {"days": 2}}], "key_args": [{"days": 2}], "present_args": [[]]}, **kw)


def forced_row(rid, family="narrate_r3", kind="narrate", **kw):
    ideal = "Say what the calendar holds.\n<channel|>Your Ethics class is at 11 AM.<turn|>"
    return mkrow(rid, kind, family, f"{BOS}<|turn>user\nwhat now {rid}?<turn|>\n<|turn>model\n{FORCED}", ideal,
                 {"step": "final", "final": True, "forced": True, "narrate": True, "facts": ["11 AM", "Ethics"]}, **kw)


def chat_row(rid, kind="chat_anchor", family="chat_voice_r4", **kw):
    ideal = "Hi there, I am doing well today, thanks for asking.<turn|>"
    return mkrow(rid, kind, family, f"{BOS}<|turn>user\nhello {rid}<turn|>\n<|turn>model\n", ideal,
                 {"step": "final", "final": True, "facts": []}, **kw)


def mm_row(rid):
    return {"id": rid, "family": "vision_r3", "modality": "vision", "prompt": "x",
            "label": json.dumps({"modality": "vision", "rl_kind": "mm", "round": 5, "ceiling": None})}


def write_jsonl(path, rows):
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def label_of(row):
    return json.loads(row["label"]) if isinstance(row["label"], str) else row["label"]


def parsed_row(row):
    r = dict(row)
    r["label"] = label_of(row)
    return r


class Stub:
    """A fake engine. `reply(body) -> (text, finish_reason)`; records every request."""

    def __init__(self, reply, strip_stop=False, token_fn=None, models=(MODEL,), bos_extra=0):
        self.reply, self.strip_stop, self.models = reply, strip_stop, list(models)
        self.token_fn = token_fn or (lambda t: len(t.split()) + 1)
        self.bos_extra = bos_extra
        self.requests = []
        self.lock = threading.Lock()

    def _cut(self, text, fin):
        if self.strip_stop and fin == "stop":
            for s in S.STOPS:
                if text.endswith(s):
                    return text[: -len(s)]
        return text

    def transport(self, path, body):
        with self.lock:
            self.requests.append((path, body))
        if path == "/v1/models":
            return {"data": [{"id": m} for m in self.models]}
        if path == "/v1/completions":
            n = body.get("n", 1)
            choices, comp = [], 0
            for i in range(n):
                text, fin = self.reply(body)
                comp += self.token_fn(text)
                choices.append({"index": i, "text": self._cut(text, fin), "finish_reason": fin})
            return {"choices": choices, "usage": {"prompt_tokens": self.token_fn(body["prompt"]) + self.bos_extra,
                                                  "completion_tokens": comp}}
        if path == "/v1/chat/completions":
            msg, fin = self.reply(body)
            return {"choices": [{"index": 0, "message": msg, "finish_reason": fin}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        raise AssertionError(path)


def echo(rows):
    table = {parsed_row(r)["prompt"]: label_of(r)["ideal_completion"] for r in rows}
    return lambda body: (table[body["prompt"]], "stop")


def run(argv, stub, environ=None):
    environ = {"MOE_DIR": "/tmp/isolated-moe"} if environ is None else environ
    return S.main(argv, transport=stub.transport, environ=environ)


def base_argv(tmp_path, prompts, extra=()):
    return ["--base-url", "http://127.0.0.1:18791", "--served-model", MODEL, "--prompts", prompts,
            "--out", str(tmp_path / "pre-ok-report.json"), "--G", "3", "--per-bucket", "2", "--safety-train", "3",
            "--safety-dev", "2", "--skip-bos-check", *extra]


# ------------------------------------------------------------------ guards

def test_refuses_without_moe_dir(tmp_path):
    rows = [call_row("a")]
    stub = Stub(echo(rows))
    for env in ({}, {"MOE_DIR": ""}, {"MOE_DIR": "  "}):
        with pytest.raises(SystemExit) as e:
            run(base_argv(tmp_path, write_jsonl(tmp_path / "p.jsonl", rows)), stub, environ=env)
        assert e.value.code == 2
    assert stub.requests == []                                  # nothing was sent


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8765", "http://127.0.0.1:8775/",
                                 "http://192.168.1.4:18791", "https://127.0.0.1:18791", "http://127.0.0.1",
                                 "http://127.0.0.1:18791/v1"])
def test_refuses_live_ports_and_non_loopback(url):
    with pytest.raises(SystemExit):
        S.check_base_url(url)


def test_accepts_free_loopback_port():
    assert S.check_base_url("http://127.0.0.1:18791") == "http://127.0.0.1:18791"
    assert S.check_base_url("http://localhost:18792/") == "http://localhost:18792"


def test_port_shorthand_and_exclusive_flags(tmp_path):
    a = S.parse_args(["--port", "18791", "--served-model", MODEL, "--prompts", "p", "--out", "o"])
    assert a.base_url == "http://127.0.0.1:18791" and a.anchor_per_bucket == 30
    with pytest.raises(SystemExit):
        S.parse_args(["--served-model", MODEL, "--prompts", "p", "--out", "o"])


def test_refuses_ling_and_embedders():
    for bad in ("Ling-V3-4bit", "bge-embed-small", "qwen3-rerank"):
        with pytest.raises(SystemExit):
            S.check_model(bad)
    S.check_model("gemma-4-E4B-it-oQ4e-mtp-sampling")             # "ling" inside a word is fine


def test_no_process_control_in_the_module():
    mods = S._imports(Path(S.__file__))
    assert not mods & {"subprocess", "signal", "multiprocessing", "socket", "torch", "numpy", "transformers_top"}
    src = Path(S.__file__).read_text()
    assert "os.system" not in src and "os.kill" not in src and "Popen" not in src
    assert "/admin" not in src                                  # never loads or unloads a model through the engine's admin API


def test_refuses_unlisted_model(tmp_path):
    rows = [call_row("a")]
    stub = Stub(echo(rows), models=["something-else"])
    with pytest.raises(SystemExit):
        run(base_argv(tmp_path, write_jsonl(tmp_path / "p.jsonl", rows)), stub)
    assert [p for p, _ in stub.requests] == ["/v1/models"]


def test_refuses_rows_not_stamped_round_5(tmp_path):
    r = call_row("a")
    lab4 = label_of(r)
    lab4["round"] = 4
    r["label"] = json.dumps(lab4)
    stub = Stub(echo([r]))
    with pytest.raises(SystemExit):
        run(base_argv(tmp_path, write_jsonl(tmp_path / "p.jsonl", [r])), stub)
    assert stub.requests == []


# ------------------------------------------------------------------ the request

def test_forced_prompt_sent_unmodified_and_parsed_forced():
    row = parsed_row(forced_row("f1"))
    seen = {}

    def reply(body):
        seen["body"] = body
        return "Let me answer.\n<channel|>Your Ethics class is at 11 AM.<turn|>", "stop"       # starts inside the open thought
    stub = Stub(reply)
    recs = S.sample_completions(S.Client("http://127.0.0.1:1", transport=stub.transport), MODEL, row, 2, "calls", 0, 1024)
    path, body = stub.requests[0]
    assert path == "/v1/completions" and "messages" not in body and "tools" not in body
    assert body["prompt"] == row["prompt"] and body["prompt"].endswith(FORCED)          # byte for byte
    assert body["skip_special_tokens"] is False and body["max_tokens"] == 1024 and body["stream"] is False
    assert (body["temperature"], body["top_p"], body["top_k"]) == (1.0, 0.95, 64)
    assert "seed" in body and body["include_stop_str_in_output"] is True
    # the text reaches reward.parse with forced=True: it parses only that way
    assert not W.parse(recs[0]["text"], True).problems
    assert W.parse(recs[0]["text"], False).problems
    sc = S.score_sample(recs[0]["text"], recs[0]["truncated"], row["label"], 5)
    assert sc["total"] == W.score(recs[0]["text"], row["label"], rnd=5).total and not sc["malformed"]


def test_max_tokens_from_label_else_1024_and_override():
    a = S.parse_args(["--base-url", "http://127.0.0.1:18791", "--served-model", MODEL, "--prompts", "p", "--out", "o"])
    r1 = parsed_row(call_row("a"))
    assert S.row_max_tokens(r1, a) == 1024
    r1["label"]["max_tokens"] = 400
    assert S.row_max_tokens(r1, a) == 400
    a.max_tokens = 1536
    assert S.row_max_tokens(r1, a) == 1536


def test_seeded_calls_distinct_and_deterministic_and_n_mode_single_call():
    row = parsed_row(call_row("a"))
    stub = Stub(echo([call_row("a")]))
    c = S.Client("http://127.0.0.1:1", transport=stub.transport)
    S.sample_completions(c, MODEL, row, 6, "calls", 7, 100)
    seeds = [b["seed"] for p, b in stub.requests]
    assert len(seeds) == 6 and len(set(seeds)) == 6 and all("n" not in b for _, b in stub.requests)
    stub2 = Stub(echo([call_row("a")]))
    S.sample_completions(S.Client("http://127.0.0.1:1", transport=stub2.transport), MODEL, row, 6, "calls", 7, 100)
    assert seeds == [b["seed"] for p, b in stub2.requests]
    stub3 = Stub(echo([call_row("a")]))
    recs = S.sample_completions(S.Client("http://127.0.0.1:1", transport=stub3.transport), MODEL, row, 6, "n", 7, 100)
    assert len(stub3.requests) == 1 and stub3.requests[0][1]["n"] == 6 and len(recs) == 6
    assert all(r["tokens"] is None for r in recs)                                       # no per-choice counts without a tokenizer
    recs = S.sample_completions(S.Client("http://127.0.0.1:1", transport=Stub(echo([call_row("a")])).transport), MODEL, row, 6, "n", 7, 100,
                                count_tokens=lambda t: 3)
    assert all(r["tokens"] == 3 and r["tokens_exact"] for r in recs)


# ------------------------------------------------------------------ echo stub == scoring the ideal directly

@pytest.mark.parametrize("strip_stop", [False, True])
def test_echo_of_ideal_scores_and_parses_identically(strip_stop):
    rows = [call_row("c1"), forced_row("f1"), chat_row("h1"), call_row("c2", family="compose_r3")]
    stub = Stub(echo(rows), strip_stop=strip_stop)
    cl = S.Client("http://127.0.0.1:1", transport=stub.transport)
    for r in map(parsed_row, rows):
        ideal = r["label"]["ideal_completion"]
        recs = S.sample_completions(cl, MODEL, r, 2, "calls", 0, 1024)
        for rec in recs:
            assert not rec["truncated"]
            assert W.parse(rec["text"], r["label"]["forced"]) == W.parse(ideal, r["label"]["forced"])       # Parsed fields
            a, b = S.score_sample(rec["text"], False, r["label"], 5), W.score(ideal, r["label"], rnd=5)
            assert a["total"] == b.total and a["flags"] == list(b.flags)
            assert a["parts"] == {k: round(v, 6) for k, v in b.parts.items() if abs(v) > 1e-12}
            assert a["total"] >= r["label"]["ceiling"] - 1e-9


def test_stripped_stop_is_restored_by_kind():
    t, trunc = S.finalize_completion("Hello there friend.", "stop")
    assert t.endswith("<turn|>") and not trunc
    t, _ = S.finalize_completion(call("get_upcoming", days=2), "stop")
    assert t.endswith("<|tool_response>")
    t, _ = S.finalize_completion("Hi", "stop", stop_reason="<eos>")
    assert t == "Hi<eos>"
    t, _ = S.finalize_completion("Hi<turn|>", "stop")
    assert t == "Hi<turn|>"
    assert W.parse(S.finalize_completion("Hello there friend.", "stop")[0], False).stopped


def test_truncation_is_finish_reason_length_and_exact_cap_stop_is_not():
    t, trunc = S.finalize_completion("a long ramble that never ends", "length")
    assert trunc and t == "a long ramble that never ends"                            # no stop invented
    assert W.score(t, lab(final=True, step="final"), rnd=5).total == -1.0             # scored as the malformed failure
    t, trunc = S.finalize_completion("All done here.<turn|>", "length")                # stop emitted at exactly max_tokens
    assert not trunc
    with pytest.raises(ValueError):
        S.finalize_completion("x", None)


def test_truncated_sample_flows_to_the_report_share():
    rows = [forced_row(f"f{i}") for i in range(3)]
    n = {"i": 0}

    def reply(body):
        n["i"] += 1
        return ("never closes", "length") if n["i"] % 3 == 0 else ("Say it.\n<channel|>Ethics at 11 AM.<turn|>", "stop")
    stub = Stub(reply)
    cl = S.Client("http://127.0.0.1:1", transport=stub.transport)
    recs = S.sample_completions(cl, MODEL, parsed_row(rows[0]), 6, "calls", 0, 50)
    assert [r["truncated"] for r in recs] == [False, False, True, False, False, True]


# ------------------------------------------------------------------ BOS check

def test_bos_check_passes_and_catches_a_second_bos():
    rows = [parsed_row(call_row(f"c{i}")) for i in range(5)]
    count = lambda t: len(t.split()) + 1
    ok = S.bos_check(S.Client("x", transport=Stub(lambda b: ("", "stop"), token_fn=count).transport), MODEL, rows, count)
    assert ok["ok"] and ok["checked"] == 5
    bad = S.bos_check(S.Client("x", transport=Stub(lambda b: ("", "stop"), token_fn=count, bos_extra=1).transport), MODEL, rows, count)
    assert not bad["ok"] and bad["extra_bos"] and bad["mismatches"][0]["served"] == bad["mismatches"][0]["hf"] + 1


def test_main_aborts_on_double_bos_and_requires_a_counter(tmp_path):
    rows = [call_row("a")]
    p = write_jsonl(tmp_path / "p.jsonl", rows)
    argv = ["--base-url", "http://127.0.0.1:18791", "--served-model", MODEL, "--prompts", p, "--out", str(tmp_path / "o.json"),
            "--G", "2", "--per-bucket", "1"]
    stub = Stub(echo(rows))
    with pytest.raises(SystemExit):                                     # no tokenizer, no counts, no skip flag
        run(argv, stub)
    counts = tmp_path / "counts.json"
    counts.write_text(json.dumps({"a": len(parsed_row(rows[0])["prompt"].split()) + 1}))
    assert run(argv + ["--token-counts", str(counts)], Stub(echo(rows))) == 0
    with pytest.raises(SystemExit) as e:
        run(argv + ["--token-counts", str(counts)], Stub(echo(rows), bos_extra=1))
    assert e.value.code == 4


# ------------------------------------------------------------------ stratification

def pool():
    rows = []
    rows += [call_row(f"s{i}#0", family="restraint_r4") for i in range(12)]
    rows += [call_row(f"s{i}#1", family="restraint_r4") for i in range(3)]               # copies of the same prompts
    rows += [forced_row(f"n{i}#0") for i in range(9)]
    rows += [chat_row(f"h{i}#0") for i in range(7)]
    rows += [chat_row(f"q{i}#0", kind="session_anchor") for i in range(6)]
    rows += [chat_row(f"r{i}#0", kind="recall", family="session_chat_r4") for i in range(4)]
    rows += [call_row(f"x{i}#0", family="other_r3", kind="select") for i in range(5)]
    rows += [mm_row(f"v{i}#0") for i in range(4)]
    return [parsed_row(r) for r in rows]


def test_stratify_counts_dedupe_and_skips_multimodal():
    picked, info = S.stratify(pool(), per_bucket=4, anchor_per_bucket=3, safety_n=6, seed=1)
    kinds = {}
    for r in picked:
        kinds.setdefault(S.bucket_of(r), []).append(r["id"])
    assert {b: len(v) for b, v in kinds.items()} == {"safety": 6, "narrate": 4, "chat_anchor": 3, "session_anchor": 3,
                                                     "session": 4, "select": 4}
    assert all(S.is_text(r) for r in picked)
    assert len({S.base_id(r["id"]) for r in picked}) == len(picked)                      # one copy per base id
    assert info["safety"] == {"available": 12, "requested": 6, "picked": 6}
    again, _ = S.stratify(pool(), 4, 3, 6, seed=1)
    assert [r["id"] for r in again] == [r["id"] for r in picked]                          # deterministic
    other, _ = S.stratify(pool(), 4, 3, 6, seed=2)
    assert [r["id"] for r in other] != [r["id"] for r in picked]


def test_stratify_short_bucket_reports_shortfall_not_padding():
    picked, info = S.stratify(pool(), per_bucket=30, anchor_per_bucket=30, safety_n=100, seed=0)
    assert info["session"] == {"available": 4, "requested": 30, "picked": 4}
    assert sum(1 for r in picked if S.bucket_of(r) == "session") == 4


def test_bucket_follows_the_export_r5_bucket_order():
    r = parsed_row(call_row("a", family="compose_r3_extra"))
    assert S.bucket_of(r) == "safety"
    assert S.bucket_of(parsed_row(call_row("a", family="narrate_r3"))) == "other"               # a call step outside the lists
    assert S.bucket_of(parsed_row(forced_row("a"))) == "narrate"
    assert S.bucket_of(parsed_row(forced_row("a", family="compose_r3"))) == "narrate"           # narrate is tested before safety
    assert S.bucket_of(parsed_row(forced_row("a", family="followup_r3"))) == "session"           # ... and session before narrate
    assert S.bucket_of(parsed_row(call_row("a", family="followup_r3"))) == "session"
    assert S.bucket_of(parsed_row(call_row("a", family="session_recall_r4", kind="decision"))) == "session"
    assert S.bucket_of(parsed_row(chat_row("a", kind="session_anchor", family="session_chat_r4"))) == "session_anchor"   # anchors stay apart
    assert S.bucket_of(parsed_row(chat_row("a"))) == "chat_anchor"
    assert S.bucket_of(parsed_row(call_row("a", family="mail_r3", kind="select"))) == "select"


def test_bucket_families_match_the_export():
    import export_prompts as E
    assert set(S.SAFETY_FAMILIES) == set(E.COMPOSE_SAFETY_FAMILIES)
    assert set(S.SESSION_FAMILIES) == set(E.R5_SESSION_FAMILIES)
    for kind, fam in (("call", "restraint_r4"), ("narrate", "mail_r3"), ("recall", "session_chat_r4"), ("select", "device_r3"),
                      ("call", "followup_r3"), ("chat_anchor", "chat_voice_r4")):
        it = {"family": fam, "label": {"rl_kind": kind}}
        want = E.r5_bucket(it)
        got = S.bucket_of(it)
        assert got == want, (kind, fam, got, want)


def test_other_bucket_is_stratified_per_rl_kind():
    rows = [parsed_row(call_row(f"c{i}#0", family="misc_r3")) for i in range(6)] + \
           [parsed_row(call_row(f"d{i}#0", family="misc_r3", kind="decision")) for i in range(6)]
    picked, info = S.stratify(rows, 3, 3, 5, seed=0)
    assert info == {"other:call": {"available": 6, "requested": 3, "picked": 3}, "other:decision": {"available": 6, "requested": 3, "picked": 3}}
    assert {S.bucket_of(r) for r in picked} == {"other"}


# ------------------------------------------------------------------ the share arithmetic

def smp(total, parts=None, truncated=False, tokens=100, exact=True):
    parts = dict(parts or {})
    if truncated:
        parts = {"malformed": -1.0}
        total = -1.0
    return {"total": total, "parts": parts, "flags": [], "truncated": truncated, "malformed": parts.get("malformed", 0) < 0,
            "tokens": tokens, "tokens_exact": exact, "text": "t"}


def grp(gid, kind, ceiling, samples, route="completions", own=None, mt=1024):
    return {"id": gid, "family": "f", "rl_kind": kind, "ceiling": ceiling, "route": route, "samples": samples,
            "max_tokens": mt, "label_max_tokens": own}


def test_zero_variance_and_all_pass_all_fail_split():
    groups = [
        grp("a", "call", 1.5, [smp(1.5)] * 4),                                    # all pass
        grp("b", "call", 1.5, [smp(1.0)] * 4),                                    # zero-variance, below the ceiling: all fail
        grp("c", "call", 1.5, [smp(1.5), smp(1.5), smp(-2.0, {"forbidden": -2.0}), smp(1.5)]),   # informative, mixed
        grp("d", "call", 1.5, [smp(-1.0, truncated=True)] * 4),                  # all truncated: zero-variance fail, zero advantage
    ]
    st = S.bucket_stats(groups)
    assert st["zero_variance_share"] == 0.75 and st["zv_all_pass_share"] == 0.25 and st["zv_all_fail_share"] == 0.5
    assert st["informative_unsafe_share"] == 0.25                                  # only c fires forbidden on some but not all
    assert st["truncation_share"] == 0.25 and st["truncated_groups_share"] == 0.25
    assert st["zero_advantage_share"] == 0.75                                      # a, b, d (c has varying kept samples)
    assert st["pg_bearing"] and st["n_prompts"] == 4 and st["n_samples"] == 16
    assert [p["id"] for p in st["per_prompt"] if p["all_pass"]] == ["a"]


def test_informative_shares_unsafe_all_fire_is_not_informative_and_facts_terms():
    groups = [
        grp("a", "narrate", 2.0, [smp(0.5, {"forbidden": -2.0})] * 3),                         # all fire: not informative
        grp("b", "narrate", 2.0, [smp(2.0, {"facts": 1.0}), smp(1.0, {"facts": 0.0}), smp(2.0, {"facts": 1.0})]),
        grp("c", "narrate", 2.0, [smp(2.0, {"facts": 1.0}), smp(-1.0, truncated=True), smp(-1.0, truncated=True)]),   # one parsed sample
        grp("d", "narrate", 2.0, [smp(1.5, {"nothing_claim": -1.0, "facts": 1.0}), smp(2.0, {"facts": 1.0}), smp(2.0, {"facts": 1.0})]),
    ]
    st = S.bucket_stats(groups)
    assert st["informative_unsafe_share"] == 0.25 and st["informative_facts_share"] == 0.25   # b only; d varies in nothing_claim (unsafe)


def test_anchor_buckets_are_not_pg_bearing_and_have_no_split():
    st = S.bucket_stats([grp("a", "chat_anchor", 1.0, [smp(1.0)] * 3), grp("b", "chat_anchor", 1.0, [smp(1.0), smp(0.0), smp(1.0)])])
    assert not st["pg_bearing"] and st["zv_all_pass_share"] is None and st["zero_variance_share"] == 0.5
    assert not st["over_60_zero_variance"]


def test_length_stats_nearest_rank_and_cap_arithmetic():
    xs = list(range(1, 101))
    ls = S.length_stats(xs)
    assert (ls["mean"], ls["p50"], ls["p99"], ls["max"]) == (50.5, 50, 99, 100)
    assert S.length_stats([])["p99"] is None
    mk = lambda kind, lens, trunc=0: S.bucket_stats([grp(f"{kind}{i}", kind, 1.0, [smp(1.0, tokens=n, truncated=(j < trunc and i == 0)) for j, n in enumerate(lens)])
                                                     for i in range(2)])
    buckets = {"narrate": mk("narrate", [100, 200, 300, 400]), "safety": mk("safety", [100, 150, 700, 120]), "select": mk("select", [50] * 4)}
    cap = S.cap_report(buckets, False)
    assert cap["suggested_cap"] == 700 + 64 and cap["suggested_cap_bucket"] == "safety"
    assert cap["maxc_recommendation"] == 768 and cap["censored_buckets"] == []                  # 764 < the 768 default
    buckets["safety"] = mk("safety", [100, 150, 900, 120])
    assert S.cap_report(buckets, False)["maxc_recommendation"] == 964


def test_censored_bucket_raises_maxc_and_own_cap_buckets_use_reexport():
    cens = S.bucket_stats([grp(f"n{i}", "narrate", 2.0, [smp(2.0, tokens=500), smp(-1.0, truncated=True, tokens=1024)]) for i in range(2)])
    assert cens["truncation_over_1pct"]
    chat = S.bucket_stats([grp(f"h{i}", "chat_anchor", 1.0, [smp(1.0, tokens=300), smp(-1.0, truncated=True, tokens=1024)], own=1024) for i in range(2)])
    cap = S.cap_report({"narrate": cens, "chat_anchor": chat}, False)
    assert cap["censored_buckets"] == ["narrate"] and cap["maxc_recommendation"] == 1536
    assert cap["reexport_label_max_tokens"] == ["chat_anchor"] and cap["suggested_cap"] == 1024 + 64      # chat is not in the flag's cap
    assert "1536" in cap["note"]


def test_reconstructed_or_inexact_lengths_withhold_suggested_cap():
    st = S.bucket_stats([grp("a", "narrate", 2.0, [smp(2.0, tokens=100)] * 3, route="reconstructed")])
    assert st["route"] == "reconstructed"
    assert S.cap_report({"narrate": st}, True)["suggested_cap"] is None
    inexact = S.bucket_stats([grp("a", "narrate", 2.0, [smp(2.0, tokens=None, exact=False)] * 3)])
    assert S.cap_report({"narrate": inexact}, False)["suggested_cap"] is None


def test_mix_weighted_zv0_over_pg_buckets_only():
    b = {"safety": {"pg_bearing": True, "zv_all_pass_share": 0.5}, "narrate": {"pg_bearing": True, "zv_all_pass_share": 0.1},
         "chat_anchor": {"pg_bearing": False, "zv_all_pass_share": None}, "recall": {"pg_bearing": True, "zv_all_pass_share": 1.0}}
    mix = {"safety": 300, "narrate": 100, "chat_anchor": 90, "select": 50}                   # recall unsampled in the mix: ignored
    assert S.mix_weighted(b, mix, "zv_all_pass_share") == round((300 * 0.5 + 100 * 0.1) / 400, 4)
    assert S.mix_weighted({}, mix, "zv_all_pass_share") is None


def test_ungrounded_review_lists_flagged_samples():
    bad = smp(1.5, {"ungrounded": -0.5})
    bad["flags"] = ["ungrounded: ['Zed']"]
    st = S.bucket_stats([grp("a", "narrate", 2.0, [bad, smp(2.0), smp(2.0)]), grp("b", "narrate", 2.0, [smp(2.0)] * 3)], review_n=5)
    u = st["ungrounded"]
    assert u["flagged_samples"] == 1 and u["scored_samples"] == 6 and u["flagged_share"] == round(1 / 6, 4)
    assert u["review"][0]["id"] == "a" and "hand" in u["hand_check"]


# ------------------------------------------------------------------ chat-completions reconstruction fallback

def message_for(ideal: str, forced: bool) -> dict:
    """What the chat endpoint would return for this ideal: thought as reasoning_content, calls as tool_calls."""
    p = W.parse(ideal, forced)
    msg = {"role": "assistant", "content": p.text, "reasoning_content": p.thought or None}
    if p.calls:
        msg["tool_calls"] = [{"id": "c", "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                             for c in p.calls]
    return msg


@pytest.mark.parametrize("make", [call_row, forced_row, chat_row])
def test_reconstructed_ideal_parses_and_scores_like_the_ideal(make):
    row = parsed_row(make("r1"))
    ideal, forced = row["label"]["ideal_completion"], row["label"]["forced"]
    text = S.reconstruct(message_for(ideal, forced), forced)
    pa, pb = W.parse(text, forced), W.parse(ideal, forced)
    assert pa.calls == pb.calls and pa.text == pb.text and pa.thought == pb.thought and not pa.problems
    assert W.score(text, row["label"], rnd=5).total == W.score(ideal, row["label"], rnd=5).total


def test_reconstruct_nested_args_and_content_thought_markers():
    msg = {"content": "<|channel>thought\nhmm<channel|>Done.", "tool_calls": [
        {"function": {"name": "add_reminder", "arguments": json.dumps({"title": "a, b}", "n": 3, "ok": True, "l": ["x", "y"], "o": {"k": 1.5}})}}]}
    text = S.reconstruct(msg, False)
    p = W.parse(text, False)
    assert not p.problems and p.thought == "hmm" and p.text == "Done."
    assert p.calls == [{"name": "add_reminder", "args": {"title": "a, b}", "n": 3, "ok": True, "l": ["x", "y"], "o": {"k": 1.5}}}]


def test_chat_route_end_to_end_is_labelled_reconstructed_and_withholds_cap(tmp_path):
    rows = [dict(call_row("c1"), messages=[{"role": "user", "content": "what is on c1?"}], tools=[]),
            dict(forced_row("f1"), messages=[{"role": "user", "content": "what now?"}], tools=[])]
    table = {r["messages"][0]["content"]: message_for(label_of(r)["ideal_completion"], label_of(r)["forced"]) for r in rows}
    stub = Stub(lambda body: (table[body["messages"][-1]["content"]], "stop"))
    p = write_jsonl(tmp_path / "p.jsonl", rows)
    assert run(base_argv(tmp_path, p, ["--route", "chat"]), stub) == 0
    paths = {path for path, _ in stub.requests}
    assert paths == {"/v1/models", "/v1/chat/completions"}                                   # no completions call on the fallback
    rep = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert rep["route"] == "reconstructed" and rep["reconstructed"] is True
    assert rep["zv0_pass"] is None and rep["zv0_fail"] is None and rep["suggested_cap"] is None
    assert all(b["route"] == "reconstructed" for b in rep["buckets"].values())
    assert rep["bos_check"]["skipped"]
    rep_mean = {b: st["mean_score"] for b, st in rep["buckets"].items()}
    for r in rows:
        assert rep_mean[S.bucket_of(parsed_row(r))] == round(label_of(r)["ceiling"], 4)


def test_chat_route_needs_raw_messages():
    with pytest.raises(ValueError):
        S.sample_chat(S.Client("x", transport=Stub(lambda b: ({}, "stop")).transport), MODEL, parsed_row(call_row("a")), 1, 0, 100)


# ------------------------------------------------------------------ end to end

def test_end_to_end_report_fields_dev_slice_and_resume(tmp_path):
    train = [call_row(f"s{i}#0", family="restraint_r4") for i in range(5)] + [forced_row(f"n{i}#0") for i in range(4)] + \
            [chat_row(f"h{i}#0") for i in range(3)] + [mm_row("v0#0")]
    dev = [call_row(f"ds{i}#0", family="restraint_r4") for i in range(4)] + [forced_row("dn#0")]
    p, d = write_jsonl(tmp_path / "p.jsonl", train), write_jsonl(tmp_path / "d.jsonl", dev)
    ideal_of = {parsed_row(r)["prompt"]: label_of(r)["ideal_completion"] for r in train + dev if "ideal_completion" in label_of(r)}
    state = {"i": 0}

    def reply(body):                                     # every third call to the chat anchors is a ramble, to exercise truncation
        state["i"] += 1
        if "hello" in body["prompt"] and state["i"] % 2 == 0:
            return "x " * 5, "length"
        return ideal_of[body["prompt"]], "stop"
    stub = Stub(reply)
    argv = base_argv(tmp_path, p, ["--text-dev", d])
    assert run(argv, stub) == 0
    rep = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert rep["schema"] == S.SCHEMA and rep["round"] == 5 and rep["G"] == 3 and rep["route"] == "completions"
    assert set(rep["buckets"]) == {"safety", "narrate", "chat_anchor"} and set(rep["dev"]) == {"dev:safety"}
    assert rep["buckets"]["safety"]["n_prompts"] == 3 and rep["dev"]["dev:safety"]["n_prompts"] == 2   # --safety-train 3, --safety-dev 2
    assert rep["buckets"]["narrate"]["n_prompts"] == 2 and rep["multimodal_rows_skipped"] == 1
    nar = rep["buckets"]["narrate"]
    for key in ("zero_variance_share", "zv_all_pass_share", "zv_all_fail_share", "informative_unsafe_share", "informative_facts_share",
                "truncation_share", "completion_tokens", "per_prompt", "ungrounded"):
        assert key in nar
    assert nar["zero_variance_share"] == 1.0 and nar["zv_all_pass_share"] == 1.0 and nar["truncation_share"] == 0.0
    assert set(nar["completion_tokens"]) == {"n", "mean", "p50", "p99", "max"} and nar["completion_tokens"]["n"] == 6
    assert len(nar["per_prompt"][0]["scores"]) == 3
    assert rep["buckets"]["chat_anchor"]["truncation_share"] > 0 and not rep["buckets"]["chat_anchor"]["pg_bearing"]
    assert rep["suggested_cap"] is not None and rep["cap"]["suggested_cap_bucket"] in rep["buckets"]
    assert rep["zv0_pass"] == 1.0 and rep["zv0_fail"] == 0.0 and rep["export_mix"] == {"safety": 5, "narrate": 4}
    assert len(rep["always_solved_row_ids"]) == 5 and len(rep["always_solved_base_ids"]) == 5
    assert set(rep["always_solved_by_bucket"]) == {"safety", "narrate"}
    assert rep["reexport_buckets"] == ["narrate", "safety"]                                    # both > 60% zero-variance
    assert rep["by_rl_kind"]["narrate"]["n_prompts"] == 2 and "http_calls" in rep and rep["bos_check"]["skipped"]
    assert rep["moe_dir_set"] is True and not rep["errors"]
    assert rep["selection"]["dev:safety"]["marker"] == "family prefix"                       # this dev file carries no slice marker
    assert rep["session_bucket"]["n_prompts_sampled"] == 0
    n_calls = len(stub.requests)
    # a rerun resumes from the sample cache: no new completions requests
    stub2 = Stub(reply)
    assert run(argv, stub2) == 0
    assert [p for p, _ in stub2.requests] == ["/v1/models"] and n_calls > 1
    rep2 = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert rep2["buckets"]["narrate"]["per_prompt"] == nar["per_prompt"]
    # a changed seed is a different run: the cache does not apply
    stub3 = Stub(reply)
    run(argv + ["--seed", "1"], stub3)
    assert len(stub3.requests) > 1


def test_server_errors_mark_rows_incomplete_and_exit_3(tmp_path):
    rows = [call_row("a#0", family="restraint_r4"), call_row("b#0", family="restraint_r4")]
    p = write_jsonl(tmp_path / "p.jsonl", rows)

    def reply(body):
        if "b#0" in body["prompt"]:
            raise ValueError("boom")
        return label_of(rows[0])["ideal_completion"], "stop"
    stub = Stub(reply)
    rc = run(base_argv(tmp_path, p), stub)
    assert rc == 3
    rep = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert rep["incomplete_rows"] == ["b#0"] and rep["buckets"]["safety"]["n_prompts"] == 1


def test_missing_choices_is_retried_then_errors():
    calls = {"n": 0}

    def tr(path, body):
        calls["n"] += 1
        return {"usage": {}}                                   # oMLX under contention drops `choices`
    c = S.Client("x", retries=2, transport=tr)
    with pytest.raises(RuntimeError):
        c.request("/v1/completions", {"prompt": "x"})
    assert calls["n"] == 3


# ------------------------------------------------------------------ review fixes: mixed caps, session bucket, guards

def test_mixed_cap_bucket_is_decided_per_row_not_per_bucket():
    gov = [grp(f"n{i}", "narrate", 2.0, [smp(2.0, tokens=900)] * 3) for i in range(9)]
    own = [grp("f0", "narrate", 2.0, [smp(2.0, tokens=100)] * 3, own=1024)]                  # a followup_r3 narrate step with its own cap
    cap = S.cap_report({"session": S.bucket_stats(own), "narrate": S.bucket_stats(gov + own)}, False)
    assert cap["suggested_cap"] == 900 + 64 and cap["suggested_cap_bucket"] == "narrate"        # the old per-bucket test said 264
    mixed = S.bucket_stats(gov + own)
    assert mixed["governed"]["n_prompts"] == 9 and mixed["own_cap"]["n_prompts"] == 1
    assert mixed["governed"]["completion_tokens"]["p99"] == 900 and mixed["own_cap"]["completion_tokens"]["p99"] == 100


def test_truncation_is_attributed_to_the_flag_or_to_label_max_tokens_by_row():
    cut = lambda: [smp(2.0, tokens=500), smp(-1.0, truncated=True, tokens=1024)]
    fine = lambda: [smp(2.0, tokens=300)] * 2
    # the governed rows truncate: the flag is censored even though the bucket also holds clean own-cap rows
    a = S.bucket_stats([grp("g", "narrate", 2.0, cut()), grp("o", "narrate", 2.0, fine(), own=1024)])
    ca = S.cap_report({"narrate": a}, False)
    assert ca["censored_buckets"] == ["narrate"] and ca["reexport_label_max_tokens"] == [] and ca["maxc_recommendation"] == 1536
    # the own-cap rows truncate: re-export label.max_tokens, leave the flag alone
    b = S.bucket_stats([grp("g", "narrate", 2.0, fine()), grp("o", "narrate", 2.0, cut(), own=1024)])
    cb = S.cap_report({"narrate": b}, False)
    assert cb["censored_buckets"] == [] and cb["reexport_label_max_tokens"] == ["narrate"]
    assert cb["suggested_cap"] == 300 + 64 and cb["suggested_cap_all_text"] == 1024 + 64          # the literal RL-SCOPE reading, kept next to it
    assert cb["suggested_cap_all_text_bucket"] == "narrate"


def test_ungrounded_review_carries_the_ground_text_and_informative_any():
    g = grp("u", "narrate", 2.0, [smp(1.0, {"ungrounded": -1.0}), smp(2.0)])
    g.update(ground="Ethics at 11 AM.", facts=["11 AM"], names=["sam"])
    g["samples"][0]["flags"] = ["ungrounded:12"]
    st = S.bucket_stats([g])
    rv = st["ungrounded"]["review"][0]
    assert rv["ground"] == "Ethics at 11 AM." and rv["facts"] == ["11 AM"] and rv["names"] == ["sam"] and rv["family"] == "f"
    assert st["informative_facts_share"] == 1.0 and st["informative_any_share"] == 1.0


def test_session_bucket_reports_composition_and_informative_share(tmp_path):
    rows = [forced_row(f"f{i}#0", family="followup_r3") for i in range(3)] + [forced_row("f0#1", family="followup_r3")] + \
           [chat_row(f"r{i}#0", kind="recall", family="session_chat_r4") for i in range(2)] + \
           [chat_row("q0#0", kind="session_anchor", family="session_chat_r4"), forced_row("n0#0"), call_row("s0#0", family="restraint_r4")]
    ideal_of = {parsed_row(r)["prompt"]: label_of(r)["ideal_completion"] for r in rows}
    stub = Stub(lambda body: (ideal_of[body["prompt"]], "stop"))
    assert run(base_argv(tmp_path, write_jsonl(tmp_path / "p.jsonl", rows), ["--per-bucket", "5"]), stub) == 0
    rep = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert set(rep["buckets"]) == {"session", "session_anchor", "narrate", "safety"}
    ses = rep["session_bucket"]
    assert ses["n_prompts_sampled"] == 5 and ses["composition"]["pg_total"] == 5 and ses["composition"]["anchor_total"] == 1
    assert ses["composition"]["pg_bearing"]["followup_r3:narrate"] == 3 and ses["composition"]["pg_bearing"]["session_chat_r4:recall"] == 2
    assert ses["informative_any_share"] == 0.0 and ses["zero_variance_share"] == 1.0
    assert rep["buckets"]["session"]["pg_bearing"] and not rep["buckets"]["session_anchor"]["pg_bearing"]


def test_moe_dir_must_not_be_the_live_directory(tmp_path):
    home = tmp_path / "home"
    (home / ".moe").mkdir(parents=True)
    for bad in (str(home / ".moe"), str(home / ".moe") + "/", "~/.moe"):
        with pytest.raises(SystemExit) as e:
            S.check_environment({"MOE_DIR": bad, "HOME": str(home)})
        assert e.value.code == 2
    assert S.check_environment({"MOE_DIR": str(home / "iso" / ".moe-test"), "HOME": str(home)})
    assert S.check_environment({"MOE_DIR": str(tmp_path / "iso"), "HOME": str(home)})


def test_token_counts_with_mode_n_is_rejected_up_front(tmp_path):
    rows = [call_row("a#0", family="restraint_r4")]
    p = write_jsonl(tmp_path / "p.jsonl", rows)
    counts = tmp_path / "c.json"
    counts.write_text(json.dumps({"a#0": 5}))
    argv = [x for x in base_argv(tmp_path, p, ["--mode", "n", "--token-counts", str(counts)]) if x != "--skip-bos-check"]
    stub = Stub(echo(rows))
    with pytest.raises(SystemExit) as e:
        run(argv, stub)
    assert e.value.code == 2 and stub.requests == []


def test_token_counts_missing_rows_are_skipped_and_all_missing_stops(tmp_path):
    rows = [call_row(f"a{i}#0", family="restraint_r4") for i in range(4)]
    p = write_jsonl(tmp_path / "p.jsonl", rows)
    argv = [x for x in base_argv(tmp_path, p) if x != "--skip-bos-check"]
    stub = Stub(echo(rows))
    counts = tmp_path / "c.json"
    prompts = {r["id"]: r["prompt"] for r in rows}
    counts.write_text(json.dumps({"a0#0": stub.token_fn(prompts["a0#0"]), "a1#0": stub.token_fn(prompts["a1#0"])}))
    assert run(argv + ["--token-counts", str(counts)], stub) == 0                          # two rows have no count: skipped, not a KeyError
    rep = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert rep["bos_check"]["ok"] and rep["bos_check"]["checked"] == 2 and rep["bos_check"]["rows_without_count"] == 1   # 3 sampled, 2 counted
    counts.write_text(json.dumps({"zzz": 3}))
    with pytest.raises(SystemExit) as e:
        run(argv + ["--token-counts", str(counts)], Stub(echo(rows)))
    assert e.value.code == 4


def test_dev_safety_prefers_the_slice_marker_and_falls_back_to_the_family(tmp_path):
    train = [call_row("s0#0", family="restraint_r4"), forced_row("n0#0")]
    dev = [dict(call_row(f"d{i}#0", family="some_future_family"), slice="safety", samples=12) for i in range(3)] + \
          [dict(call_row("d9#0", family="restraint_r4"), slice="narrate", samples=6), dict(forced_row("dn#0"), slice="narrate", samples=6)]
    rows_all = train + dev
    ideal_of = {parsed_row(r)["prompt"]: label_of(r)["ideal_completion"] for r in rows_all}
    stub = Stub(lambda body: (ideal_of[body["prompt"]], "stop"))
    argv = base_argv(tmp_path, write_jsonl(tmp_path / "p.jsonl", train), ["--text-dev", write_jsonl(tmp_path / "d.jsonl", dev)])
    assert run(argv, stub) == 0
    rep = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert rep["selection"]["dev:safety"] == {"available": 3, "requested": 2, "picked": 2, "marker": "row.slice"}
    assert rep["dev"]["dev:safety"]["n_prompts"] == 2
    assert S.is_dev_safety({"slice": "narrate", "family": "restraint_r4"}) is False and S.is_dev_safety({"family": "restraint_r4"}) is True


def test_resume_cache_is_keyed_by_the_weights_and_the_request_shape(tmp_path):
    rows = [call_row(f"s{i}#0", family="restraint_r4") for i in range(2)]
    p = write_jsonl(tmp_path / "p.jsonl", rows)
    w = tmp_path / "weights"
    w.mkdir()
    (w / "model.safetensors").write_bytes(b"a" * 10)
    (w / "config.json").write_text("{}")
    argv = base_argv(tmp_path, p, ["--model-path", str(w)])
    assert run(argv, Stub(echo(rows))) == 0
    again = Stub(echo(rows))
    assert run(argv, again) == 0 and [q for q, _ in again.requests] == ["/v1/models"]            # same weights: resumed
    (w / "model.safetensors").write_bytes(b"b" * 11)                                             # a different SFT under the same served name
    swapped = Stub(echo(rows))
    assert run(argv, swapped) == 0 and any(q == "/v1/completions" for q, _ in swapped.requests)
    other = Stub(echo(rows))
    assert run(argv + ["--omit-fields", "add_special_tokens"], other) == 0 and any(q == "/v1/completions" for q, _ in other.requests)
    # the engine's own /v1/models identity counts too
    class Listed(Stub):
        def transport(self, path, body):
            out = super().transport(path, body)
            if path == "/v1/models":
                out["data"][0]["path"] = self.where
            return out
    a, b = Listed(echo(rows)), Listed(echo(rows))
    a.where, b.where = "/m/one", "/m/two"
    argv2 = base_argv(tmp_path, p, ["--samples-out", str(tmp_path / "s2.jsonl")])
    assert run(argv2, a) == 0 and any(q == "/v1/completions" for q, _ in a.requests)
    assert run(argv2, b) == 0 and any(q == "/v1/completions" for q, _ in b.requests)


def test_always_solved_lists_base_ids_and_every_copy(tmp_path):
    rows = [call_row("s0#0", family="restraint_r4"), call_row("s0#1", family="restraint_r4"), call_row("s0#2", family="restraint_r4"),
            call_row("s1#0", family="restraint_r4"), call_row("s1#1", family="restraint_r4")]
    ideal_of = {parsed_row(r)["prompt"]: label_of(r)["ideal_completion"] for r in rows}
    bad = call("send_email", to="x") + "<|tool_response>"

    def reply(body):
        return (bad if "s1#" in body["prompt"] else ideal_of[body["prompt"]]), "stop"
    assert run(base_argv(tmp_path, write_jsonl(tmp_path / "p.jsonl", rows)), Stub(reply)) == 0
    rep = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert rep["always_solved_base_ids"] == ["s0"]
    assert rep["always_solved_row_ids"] == ["s0#0", "s0#1", "s0#2"]                                # an id filter drops every copy
    assert rep["always_solved_by_family"]["restraint_r4"] == {"sampled": 2, "always_solved": 1}


def test_max_tokens_override_means_no_row_uses_its_own_cap():
    row = parsed_row(chat_row("h#0"))
    row["label"]["max_tokens"] = 300
    raw = {"h#0": [{"text": "ok<turn|>", "truncated": False, "tokens": 3, "tokens_exact": True, "route": "completions", "max_tokens": 77}] * 2}
    own = S.build_groups([row], raw, 5, S.SAFETY_FAMILIES)["chat_anchor"][0]
    gov = S.build_groups([row], raw, 5, S.SAFETY_FAMILIES, override=77)["chat_anchor"][0]
    assert own["label_max_tokens"] == 300 and gov["label_max_tokens"] is None


# ------------------------------------------------------------------ the same stub behind real loopback HTTP

def _serve(stub):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            raw = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._send(stub.transport(self.path, None))

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._send(stub.transport(self.path, body))
    try:
        srv = HTTPServer(("127.0.0.1", 0), H)
    except PermissionError:
        pytest.skip("this sandbox does not allow binding a loopback port")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

# ------------------------------------------------------------------ verifier round 2: weights, notes, cap note, the ideal check

def test_other_bucket_shares_are_weighted_by_each_kinds_share_of_the_export():
    """stratify() takes equal rows per rl_kind, so an unweighted bucket-level share over-counts the rare kinds."""
    easy = [grp(f"c{i}", "call", 1.5, [smp(1.5)] * 4) for i in range(3)]                      # all pass
    hard = [grp(f"d{i}", "decision", 1.0, [smp(1.0), smp(0.0)] * 2) for i in range(3)]        # informative
    flat = S.bucket_stats(easy + hard)
    assert flat["zero_variance_share"] == 0.5 and flat["shares_weighted"] is False and flat["kind_weights"] is None
    for g in easy:
        g["weight"] = 9.0                                                                       # 'call' is 90% of the bucket's rows
    for g in hard:
        g["weight"] = 1.0
    w = S.bucket_stats(easy + hard)
    assert w["shares_weighted"] and w["kind_weights"] == {"call": 9.0, "decision": 1.0}
    assert w["zero_variance_share"] == 0.9 and w["zv_all_pass_share"] == 0.9 and w["zv_all_fail_share"] == 0.0
    assert w["over_60_zero_variance"] and not flat["over_60_zero_variance"]                    # the weighted share decides the re-export
    assert w["n_prompts"] == 6 and w["n_samples"] == 24                                         # counts stay unweighted


def test_other_weights_come_from_the_full_export_rows_and_feed_the_report(tmp_path):
    pool = [parsed_row(call_row(f"c{i}#{k}", family="misc_r3")) for i in range(8) for k in range(2)] + \
           [parsed_row(call_row(f"d{i}#0", family="misc_r3", kind="decision")) for i in range(2)]
    picked, sel = S.stratify(pool, 2, 2, 5, seed=0)
    groups = [grp(r["id"], r["label"]["rl_kind"], 1.0, [smp(1.0)] * 3) for r in picked]
    w = S.other_weights(groups, pool, sel)
    assert w == {"call": 16 / 2, "decision": 2 / 2}                                            # rows (copies counted), per sampled group
    assert S.other_weights(groups, None, sel) == {"call": 8 / 2, "decision": 2 / 2}            # else the deduplicated pool sizes
    rows = [call_row(f"c{i}#0", family="misc_r3") for i in range(6)] + [call_row(f"d{i}#0", family="misc_r3", kind="decision") for i in range(2)]
    p = write_jsonl(tmp_path / "p.jsonl", rows)
    ideal_of = {parsed_row(r)["prompt"]: label_of(r)["ideal_completion"] for r in rows}
    wrong = call("get_upcoming", days=9) + "<|tool_response>"
    state = {"n": 0}

    def reply(body):                      # decisions: half the samples wrong (informative); calls: always the ideal
        if "d0" in body["prompt"] or "d1" in body["prompt"]:
            state["n"] += 1
            return (wrong if state["n"] % 2 else ideal_of[body["prompt"]]), "stop"
        return ideal_of[body["prompt"]], "stop"
    assert run(base_argv(tmp_path, p, ["--per-bucket", "2", "--G", "4"]), Stub(reply)) == 0
    rep = json.loads((tmp_path / "pre-ok-report.json").read_text())
    other = rep["buckets"]["other"]
    assert other["shares_weighted"] and other["kind_weights"]["call"] == 3.0 and other["kind_weights"]["decision"] == 1.0
    assert other["zero_variance_share"] == 0.75          # 3 of 4 weighted: the 2 sampled calls count 3x each, the decisions 1x each
    assert rep["by_rl_kind"]["call"]["zero_variance_share"] == 1.0
    assert "r5_bucket" in rep["bucket_notes"]["session_anchor"] and "kind_weights" in rep["bucket_notes"]["other"]


def test_session_anchor_is_its_own_bucket_but_a_session_pg_row_is_session():
    anchor = {"family": "session_chat_r4", "label": {"rl_kind": "session_anchor"}}
    pg = {"family": "session_chat_r4", "label": {"rl_kind": "recall"}}
    assert S.bucket_of(anchor) == "session_anchor" and S.bucket_of(pg) == "session"
    assert not S.bucket_stats([grp("a", "session_anchor", 1.0, [smp(1.0)] * 3)])["pg_bearing"]


def test_cap_report_with_only_own_cap_rows_says_so_and_still_reads_the_exact_counts():
    own = S.bucket_stats([grp(f"h{i}", "chat_anchor", 1.0, [smp(1.0, tokens=1500)] * 3, own=1024) for i in range(2)])
    cap = S.cap_report({"chat_anchor": own}, False)
    assert cap["suggested_cap"] is None and cap["suggested_cap_all_text"] == 1500 + 64 and cap["suggested_cap_all_text_bucket"] == "chat_anchor"
    assert "no cap-governed rows" in cap["note"] and "no exact per-sample token counts" not in cap["note"]
    assert cap["governed_prompts"] == {} and cap["own_cap_prompts"] == {"chat_anchor": 2}
    # inexact counts: the old note still applies, and nothing is invented
    inexact = S.bucket_stats([grp("a", "narrate", 2.0, [smp(2.0, tokens=None, exact=False)] * 3)])
    c2 = S.cap_report({"narrate": inexact}, False)
    assert c2["suggested_cap"] is None and c2["suggested_cap_all_text"] is None and "no exact per-sample token counts" in c2["note"]
    # a bucket with governed rows keeps both caps
    gov = S.bucket_stats([grp("n0", "narrate", 2.0, [smp(2.0, tokens=300)] * 3)])
    c3 = S.cap_report({"narrate": gov, "chat_anchor": own}, False)
    assert c3["suggested_cap"] == 364 and c3["suggested_cap_all_text"] == 1564


def exported_style_row(rid, ideal, ceiling, kind="call", family="gather_then_send_r4", **label_kw):
    """A row whose ceiling comes from the TABLE (as the export writes it), not from scoring the ideal: the way a bad ideal shows."""
    label = lab(rl_kind=kind, ideal_completion=ideal, ceiling=ceiling, **label_kw)
    return {"id": rid, "family": family, "prompt": f"{BOS}<|turn>user\nq {rid}<turn|>\n<|turn>model\n", "label": json.dumps(label), "modality": "text"}


def test_ideal_check_catches_an_ideal_that_misses_the_table_ceiling():
    """The audit's finding: call:send_message{} (empty args) against a 1.5 call ceiling scored 1.0 and no stub could tell."""
    exp = {"expected": [{"name": "get_upcoming", "args": {"days": 2}}], "key_args": [{"days": 2}], "present_args": [[]]}
    good = exported_style_row("g#0", call("get_upcoming", days=2) + "<|tool_response>", 1.5, **exp)
    empty = exported_style_row("e#0", call("get_upcoming") + "<|tool_response>", 1.5, **exp)
    rep = S.ideal_check([parsed_row(good), parsed_row(empty)])
    k = rep["by_rl_kind"]["call"]
    assert not rep["ok"] and rep["failing_kinds"] == ["call"] and (k["n"], k["bad"], k["below_ceiling"]) == (2, 1, 1)
    assert k["examples"][0]["id"] == "e#0" and "below ceiling 1.5" in k["examples"][0]["why"][0] and k["ideal_mean"] == 1.25
    assert S.ideal_check([parsed_row(good)])["ok"]


def test_ideal_check_counts_penalised_ideals_missing_ideals_and_ceilings_above():
    final = {"step": "final", "final": True}
    pen = exported_style_row("p#0", call("send_email", to="a@b.co") + "<|tool_response>", 1.5, family="restraint_r4",
                             expected=[{"name": "get_upcoming", "args": {}}], key_args=[{}], present_args=[[]])
    ok = exported_style_row("k#0", "Your Ethics class is at 11 AM.<turn|>", 1.0, kind="decision", family="restraint_r4", **final)
    nope = exported_style_row("n#0", "", 1.0, kind="decision", family="restraint_r4", **final)
    over = exported_style_row("o#0", "Your Ethics class is at 11 AM.<turn|>", 0.5, kind="decision", family="restraint_r4", **final)
    nocl = parsed_row(exported_style_row("c#0", "Your Ethics class is at 11 AM.<turn|>", 1.0, kind="decision", **final))
    nocl["label"].pop("ceiling")
    rep = S.ideal_check([parsed_row(r) for r in (pen, ok, nope, over)] + [nocl])
    k = rep["by_rl_kind"]["decision"]
    assert k["n"] == 4 and k["missing_ideal"] == 1 and k["above_ceiling"] == 1 and k["no_ceiling"] == 1 and k["bad"] == 3
    c = rep["by_rl_kind"]["call"]
    assert c["n"] == 1 and c["penalised"] == 1 and "forbidden" in c["penalising_terms"] and c["below_ceiling"] == 1
    # the bar is a share per kind: 1 bad in 200 is within 1%
    many = [parsed_row(ok)] * 199 + [parsed_row(nope)]
    assert S.ideal_check(many)["ok"] and not S.ideal_check(many, bar=0.001)["ok"]


def test_ideal_check_gates_the_run_before_any_request_and_can_be_overridden(tmp_path, capsys):
    exp = {"expected": [{"name": "get_upcoming", "args": {"days": 2}}], "key_args": [{"days": 2}], "present_args": [[]]}
    bad = exported_style_row("e#0", call("get_upcoming") + "<|tool_response>", 1.5, **exp)
    good = exported_style_row("g#0", call("get_upcoming", days=2) + "<|tool_response>", 1.5, **exp)
    p = write_jsonl(tmp_path / "p.jsonl", [bad, good])
    ideal_of = {parsed_row(r)["prompt"]: label_of(r)["ideal_completion"] for r in (bad, good)}
    stub = Stub(lambda body: (ideal_of[body["prompt"]], "stop"))
    with pytest.raises(SystemExit) as e:
        run(base_argv(tmp_path, p), stub)
    assert e.value.code == 5 and stub.requests == []                                             # nothing was sent, not even /v1/models
    assert not (tmp_path / "pre-ok-report.json").exists()
    # offline mode: no engine, no MOE_DIR, no served model
    out = tmp_path / "ic.json"
    assert S.main(["--prompts", p, "--ideal-check-only", "--out", str(out)], environ={}) == 5
    assert json.loads(out.read_text())["failing_kinds"] == ["call"]
    assert S.main(["--prompts", write_jsonl(tmp_path / "g.jsonl", [good]), "--ideal-check-only"], environ={}) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    # the override samples, records the failure, and says the zv0 split is not meaningful
    assert run(base_argv(tmp_path, p, ["--allow-ideal-trips"]), stub) == 0
    rep = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert rep["ideal_check"]["ok"] is False and rep["ideal_check_overridden"] is True and "NOT MEANINGFUL" in rep["zv0_basis"]
    # a clean run records the check and no caveat
    stub2 = Stub(lambda body: (ideal_of[body["prompt"]], "stop"))
    assert run(base_argv(tmp_path, write_jsonl(tmp_path / "g2.jsonl", [good])), stub2) == 0
    rep2 = json.loads((tmp_path / "pre-ok-report.json").read_text())
    assert rep2["ideal_check"]["ok"] and rep2["ideal_check_overridden"] is False and "NOT MEANINGFUL" not in rep2["zv0_basis"]



# ------------------------------------------------------------------ verifier round 3: rounds 3/4, tiny kinds, weights, on-policy review

def _bad_ideal_row(rid, kind="decision"):
    return exported_style_row(rid, "", 1.0, kind=kind, family="restraint_r4", step="final", final=True)


def _good_ideal_row(rid, kind="decision"):
    return exported_style_row(rid, "Your Ethics class is at 11 AM.<turn|>", 1.0, kind=kind, family="restraint_r4", step="final", final=True)


@pytest.mark.parametrize("rnd", [3, 4])
def test_ideal_gate_is_skipped_for_rounds_3_and_4_and_says_so(rnd):
    """Round 3/4 exports carry no rl_kind, ideal_completion or ceiling: the gate must not call every row 'unlabelled'."""
    legacy = {"id": "x#0", "family": "misc", "prompt": "p", "modality": "text", "label": {"step": "call", "round": rnd}}
    rep = S.ideal_check([legacy] * 5, rnd)
    assert rep["ok"] and rep["skipped"] is True and rep["failing_kinds"] == [] and "round-5" in rep["reason"]
    assert S.ideal_check([parsed_row(_bad_ideal_row("b#0"))], 5)["skipped"] is False


def test_ideal_gate_main_records_skip_for_round_4_instead_of_exiting_5(tmp_path, capsys):
    rows = [call_row(f"c{i}#0", family="misc_r3") for i in range(2)]
    for r in rows:
        lb = label_of(r)
        for k in ("rl_kind", "ideal_completion", "ceiling"):
            lb.pop(k)
        lb["round"] = 4
        r["label"] = json.dumps(lb)
    p = write_jsonl(tmp_path / "p4.jsonl", rows)
    assert S.main(["--prompts", p, "--round", "4", "--ideal-check-only"], environ={}) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] is True and out["ok"] is True and out["round"] == 4


def test_ideal_bar_is_the_exact_integer_rule_of_plan_and_names_the_rows():
    """A kind of 14 rows tolerates none (one row is 7%); a kind of 100 tolerates one; the report names the bad ids and the count."""
    import plan
    for n, bad in ((14, 1), (14, 0), (99, 1), (100, 1), (100, 2), (101, 1), (300, 3), (300, 4), (700, 7), (700, 8)):
        rows = [parsed_row(_good_ideal_row(f"g{i}#0")) for i in range(n - bad)] + [parsed_row(_bad_ideal_row(f"b{i}#0")) for i in range(bad)]
        rep = S.ideal_check(rows)
        k = rep["by_rl_kind"]["decision"]
        assert rep["ok"] == (not plan.kind_trip_problems({"decision": bad}, {"decision": n})), (n, bad)
        assert k["bad"] == bad and k["allowed"] == n // 100 and k["bad_ids"] == [f"b{i}#0" for i in range(bad)]
        assert k["over_by"] == max(0, bad - n // 100)


def test_ideal_floor_rows_only_relaxes_small_kinds_and_is_recorded():
    rows = [parsed_row(_good_ideal_row(f"g{i}#0")) for i in range(13)] + [parsed_row(_bad_ideal_row("b#0"))]
    assert not S.ideal_check(rows)["ok"]
    rep = S.ideal_check(rows, floor_rows=1)
    assert rep["ok"] and rep["floor_rows"] == 1 and rep["by_rl_kind"]["decision"]["allowed"] == 1 and rep["by_rl_kind"]["decision"]["bad"] == 1
    two = rows[:12] + [parsed_row(_bad_ideal_row("b2#0")), parsed_row(_bad_ideal_row("b3#0"))]
    assert not S.ideal_check(two, floor_rows=1)["ok"]                                  # the floor is a count, not a licence
    big = [parsed_row(_good_ideal_row(f"g{i}#0")) for i in range(98)] + [parsed_row(_bad_ideal_row("b#0")), parsed_row(_bad_ideal_row("c#0"))]
    assert not S.ideal_check(big, floor_rows=1)["ok"]                                   # the floor never applies to a kind of 100


def test_ideal_gate_cli_reports_the_row_ids_of_a_tiny_failing_kind(tmp_path, capsys):
    rows = [_good_ideal_row(f"g{i}#0") for i in range(13)] + [_bad_ideal_row("b#0")]
    p = write_jsonl(tmp_path / "p.jsonl", rows)
    assert S.main(["--prompts", p, "--ideal-check-only"], environ={}) == 5
    cap = capsys.readouterr()
    assert json.loads(cap.out)["by_rl_kind"]["decision"]["bad_ids"] == ["b#0"] and "b#0" in cap.err and "allowed 0" in cap.err
    assert S.main(["--prompts", p, "--ideal-check-only", "--ideal-floor-rows", "1"], environ={}) == 0


def test_mean_score_and_ceiling_mean_are_weighted_like_the_shares():
    easy = [grp(f"c{i}", "call", 1.5, [smp(1.5)] * 4) for i in range(3)]
    hard = [grp(f"d{i}", "decision", 1.0, [smp(1.0), smp(0.0)] * 2) for i in range(3)]
    flat = S.bucket_stats(easy + hard)
    assert flat["mean_score"] == round((1.5 * 12 + 0.5 * 12) / 24, 4) and flat["ceiling_mean"] == 1.25
    for g in easy:
        g["weight"] = 9.0
    for g in hard:
        g["weight"] = 1.0
    w = S.bucket_stats(easy + hard)
    assert w["shares_weighted"] and w["mean_score"] == round(0.9 * 1.5 + 0.1 * 0.5, 4) and w["ceiling_mean"] == round(0.9 * 1.5 + 0.1 * 1.0, 4)
    assert "mean_score" in S.BUCKET_NOTES["other"] and "ceiling_mean" in S.BUCKET_NOTES["other"]


def test_on_policy_penalty_review_lists_flagged_samples_per_term():
    def smp_t(total, **parts):
        s = smp(total)
        s["parts"] = parts
        s["text"] = "txt"
        return s
    g = grp("n1", "narrate", 2.0, [smp_t(2.0), smp_t(1.5, over_mention=-0.5), smp_t(1.5, verbatim=-0.5), smp_t(1.0, over_mention=-0.5, verbose=-0.5)])
    st = S.bucket_stats([g], review_n=1)["on_policy_penalties"]
    assert set(st) == set(S.REVIEW_TERMS)
    assert st["over_mention"]["flagged_samples"] == 2 and st["over_mention"]["scored_samples"] == 4 and st["over_mention"]["flagged_share"] == 0.5
    assert len(st["over_mention"]["review"]) == 1 and st["over_mention"]["review"][0]["id"] == "n1"        # review_n caps the list
    assert st["verbatim"]["flagged_samples"] == 1 and st["nothing_claim"]["flagged_samples"] == 0 and st["nothing_claim"]["flagged_share"] == 0.0


def test_real_http_round_trip_sends_the_prompt_unmodified():
    row = parsed_row(forced_row("f1"))
    stub = Stub(echo([forced_row("f1")]))
    srv = _serve(stub)
    try:
        port = srv.server_address[1]
        assert port not in S.FORBIDDEN_PORTS
        client = S.Client(S.check_base_url(f"http://127.0.0.1:{port}"), timeout=10)
        assert S.served_models(client) == [MODEL]
        recs = S.sample_completions(client, MODEL, row, 2, "calls", 0, 1024)
    finally:
        srv.shutdown()
    assert stub.requests[1][1]["prompt"] == row["prompt"]
    assert recs[0]["text"] == row["label"]["ideal_completion"]


def test_real_http_main_writes_the_report(tmp_path):
    rows = [forced_row(f"n{i}#0") for i in range(3)]
    stub = Stub(echo(rows))
    srv = _serve(stub)
    try:
        port = srv.server_address[1]
        rc = S.main(["--base-url", f"http://127.0.0.1:{port}", "--served-model", MODEL, "--prompts", write_jsonl(tmp_path / "p.jsonl", rows),
                     "--out", str(tmp_path / "r.json"), "--G", "2", "--per-bucket", "2", "--skip-bos-check"], environ={"MOE_DIR": "/tmp/iso"})
    finally:
        srv.shutdown()
    assert rc == 0 and json.loads((tmp_path / "r.json").read_text())["buckets"]["narrate"]["n_prompts"] == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
