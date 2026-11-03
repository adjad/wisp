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
                                                     "recall": 4, "select": 4}
    assert all(S.is_text(r) for r in picked)
    assert len({S.base_id(r["id"]) for r in picked}) == len(picked)                      # one copy per base id
    assert info["safety"] == {"available": 12, "requested": 6, "picked": 6}
    again, _ = S.stratify(pool(), 4, 3, 6, seed=1)
    assert [r["id"] for r in again] == [r["id"] for r in picked]                          # deterministic
    other, _ = S.stratify(pool(), 4, 3, 6, seed=2)
    assert [r["id"] for r in other] != [r["id"] for r in picked]


def test_stratify_short_bucket_reports_shortfall_not_padding():
    picked, info = S.stratify(pool(), per_bucket=30, anchor_per_bucket=30, safety_n=100, seed=0)
    assert info["recall"] == {"available": 4, "requested": 30, "picked": 4}
    assert sum(1 for r in picked if S.bucket_of(r) == "recall") == 4


def test_bucket_is_rl_kind_with_a_safety_carve_out():
    r = parsed_row(call_row("a", family="compose_r3_extra"))
    assert S.bucket_of(r) == "safety"
    assert S.bucket_of(parsed_row(call_row("a", family="narrate_r3"))) == "call"
    assert S.bucket_of(parsed_row(forced_row("a"))) == "narrate"


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
    assert len(rep["always_solved_row_ids"]) == 5
    assert set(rep["always_solved_by_bucket"]) == {"safety", "narrate"}
    assert rep["reexport_buckets"] == ["narrate", "safety"]                                    # both > 60% zero-variance
    assert rep["by_rl_kind"]["narrate"]["n_prompts"] == 2 and "http_calls" in rep and rep["bos_check"]["skipped"]
    assert rep["moe_dir_set"] is True and not rep["errors"]
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
