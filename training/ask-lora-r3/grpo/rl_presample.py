#!/usr/bin/env python3
"""Mac-only pre-OK sampler for the r4 RL stage (RL-SCOPE C10c, runbook M6b). Pure standard library: no torch, no model
load, no subprocess. It never starts, stops or configures Wisp or an engine; it talks to an engine somebody else
started, on a free loopback port (never 8000, 8765 or 8775), and needs MOE_DIR set (the isolation marker) before it
will send a single request.

What it does: take the round-5 exported prompts (`grpo_prompts.jsonl`, optionally `grpo_text_dev.jsonl`), pick a
stratified subset (30 per bucket, 100 train safety rows, 50 dev safety rows by default), sample G = 6 completions per
row from the quantised r4 SFT at PRODUCTION sampling (T 1.0, top_p 0.95, top_k 64), score each with
`reward.score(text, label, rnd=5)` and write `pre-ok-report.json`: per bucket the zero-variance share (and, for the
PG-bearing buckets, its all-pass / all-fail split), the informative-group shares, the truncation share, the
completion-length mean / p50 / p99, the on-policy `ungrounded` review list, per-prompt scores (the S-learn / S-grader
null), `suggested_cap` (largest text-bucket p99 + 64) and the always-solved row ids for the re-export filter.

Route (default `completions`): POST /v1/completions with the row's exported rendered `prompt` string VERBATIM (forced
rows keep their trailing `<|channel>thought\\n`), no chat template and no tool parser, `skip_special_tokens: false` so
`<|tool_call>`, `<channel|>` and the stop tokens survive, `max_tokens = label.max_tokens` else 1,024, and either six
seeded calls per row (`--mode calls`, exact per-sample token counts from `usage`) or one `n=6` call (`--mode n`). The
text goes to `reward.score(..., rnd=5)`, which parses with `forced=label.forced`. Truncation is
`finish_reason == "length"`; a stop emitted at exactly max_tokens is not truncation. If the server strips the stop
string, the matching stop is appended on `finish_reason == "stop"`.

Fallback (`--route chat`, only for an oMLX build that cannot serve /v1/completions with a rendered prompt): POST
/v1/chat/completions with the row's raw `messages` and `tools` (the export must carry them) and rebuild the Gemma markup
from `reasoning_content`, `content` and `tool_calls`. Everything measured that way is labelled "reconstructed" and is NOT
used for `zv0_pass`, `zv0_fail` or `suggested_cap` (they are null). A forced rendered prompt cannot be sent through chat
completions, so a reconstructed forced row only approximates the production step.

    python rl_presample.py --base-url http://127.0.0.1:<free port> --served-model <r4 SFT id> \\
        --prompts $P/grpo_prompts.jsonl --text-dev $P/grpo_text_dev.jsonl --per-bucket 30 --safety-train 100 \\
        --safety-dev 50 --G 6 --round 5 --seed 0 --tokenizer-dir <HF dir of the SFT> --out $P/pre-ok-report.json

Run it with Wisp quit and the heavy lock held, as M6b says. MOE_DIR must be set to the isolated state directory.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reward as W  # noqa: E402

SCHEMA = "rl-presample/1"
FORBIDDEN_PORTS = (8000, 8765, 8775)         # the live engine, the live Wisp backend, the isolated test backend
LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")
NEVER_MODELS = ("ling", "embed", "rerank")   # standing rule: never load Ling or the embedder for tests
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}          # Wisp's production sampling for Ask
DEFAULT_MAX_TOKENS = 1024
STOPS = tuple(W.STOPS)                        # ("<turn|>", "<|tool_response>", "<eos>")
TURN_END, TOOL_RESPONSE = "<turn|>", "<|tool_response>"

# The §4.1 buckets are the export's `label.rl_kind` plus a "safety" carve-out by builder family (the safety and compose
# decisions bucket, which is built from several rl_kinds: decision finals, call steps, selection-like first steps).
SAFETY_FAMILIES = ("restraint_r4", "gather_then_send_r4", "safe_restraint", "compose_r3", "safe_effects",
                   "ignore_embedded_instructions", "no_write_for_reads", "reminders_r3")
ANCHOR_KINDS = ("chat_anchor", "session_anchor")        # constant reward + KL: zero advantage by design, never PG-bearing
CEILINGS = {"call": 1.5, "select": 1.5, "narrate": 2.0, "recall": 2.0, "recall_none": 1.5, "chat_anchor": 1.0,
            "session_anchor": 1.0, "decision": 1.0, "instruction": 1.0}   # §3.3 ceiling table; the export's label.ceiling wins
CLARIFY_CEILING = 1.5
UNSAFE_TERMS = ("forbidden", "early_write", "planted", "wrong_target", "payload_text", "nothing_claim")
FACTS_TERMS = ("facts", "ungrounded", "verbatim", "over_mention", "recall", "recall_absent", "recall_denial", "over_recall")
TOL = 1e-6
TRUNC_REEXPORT_SHARE = 0.01                  # RL-SCOPE §2.3: above 1% truncation the 1,024 p99 is censored
CAP_MARGIN = 64
MAXC_DEFAULT, MAXC_RAISED = 768, 1536
ZV_REEXPORT_SHARE = 0.60                     # §4.1: more than 60% zero-variance in a non-anchor bucket -> drop its always-solved rows


def die(msg: str, code: int = 2):
    print(f"rl_presample: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ------------------------------------------------------------------ isolation guards

def check_environment(environ=None) -> str:
    """Refuse to run unless MOE_DIR is set (the isolated-state marker, memory: safe live testing)."""
    environ = os.environ if environ is None else environ
    moe = (environ.get("MOE_DIR") or "").strip()
    if not moe:
        die("MOE_DIR is not set. Point it at the isolated state directory first (it must never be the live ~/.moe); "
            "nothing was sent.")
    return moe


def check_base_url(url: str) -> str:
    """A loopback http URL with an explicit port that is not one of the live ports."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "http":
        die(f"--base-url must be http://127.0.0.1:<port>, got {url!r}")
    host = (u.hostname or "").lower()
    if host not in LOOPBACK and host != "::1":
        die(f"--base-url must be a loopback address (the prompts are private), got host {host!r}")
    try:
        port = u.port
    except ValueError:
        port = None
    if not port:
        die("--base-url needs an explicit port")
    if port in FORBIDDEN_PORTS:
        die(f"port {port} is one of the live ports {FORBIDDEN_PORTS}; use an isolated engine on a free port")
    if u.path not in ("", "/") or u.query:
        die("--base-url must not carry a path or query")
    return f"http://{u.netloc}"


def check_model(name: str) -> str:
    words = re.split(r"[^a-z0-9]+", (name or "").lower())
    if not name or any(w.startswith(NEVER_MODELS) for w in words):
        die(f"refusing served model {name!r} (never Ling, the embedder or a reranker for tests)")
    return name


# ------------------------------------------------------------------ rows

def load_rows(path: str) -> list:
    rows = []
    with open(path) as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as exc:
                die(f"{path}:{ln}: not JSON ({exc})")
            if isinstance(r.get("label"), str):
                r["label"] = json.loads(r["label"])
            for k in ("id", "prompt", "label"):
                if k not in r:
                    die(f"{path}:{ln}: row has no {k!r}")
            rows.append(r)
    return rows


def is_text(row: dict) -> bool:
    lab = row["label"]
    return (row.get("modality") or lab.get("modality") or "text") == "text" and lab.get("rl_kind") != "mm"


def base_id(row_id: str) -> str:
    return row_id.rsplit("#", 1)[0] if "#" in row_id else row_id


def bucket_of(row: dict, safety_families=SAFETY_FAMILIES) -> str:
    fam = str(row.get("family") or "")
    if any(fam.startswith(s) for s in safety_families):
        return "safety"
    return str(row["label"].get("rl_kind") or "unlabelled")


def ceiling_of(label: dict):
    c = label.get("ceiling")
    if c is not None:
        return float(c)
    kind = label.get("rl_kind")
    if kind in CEILINGS:
        if label.get("ask") and kind not in ANCHOR_KINDS:
            return CLARIFY_CEILING
        return CEILINGS[kind]
    return None


def check_rounds(rows: list, rnd: int, what: str) -> None:
    bad = Counter(r["label"].get("round") for r in rows if r["label"].get("round") != rnd)
    if bad:
        die(f"{what}: {sum(bad.values())} rows are stamped round {dict(bad)}, not {rnd}. Export with --round {rnd} "
            "(an unstamped label is a round-3 label, and the round-5 terms would silently stay off).")


def stratify(rows: list, per_bucket: int, anchor_per_bucket: int, safety_n: int, seed: int,
             safety_families=SAFETY_FAMILIES) -> tuple:
    """Pick rows per bucket: `per_bucket` (anchors: `anchor_per_bucket`; safety: `safety_n`). One copy per base id, a
    seeded shuffle, deterministic. Returns (selected rows in bucket order, {bucket: {"available", "requested", "picked"}})."""
    pools = defaultdict(list)
    seen = set()
    for r in rows:
        if not is_text(r):
            continue
        key = base_id(r["id"])
        if key in seen:
            continue
        seen.add(key)
        pools[bucket_of(r, safety_families)].append(r)
    picked, info = [], {}
    for b in sorted(pools):
        want = safety_n if b == "safety" else anchor_per_bucket if b in ANCHOR_KINDS else per_bucket
        pool = sorted(pools[b], key=lambda r: r["id"])
        random.Random(f"{seed}-{b}").shuffle(pool)
        take = pool[:want]
        picked.extend(take)
        info[b] = {"available": len(pool), "requested": want, "picked": len(take)}
    return picked, info


# ------------------------------------------------------------------ HTTP

class Client:
    """JSON over HTTP to a loopback engine. Bypasses any proxy environment. `transport(path, body|None) -> dict` can
    replace the network for tests."""

    def __init__(self, base_url: str, timeout: float = 900.0, retries: int = 2, transport=None, omit=()):
        self.base = base_url.rstrip("/")
        self.timeout, self.retries, self.transport, self.omit = timeout, retries, transport, tuple(omit)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.calls = 0

    def _once(self, path: str, body: dict | None):
        if body is not None and self.omit:
            body = {k: v for k, v in body.items() if k not in self.omit}
        if self.transport is not None:
            return self.transport(path, body)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=self.timeout) as r:
            raw = r.read()
        return json.loads(raw) if raw else {}

    def request(self, path: str, body: dict | None = None):
        last = None
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                resp = self._once(path, body)
                if body is not None and not (resp or {}).get("choices"):
                    raise ValueError(f"no choices in the response ({str(resp)[:200]})")   # oMLX drops `choices` under contention
                return resp
            except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
                last = exc
                if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                    detail = ""
                    try:
                        detail = exc.read().decode()[:300]
                    except Exception:
                        pass
                    raise RuntimeError(f"{path}: HTTP {exc.code} {detail}") from exc
                time.sleep(min(2.0 * (attempt + 1), 10.0) if self.transport is None else 0)
        raise RuntimeError(f"{path}: {type(last).__name__}: {last}") from last


def served_models(client: Client) -> list:
    resp = client.request("/v1/models")
    return [m.get("id") for m in (resp or {}).get("data", [])]


# ------------------------------------------------------------------ completions route

def completions_body(model: str, prompt: str, max_tokens: int, *, n: int | None = None, seed: int | None = None,
                     sampling: dict | None = None, stop=STOPS, extra: dict | None = None) -> dict:
    """The request body. The prompt is the row's rendered string, untouched."""
    body = {"model": model, "prompt": prompt, "max_tokens": int(max_tokens), "stream": False,
            **(sampling or SAMPLING), "skip_special_tokens": False, "add_special_tokens": False}
    if stop:
        body["stop"] = list(stop)
        body["include_stop_str_in_output"] = True
    if n and n > 1:
        body["n"] = int(n)
    if seed is not None:
        body["seed"] = int(seed)
    body.update(extra or {})
    return body


def matching_stop(text: str, stop_reason=None) -> str:
    if isinstance(stop_reason, str) and stop_reason in STOPS:
        return stop_reason
    return TOOL_RESPONSE if text.rstrip().endswith(W.CALL_CLOSE) else TURN_END


def finalize_completion(text: str, finish_reason, stop_reason=None) -> tuple:
    """(text for the scorer, truncated). Truncation is finish_reason == "length"; a stop emitted at exactly max_tokens
    (the text still ends in one) is not truncation. When the server stripped the stop string, put it back."""
    if finish_reason is None:
        raise ValueError("the response has no finish_reason")
    text = text or ""
    ends_in_stop = text.rstrip().endswith(STOPS)
    if finish_reason == "length":
        return text, not ends_in_stop
    if not any(s in text for s in STOPS):
        text += matching_stop(text, stop_reason)
    return text, False


def sample_seed(base_seed: int, row_id: str, k: int) -> int:
    return zlib.crc32(f"{base_seed}:{row_id}:{k}".encode()) & 0x7FFFFFFF


def _choice_record(choice: dict, tokens, text: str, truncated: bool, route: str) -> dict:
    return {"text": text, "raw_finish": choice.get("finish_reason"), "truncated": truncated, "tokens": tokens, "route": route}


def sample_completions(client: Client, model: str, row: dict, G: int, mode: str, base_seed: int, max_tokens: int,
                       count_tokens=None, extra: dict | None = None) -> list:
    """G samples for one row through /v1/completions. Returns [{text, truncated, tokens, ...}]."""
    prompt = row["prompt"]
    out = []
    if mode == "n":
        resp = client.request("/v1/completions", completions_body(model, prompt, max_tokens, n=G,
                                                                 seed=sample_seed(base_seed, row["id"], 0), extra=extra))
        choices = sorted(resp["choices"], key=lambda c: c.get("index", 0))
        if len(choices) != G:
            raise ValueError(f"asked for n={G}, got {len(choices)} choices")
        usage = resp.get("usage") or {}
        for c in choices:
            text, trunc = finalize_completion(c.get("text"), c.get("finish_reason"), c.get("stop_reason") or c.get("matched_stop"))
            toks, exact = (count_tokens(text), True) if count_tokens else (None, False)
            rec = _choice_record(c, toks, text, trunc, "completions")
            rec["tokens_exact"] = exact
            rec["prompt_tokens"] = usage.get("prompt_tokens")
            out.append(rec)
        return out
    for k in range(G):
        resp = client.request("/v1/completions", completions_body(model, prompt, max_tokens, seed=sample_seed(base_seed, row["id"], k), extra=extra))
        c = resp["choices"][0]
        usage = resp.get("usage") or {}
        text, trunc = finalize_completion(c.get("text"), c.get("finish_reason"), c.get("stop_reason") or c.get("matched_stop"))
        toks = usage.get("completion_tokens")
        rec = _choice_record(c, toks, text, trunc, "completions")
        rec["tokens_exact"] = toks is not None
        rec["prompt_tokens"] = usage.get("prompt_tokens")
        out.append(rec)
    return out


# ------------------------------------------------------------------ chat fallback (reconstruction)

def _gemma_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_gemma_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{_gemma_value(x)}" for k, x in v.items()) + "}"
    return f"{W.QUOTE}{v}{W.QUOTE}"


def render_call(name: str, args: dict) -> str:
    return f"{W.CALL_OPEN}call:{name}{{" + ",".join(f"{k}:{_gemma_value(v)}" for k, v in (args or {}).items()) + f"}}{W.CALL_CLOSE}"


def split_thought(content, reasoning):
    """oMLX returns the thought as reasoning_content; fall back to Gemma's raw channel markers in the content."""
    content = content or ""
    if reasoning is None and W.CLOSE_THOUGHT in content:
        head, _, tail = content.partition(W.CLOSE_THOUGHT)
        reasoning = head.replace("<|channel>thought", "", 1).strip("\n")
        content = tail
    return content, (reasoning if isinstance(reasoning, str) else None)


def reconstruct(message: dict, forced: bool, with_stop: bool = True) -> str:
    """Rebuild the raw Gemma completion from a chat message: thought + <channel|> + content + tool calls + stop."""
    content, reasoning = split_thought(message.get("content"), message.get("reasoning_content", message.get("reasoning")))
    calls = []
    for tc in message.get("tool_calls") or ():
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": args}
        calls.append(render_call(fn.get("name", ""), args or {}))
    thought = (reasoning or "").strip()
    if forced:
        head = thought + "\n" + W.CLOSE_THOUGHT
    elif thought:
        head = W.OPEN_THOUGHT + thought + "\n" + W.CLOSE_THOUGHT
    else:
        head = ""
    stop = (TOOL_RESPONSE if calls else TURN_END) if with_stop else ""
    return head + (content or "").strip() + "".join(calls) + stop


def sample_chat(client: Client, model: str, row: dict, G: int, base_seed: int, max_tokens: int, extra: dict | None = None) -> list:
    messages, tools = row.get("messages"), row.get("tools")
    if not messages:
        raise ValueError(f"row {row['id']} carries no raw messages: the chat route needs an export that does")
    forced = bool(row["label"].get("forced"))
    out = []
    for k in range(G):
        body = {"model": model, "messages": messages, "stream": False, "max_tokens": int(max_tokens), **SAMPLING,
                "seed": sample_seed(base_seed, row["id"], k),
                "chat_template_kwargs": row.get("chat_template_kwargs") or {"enable_thinking": True}}
        if tools:
            body["tools"] = tools
        body.update(extra or {})
        resp = client.request("/v1/chat/completions", body)
        c = resp["choices"][0]
        fr = c.get("finish_reason")
        if fr is None:
            raise ValueError("the response has no finish_reason")
        trunc = fr == "length"
        text = reconstruct(c.get("message") or {}, forced, with_stop=not trunc)     # a cut reply never reached its stop
        usage = resp.get("usage") or {}
        rec = _choice_record(c, usage.get("completion_tokens"), text, trunc, "reconstructed")
        rec["tokens_exact"] = usage.get("completion_tokens") is not None
        rec["prompt_tokens"] = usage.get("prompt_tokens")
        out.append(rec)
    return out


# ------------------------------------------------------------------ BOS check

def tokenizer_counter(tokenizer_dir: str):
    """Token count of a rendered string as the trainer sees it (the HF tokenizer, add_special_tokens=False). Reads the
    tokenizer files only; loads no weights."""
    from transformers import AutoTokenizer          # lazy: the sampler itself needs nothing but the standard library
    tok = AutoTokenizer.from_pretrained(tokenizer_dir, local_files_only=True)
    return lambda text: len(tok(text, add_special_tokens=False)["input_ids"])


def counts_file_counter(path: str, rows: list):
    table = json.loads(Path(path).read_text())
    by_prompt = {r["prompt"]: table[r["id"]] for r in rows if r["id"] in table}
    return lambda text: by_prompt[text]


def bos_check(client: Client, model: str, rows: list, count_tokens, n: int = 12, seed: int = 0) -> dict:
    """The served tokenizer must not prepend a second BOS: for a sample of rows the served prompt_tokens must equal the
    HF count of the same string."""
    rng = random.Random(f"bos-{seed}")
    sample = rows if len(rows) <= n else rng.sample(rows, n)
    bad, extra, checked = [], 0, 0
    for r in sample:
        resp = client.request("/v1/completions", completions_body(model, r["prompt"], 1, sampling={"temperature": 0.0}, stop=None))
        served = (resp.get("usage") or {}).get("prompt_tokens")
        if served is None:
            return {"checked": checked, "ok": False, "reason": "the server returns no usage.prompt_tokens", "mismatches": []}
        want = count_tokens(r["prompt"])
        checked += 1
        if served != want:
            bad.append({"id": r["id"], "served": served, "hf": want})
            extra += served == want + 1
    return {"checked": checked, "ok": not bad, "extra_bos": bool(bad) and extra == len(bad), "mismatches": bad[:10]}


# ------------------------------------------------------------------ statistics

def nearest_rank(xs: list, q: float):
    if not xs:
        return None
    s = sorted(xs)
    return s[max(1, math.ceil(q * len(s))) - 1]


def length_stats(xs: list) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0, "mean": None, "p50": None, "p99": None, "max": None}
    return {"n": len(xs), "mean": round(sum(xs) / len(xs), 2), "p50": nearest_rank(xs, 0.5), "p99": nearest_rank(xs, 0.99), "max": max(xs)}


def score_sample(text: str, truncated: bool, label: dict, rnd: int) -> dict:
    res = W.score(text, label, rnd=rnd)
    parts = {k: round(v, 6) for k, v in res.parts.items() if abs(v) > 1e-12}
    return {"total": res.total, "parts": parts, "flags": list(res.flags), "truncated": truncated,
            "malformed": parts.get("malformed", 0.0) < 0}


def group_summary(samples: list, ceiling) -> dict:
    """Per-prompt facts the bucket statistics are built from. `samples` carry total/parts/truncated/malformed."""
    totals = [s["total"] for s in samples]
    zero_var = max(totals) - min(totals) <= TOL
    at_ceiling = [ceiling is not None and t >= ceiling - TOL for t in totals]
    kept = [s for s in samples if not s["truncated"]]
    kept_t = [s["total"] for s in kept]
    fired = [any(s["parts"].get(t, 0.0) < 0 for t in UNSAFE_TERMS) for s in samples]
    ok = [s for s in samples if not s["malformed"]]
    fvals = [round(sum(s["parts"].get(t, 0.0) for t in FACTS_TERMS), 6) for s in ok]
    return {
        "zero_variance": zero_var,
        "all_pass": zero_var and all(at_ceiling),
        "all_fail": zero_var and not all(at_ceiling),
        "zero_adv": len(kept) < 2 or max(kept_t) - min(kept_t) <= TOL,     # what grpo.py's mask leaves for this group
        "informative_unsafe": 0 < sum(fired) < len(samples),
        "informative_facts": len(fvals) >= 2 and max(fvals) - min(fvals) > TOL,
        "mean": round(sum(totals) / len(totals), 4),
        "unsafe_fired": sum(fired),
    }


def share(flags: list):
    return round(sum(flags) / len(flags), 4) if flags else None


def bucket_stats(groups: list, review_n: int = 40) -> dict:
    """groups: [{id, family, rl_kind, ceiling, route, samples:[...]}], all one bucket."""
    if not groups:
        return {"n_prompts": 0}
    for g in groups:
        g["summary"] = group_summary(g["samples"], g["ceiling"])
    sm = [g["summary"] for g in groups]
    samples = [s for g in groups for s in g["samples"]]
    pg = not all(g["rl_kind"] in ANCHOR_KINDS for g in groups)
    reconstructed = any(g["route"] == "reconstructed" for g in groups)
    toks = [s["tokens"] for s in samples]
    estimated = any(not s.get("tokens_exact") for s in samples)
    flagged = [(g, s) for g in groups for s in g["samples"] if s["parts"].get("ungrounded", 0.0) < 0]
    parsed = [s for s in samples if not s["malformed"]]
    out = {
        "n_prompts": len(groups), "n_samples": len(samples), "pg_bearing": pg,
        "route": "reconstructed" if reconstructed else "completions",
        "zero_variance_share": share([x["zero_variance"] for x in sm]),
        "zero_advantage_share": share([x["zero_adv"] for x in sm]),
        "zv_all_pass_share": share([x["all_pass"] for x in sm]) if pg else None,
        "zv_all_fail_share": share([x["all_fail"] for x in sm]) if pg else None,
        "informative_unsafe_share": share([x["informative_unsafe"] for x in sm]),
        "informative_facts_share": share([x["informative_facts"] for x in sm]),
        "truncation_share": share([s["truncated"] for s in samples]),
        "truncated_groups_share": share([any(s["truncated"] for s in g["samples"]) for g in groups]),
        "malformed_share": share([s["malformed"] for s in samples]),
        "completion_tokens": length_stats(toks),
        "tokens_exact": not estimated,
        "ceiling_missing": sum(g["ceiling"] is None for g in groups),
        "mean_score": round(sum(s["total"] for s in samples) / len(samples), 4),
        "ceiling_mean": round(sum(g["ceiling"] for g in groups if g["ceiling"] is not None) / max(1, sum(g["ceiling"] is not None for g in groups)), 4),
        "max_tokens": sorted({g.get("max_tokens") for g in groups if g.get("max_tokens")}),
        "own_max_tokens": any(g.get("label_max_tokens") for g in groups),
        "ungrounded": {
            "flagged_samples": len(flagged), "scored_samples": len(parsed),
            "flagged_share": round(len(flagged) / len(parsed), 4) if parsed else None,
            "hand_check": ("false-positive rate cannot be read off a score: hand- or reference-check at least 30 of these; "
                           "drop the sub-rule if more than 2% of grounded samples are penalised"),
            "review": [{"id": g["id"], "ungrounded": s["flags"], "text": s["text"][:600]} for g, s in flagged[:review_n]],
        },
        "per_prompt": [{"id": g["id"], "family": g["family"], "rl_kind": g["rl_kind"], "ceiling": g["ceiling"],
                        "scores": [s["total"] for s in g["samples"]], "mean": g["summary"]["mean"],
                        "truncated": [s["truncated"] for s in g["samples"]], "zero_variance": g["summary"]["zero_variance"],
                        "all_pass": g["summary"]["all_pass"]} for g in groups],
    }
    out["over_60_zero_variance"] = bool(pg and out["zero_variance_share"] is not None and out["zero_variance_share"] > ZV_REEXPORT_SHARE)
    out["truncation_over_1pct"] = bool(out["truncation_share"] and out["truncation_share"] > TRUNC_REEXPORT_SHARE)
    return out


def mix_weighted(buckets: dict, mix: dict, key: str):
    """Export-mix-weighted share over the PG-bearing buckets that were sampled. `mix` = {bucket: rows in the full export}."""
    num = den = 0.0
    for b, st in buckets.items():
        if not st.get("pg_bearing") or st.get(key) is None or not mix.get(b):
            continue
        num += mix[b] * st[key]
        den += mix[b]
    return round(num / den, 4) if den else None


def cap_report(buckets: dict, reconstructed: bool) -> dict:
    """suggested_cap = the largest p99 + 64 over the text buckets the `--max-completion` flag governs (those whose rows
    carry no label.max_tokens); chat and session rows carry their own cap, so a truncation problem there is fixed by
    re-exporting label.max_tokens, not by the flag (RL-SCOPE §2.3, C5)."""
    governed = {b: st for b, st in buckets.items() if st.get("n_prompts") and not st["own_max_tokens"]}
    own = {b: st for b, st in buckets.items() if st.get("n_prompts") and st["own_max_tokens"]}
    exact = all(st["tokens_exact"] for st in governed.values()) and all(st["completion_tokens"]["p99"] is not None for st in governed.values())
    p99s = {b: st["completion_tokens"]["p99"] for b, st in governed.items() if st["completion_tokens"]["p99"] is not None}
    out = {"suggested_cap": None, "suggested_cap_bucket": None, "maxc_recommendation": None, "censored_buckets": [],
           "reexport_label_max_tokens": [], "note": ""}
    out["censored_buckets"] = sorted(b for b, st in governed.items() if st["truncation_over_1pct"])
    out["reexport_label_max_tokens"] = sorted(b for b, st in own.items() if st["truncation_over_1pct"])
    if reconstructed:
        out["note"] = "reconstructed route: lengths are not production lengths, so no suggested_cap"
        return out
    if not p99s or not exact:
        out["note"] = "no exact per-sample token counts (use --mode calls, or --tokenizer-dir with --mode n): no suggested_cap"
        return out
    top = max(p99s, key=lambda b: p99s[b])
    out["suggested_cap"] = int(p99s[top] + CAP_MARGIN)
    out["suggested_cap_bucket"] = top
    if out["censored_buckets"]:
        out["maxc_recommendation"] = MAXC_RAISED
        out["note"] = (f"truncation above 1% in {out['censored_buckets']}: the p99 is censored at the sampling cap; "
                       f"set MAXC to {MAXC_RAISED} and re-cost")
    else:
        out["maxc_recommendation"] = max(MAXC_DEFAULT, out["suggested_cap"])
    if out["reexport_label_max_tokens"]:
        out["note"] += (" " if out["note"] else "") + f"re-export label.max_tokens at {MAXC_RAISED} for {out['reexport_label_max_tokens']}"
    return out


# ------------------------------------------------------------------ the run

def fingerprint(args, route: str) -> dict:
    return {"model": args.served_model, "route": route, "mode": args.mode, "seed": args.seed, "G": args.G,
            "max_tokens_override": args.max_tokens, "default_max_tokens": args.default_max_tokens, "sampling": SAMPLING}


def row_key(row: dict, mt: int) -> str:
    return f"{zlib.crc32(row['prompt'].encode()):08x}:{mt}"


def load_cache(path: str, fp: dict) -> dict:
    cache = {}
    if not path or not os.path.exists(path):
        return cache
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue                        # a line cut off by a kill
            if d.get("fp") == fp:
                cache[(d["id"], d.get("rk"))] = d["samples"]
    return cache


def row_max_tokens(row: dict, args) -> int:
    return int(args.max_tokens or row["label"].get("max_tokens") or args.default_max_tokens)


def run_sampling(client: Client, args, picked: list, route: str, count_tokens, log=lambda m: None) -> tuple:
    fp = fingerprint(args, route)
    cache = {} if args.fresh else load_cache(args.samples_out, fp)
    sink = open(args.samples_out, "a") if args.samples_out else None
    raw, errors = {}, {}
    t0 = time.monotonic()
    done_toks = 0
    try:
        for i, row in enumerate(picked, 1):
            rid = row["id"]
            mt = row_max_tokens(row, args)
            rk = row_key(row, mt)
            if len(cache.get((rid, rk), ())) == args.G:
                raw[rid] = cache[(rid, rk)]
                continue
            try:
                if route == "completions":
                    recs = sample_completions(client, args.served_model, row, args.G, args.mode, args.seed, mt, count_tokens, args.extra_body)
                else:
                    recs = sample_chat(client, args.served_model, row, args.G, args.seed, mt, args.extra_body)
            except (RuntimeError, ValueError) as exc:
                errors[rid] = str(exc)[:300]
                log(f"  ! {rid}: {errors[rid]}")
                continue
            for r in recs:
                r["max_tokens"] = mt
            raw[rid] = recs
            if sink:
                sink.write(json.dumps({"fp": fp, "id": rid, "rk": rk, "samples": recs}, ensure_ascii=False) + "\n")
                sink.flush()
            done_toks += sum(r["tokens"] or 0 for r in recs)
            if i % 5 == 0 or i == len(picked):
                el = time.monotonic() - t0
                log(f"  {i}/{len(picked)} rows, {el:.0f}s, {done_toks / max(el, 1e-9):.1f} tok/s")
    finally:
        if sink:
            sink.close()
    return raw, errors, round(time.monotonic() - t0, 1)


def build_groups(picked: list, raw: dict, rnd: int, safety_families) -> dict:
    """{bucket: [group]} from the sampled rows that have all their samples."""
    buckets = defaultdict(list)
    for row in picked:
        recs = raw.get(row["id"])
        if not recs:
            continue
        lab = row["label"]
        samples = []
        for r in recs:
            s = score_sample(r["text"], r["truncated"], lab, rnd)
            s.update(text=r["text"], tokens=r["tokens"], tokens_exact=r.get("tokens_exact", False),
                     finish_reason=r.get("raw_finish"))
            samples.append(s)
        buckets[bucket_of(row, safety_families)].append({
            "id": row["id"], "family": row.get("family"), "rl_kind": lab.get("rl_kind"), "ceiling": ceiling_of(lab),
            "route": recs[0]["route"], "max_tokens": recs[0]["max_tokens"], "label_max_tokens": lab.get("max_tokens"),
            "samples": samples})
    return buckets


def build_report(args, route: str, train_groups: dict, dev_groups: dict, mix: dict, selection: dict, extras: dict) -> dict:
    reconstructed = route != "completions"
    buckets = {b: bucket_stats(g, args.review_n) for b, g in sorted(train_groups.items())}
    dev = {f"dev:{b}": bucket_stats(g, args.review_n) for b, g in sorted(dev_groups.items())}
    by_kind = defaultdict(list)
    for g in (g for gs in train_groups.values() for g in gs):
        by_kind[g["rl_kind"] or "unlabelled"].append(g)
    kinds = {}
    for k, gs in sorted(by_kind.items()):
        st = bucket_stats(gs, 0)
        kinds[k] = {key: st[key] for key in ("n_prompts", "zero_variance_share", "truncation_share", "completion_tokens", "mean_score")}
    caps = cap_report(buckets, reconstructed)
    zv_pass = None if reconstructed else mix_weighted(buckets, mix, "zv_all_pass_share")
    zv_fail = None if reconstructed else mix_weighted(buckets, mix, "zv_all_fail_share")
    always = {b: [p["id"] for p in st["per_prompt"] if p["all_pass"]] for b, st in buckets.items() if st.get("pg_bearing")}
    reexport = sorted(b for b, st in buckets.items() if st.get("over_60_zero_variance"))
    return {
        "schema": SCHEMA, "round": args.round, "G": args.G, "served_model": args.served_model,
        "route": "completions" if not reconstructed else "reconstructed",
        "reconstructed": reconstructed, "mode": args.mode, "seed": args.seed, "sampling": SAMPLING,
        "default_max_tokens": args.default_max_tokens, "max_tokens_override": args.max_tokens,
        "selection": selection, **extras,
        "zv0_pass": zv_pass, "zv0_fail": zv_fail,
        "zv0_basis": ("export-mix-weighted over the sampled PG-bearing text buckets (chat and session anchors and "
                      "multimodal rows excluded); null on the reconstructed route"),
        "export_mix": mix,
        "suggested_cap": caps["suggested_cap"], "cap": caps,
        "buckets": buckets, "dev": dev, "by_rl_kind": kinds,
        "reexport_buckets": reexport,
        "always_solved_by_bucket": always,
        "always_solved_row_ids": sorted({i for ids in always.values() for i in ids}),
    }


def export_mix(rows: list, safety_families) -> dict:
    c = Counter(bucket_of(r, safety_families) for r in rows if is_text(r) and r["label"].get("rl_kind") not in ANCHOR_KINDS)
    return dict(c)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", help="the isolated engine, http://127.0.0.1:<free port> (never 8000, 8765, 8775)")
    ap.add_argument("--port", type=int, help="shorthand for --base-url http://127.0.0.1:PORT")
    ap.add_argument("--served-model", required=True, help="the id the engine serves the quantised r4 SFT under")
    ap.add_argument("--prompts", required=True, help="round-5 grpo_prompts.jsonl")
    ap.add_argument("--text-dev", help="round-5 grpo_text_dev.jsonl (for the dev safety slice)")
    ap.add_argument("--per-bucket", type=int, default=30)
    ap.add_argument("--anchor-per-bucket", type=int, default=None, help="default: --per-bucket")
    ap.add_argument("--safety-train", type=int, default=100)
    ap.add_argument("--safety-dev", type=int, default=50)
    ap.add_argument("--safety-families", default=",".join(SAFETY_FAMILIES), help="family name prefixes that form the safety bucket")
    ap.add_argument("--G", type=int, default=6)
    ap.add_argument("--round", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--mode", choices=("calls", "n"), default="calls", help="six seeded calls (exact token counts) or one n=G call")
    ap.add_argument("--route", choices=("completions", "chat"), default="completions",
                    help="chat = the reconstruction fallback (labelled 'reconstructed'; no zv0 or suggested_cap)")
    ap.add_argument("--max-tokens", type=int, default=None, help="override every row's cap (default: label.max_tokens)")
    ap.add_argument("--default-max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--tokenizer-dir", help="HF tokenizer directory of the SFT (local files only) for the BOS check and --mode n lengths")
    ap.add_argument("--token-counts", help="JSON {row id: HF token count of its prompt}, made on the box that has the tokenizer")
    ap.add_argument("--skip-bos-check", action="store_true", help="record the BOS check as skipped (the report says so)")
    ap.add_argument("--bos-sample", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--omit-fields", default="", help="comma list of request fields this engine rejects (e.g. add_special_tokens)")
    ap.add_argument("--extra-body", type=json.loads, default=None, help="JSON merged into every request body")
    ap.add_argument("--samples-out", default=None, help="per-row sample cache (JSONL, resumable); default <out>.samples.jsonl")
    ap.add_argument("--fresh", action="store_true", help="ignore the sample cache")
    ap.add_argument("--review-n", type=int, default=40, help="flagged ungrounded samples to list per bucket")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if bool(a.base_url) == bool(a.port):
        ap.error("give exactly one of --base-url and --port")
    if a.base_url is None:
        a.base_url = f"http://127.0.0.1:{a.port}"
    if a.anchor_per_bucket is None:
        a.anchor_per_bucket = a.per_bucket
    if a.samples_out is None:
        a.samples_out = a.out + ".samples.jsonl"
    if a.G < 2:
        ap.error("--G must be at least 2")
    return a


def main(argv=None, *, transport=None, environ=None) -> int:
    a = parse_args(argv)
    check_environment(environ)
    base = check_base_url(a.base_url)
    check_model(a.served_model)
    families = tuple(x for x in a.safety_families.split(",") if x)
    log = lambda m: print(m, file=sys.stderr, flush=True)

    rows = load_rows(a.prompts)
    text_rows = [r for r in rows if is_text(r)]
    check_rounds(text_rows, a.round, a.prompts)
    dev_rows = []
    if a.text_dev:
        dev_rows = [r for r in load_rows(a.text_dev) if is_text(r)]
        check_rounds(dev_rows, a.round, a.text_dev)
    picked, sel = stratify(text_rows, a.per_bucket, a.anchor_per_bucket, a.safety_train, a.seed, families)
    dev_pick = []
    if dev_rows:
        pool = sorted((r for r in dev_rows if bucket_of(r, families) == "safety"), key=lambda r: r["id"])
        random.Random(f"{a.seed}-dev-safety").shuffle(pool)
        dev_pick = pool[:a.safety_dev]
        sel["dev:safety"] = {"available": len(pool), "requested": a.safety_dev, "picked": len(dev_pick)}
    shortfalls = {b: s for b, s in sel.items() if s["picked"] < s["requested"]}
    for b, s in shortfalls.items():
        log(f"  short: {b} has {s['available']} rows, wanted {s['requested']}")
    all_picked = picked + dev_pick
    if not all_picked:
        die("no text rows to sample")
    mm_skipped = sum(not is_text(r) for r in rows)

    client = Client(base, a.timeout, a.retries, transport, [x for x in a.omit_fields.split(',') if x])
    avail = served_models(client)
    if a.served_model not in avail:
        die(f"the engine at {base} does not list {a.served_model!r} (lists {avail[:8]}); start the isolated engine with it first")

    count_tokens = None
    if a.tokenizer_dir:
        count_tokens = tokenizer_counter(a.tokenizer_dir)
    elif a.token_counts:
        count_tokens = counts_file_counter(a.token_counts, all_picked)
    route = "completions" if a.route == "completions" else "reconstructed"
    bos = {"ok": None, "skipped": True, "reason": "chat route: the rendered prompt is not sent"}
    if a.route == "completions":
        if a.skip_bos_check:
            bos = {"ok": None, "skipped": True, "reason": "--skip-bos-check"}
        elif count_tokens is None:
            die("the BOS check needs --tokenizer-dir or --token-counts (served prompt_tokens must equal the HF count); "
                "or pass --skip-bos-check and the report will say it was skipped")
        else:
            bos = bos_check(client, a.served_model, all_picked, count_tokens, a.bos_sample, a.seed)
            log(f"BOS check: {bos}")
            if not bos["ok"]:
                die("the served tokenizer disagrees with the HF tokenizer on the prompt token count"
                    + (" (one extra token: a second BOS is being prepended; fix the server or the prompt)" if bos.get("extra_bos") else "")
                    + f": {bos.get('mismatches') or bos.get('reason')}", 4)
    if a.mode == "n" and count_tokens is None and a.route == "completions":
        log("  note: --mode n without a tokenizer gives no exact per-sample lengths (no suggested_cap)")

    raw, errors, secs = run_sampling(client, a, all_picked, route, count_tokens, log)
    train_ids = {r["id"] for r in picked}
    groups = build_groups(all_picked, raw, a.round, families)
    train_groups, dev_groups = defaultdict(list), defaultdict(list)
    for b, gs in groups.items():
        for g in gs:
            (train_groups if g["id"] in train_ids else dev_groups)[b].append(g)
    extras = {"bos_check": bos, "http_calls": client.calls, "seconds": secs, "errors": errors,
              "incomplete_rows": sorted(errors), "shortfalls": shortfalls, "multimodal_rows_skipped": mm_skipped,
              "moe_dir_set": True,
              "caveats": ["time unverified", "quantised and bf16 lengths may differ slightly",
                          "ungrounded false positives need hand review of the listed samples"]
              + (["RECONSTRUCTED via /v1/chat/completions: not production markup; zv0 and suggested_cap withheld"] if route != "completions" else [])}
    report = build_report(a, route, train_groups, dev_groups, export_mix(text_rows, families), sel, extras)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(out)
    summary = {b: (st["n_prompts"], st.get("zero_variance_share"), st.get("truncation_share"), (st.get("completion_tokens") or {}).get("p99"))
               for b, st in {**report["buckets"], **report["dev"]}.items()}
    log(f"wrote {out}: suggested_cap {report['suggested_cap']}, zv0_pass {report['zv0_pass']}, zv0_fail {report['zv0_fail']}")
    log(f"(n_prompts, zero_var, trunc, p99) per bucket: {summary}")
    if errors:
        log(f"{len(errors)} rows could not be sampled; re-run to resume from {a.samples_out}")
        return 3
    return 0


def _imports(path: Path) -> set:
    tree = ast.parse(path.read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(x.name.split(".")[0] for x in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    return mods


if __name__ == "__main__":
    sys.exit(main())
