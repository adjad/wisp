#!/usr/bin/env python3
"""Sample the base model's own final answers for r5 on a GPU (one process per GPU).

Reads a builder ``partial.jsonl`` ({meta, messages, tools}; the last message is the target), keeps every row whose
target is a final answer (an assistant message with no tool calls), renders the prompt with r3's ``render.py`` (the
prompt oMLX serves, ending ``<|channel>thought\\n`` after a tool result) and samples the unquantized base with
production's settings: temperature 1.0, top_p 0.95, top_k 64, at most 3000 new tokens, stopping on ``<turn|>`` (106),
``<|tool_response>`` (50) and EOS (1). The row's own ``chat_template_kwargs`` are used, so thinking stays on where
the row has it on.

Output: one JSON line per (id, sample) in gen.py's format and with gen.py's ``request_hash``, so r4's choose.py and
splice.py (and choose_r5.py) work unchanged. A sample that calls a tool instead of answering records its
``tool_calls`` and so fails choose. The text is decoded with skip_special_tokens=False and split into ``reasoning``
and ``content`` (see ``parse_output``).

Order is sample-major: sample 0 for every prompt of the shard, then sample 1, and so on. Every process writes its own
file ``<out-dir>/finals.s<I>of<N>.jsonl`` and a rerun skips every (id, sample) found in ANY ``finals.*.jsonl`` of
the out dir (so it also resumes under another shard count).

    gen_finals.py --partial partial.jsonl --out-dir raw --base <model dir> --gpu 3 --shard 3/8 --k 2
    gen_finals.py ... --ids need.ids --k 6          # top-up: samples 2..5 for the ids in need.ids only

``--targets calls`` samples base at the tool-call steps instead (traces.*.jsonl). ``choose_traces.py`` keeps base's own
thought only where base's own call matches the scripted one. Each run writes ``generation.<targets>.json``: model files,
template, code hashes and settings, which are the provenance of every sample in that directory.

Batching is off (``--batch 1``): one sequence at a time per GPU. ``--batch N`` pads on the left, sorts a window of
prompts by length and runs N together; run ``--selfcheck`` first to confirm batched greedy decoding equals one-at-a-time
decoding on the box before using it. Run through run_finals.sh.
"""
from __future__ import annotations

import _paths  # noqa: F401  (sys.path for gen.py / render.py)

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from gen import request_hash  # noqa: E402  (the same hash gen.py writes)

MODEL = "gemma-4-E4B-it"
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}
MAX_NEW = 3000
STOP_IDS = (106, 50, 1)          # <turn|>, <|tool_response>, <eos>
OPEN, CLOSE = "<|channel>thought\n", "<channel|>"
FORCED = OPEN                    # render.FORCED: the generation prompt after a tool result already opened the thought
CUT = ("<turn|>", "<|tool_response>")
SPECIAL_TAIL = ("<eos>", "<pad>", "<bos>")
TOOL_CALL = re.compile(r"<\|tool_call>(.*?)(?:<tool_call\|>|$)", re.S)
MAX_CONSECUTIVE_ERRORS = 5


# ------------------------------------------------------------------ rows
def is_final(row: dict) -> bool:
    """The target is a plain assistant answer: no tool calls, text content."""
    last = (row.get("messages") or [{}])[-1]
    return (last.get("role") == "assistant" and not last.get("tool_calls")
            and isinstance(last.get("content"), str))


def has_media(row: dict) -> bool:
    """A message with image or audio parts: text-only generation cannot answer it."""
    for m in row["messages"][:-1]:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") not in (None, "text") for p in c):
            return True
    return False


def is_call(row: dict) -> bool:
    last = (row.get("messages") or [{}])[-1]
    return last.get("role") == "assistant" and bool(last.get("tool_calls"))


def select_rows(rows: list[dict], targets: str = "finals") -> tuple[list[dict], list[str]]:
    """(rows of the target kind in partial order, ids skipped for media)."""
    keep = is_call if targets == "calls" else is_final
    out, skipped = [], []
    for r in rows:
        if not keep(r):
            continue
        if has_media(r):
            skipped.append(r["meta"]["id"])
            continue
        out.append(r)
    return out, skipped


def shard_rows(rows: list[dict], shard: int, nshards: int) -> list[dict]:
    """Every nshards-th row: disjoint, covers all, and scenario neighbours (similar length) go to different GPUs."""
    if not 0 <= shard < nshards:
        raise ValueError(f"shard {shard} of {nshards}")
    return rows[shard::nshards]


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue          # a line cut off by a kill


def done_pairs(out_dir: str, prefix: str = "finals") -> set[tuple[str, int]]:
    done = set()
    for p in sorted(glob.glob(os.path.join(out_dir, f"{prefix}.*.jsonl"))):
        for d in read_jsonl(p):
            if not d.get("error") and "id" in d and "k" in d:
                done.add((d["id"], d["k"]))
    return done


def todo_list(rows: list[dict], k: int, done: set) -> list[tuple[int, dict]]:
    """Sample-major: sample 0 of every prompt, then sample 1, ..."""
    return [(kk, r) for kk in range(k) for r in rows if (r["meta"]["id"], kk) not in done]


# ------------------------------------------------------------------ prompt and output
def prompt_for(tok, row: dict) -> str:
    """The served prompt, from render.py (the target is irrelevant to it, so the row renders as NO THINK)."""
    import render
    meta = {**row["meta"], "think": False, "thought": None}
    return render.render(tok, {"messages": row["messages"], "tools": row.get("tools")}, meta)["prompt"]


def parse_output(text: str, prompt_forced: bool) -> dict:
    """Split decoded text (special tokens kept) into reasoning / content / tool calls.

    - The generation prompt after a tool result already ends ``<|channel>thought\\n``: the output is the thought, then
      ``<channel|>``, then the answer (no opening tag).
    - A first step ends ``<|turn>model\\n`` and the model opens the channel itself: ``<|channel>thought\\n...<channel|>``.
      An empty thought is ``<|channel>thought\\n<channel|>``.
    - No channel at all: reasoning None, everything is content.
    - Opened and never closed (cut off): everything is reasoning, content empty (choose rejects it as empty).
    Stop markers and padding are removed; text from the first ``<|tool_call>`` on becomes ``tool_calls``. A channel
    marker left in the content is flagged ``channel_leak``.
    """
    for c in CUT:
        text = text.split(c, 1)[0]
    for s in SPECIAL_TAIL:
        text = text.replace(s, "")
    body = text.lstrip()
    opened = body.startswith(OPEN)
    if opened:
        text = body[len(OPEN):]
    reasoning = None
    if opened or prompt_forced or CLOSE in text:
        head, sep, tail = text.partition(CLOSE)
        if sep:
            reasoning, content = head, tail
        elif opened or prompt_forced:
            reasoning, content = text, ""
        else:
            content = text
        if reasoning is not None:
            reasoning = reasoning.replace(OPEN, "", 1).strip()
    else:
        content = text
    calls = []
    for m in TOOL_CALL.finditer(content):
        raw = m.group(1).strip()
        name = re.match(r"call:\s*([\w.\-]+)", raw)
        calls.append({"type": "function", "function": {"name": name.group(1) if name else "",
                                                       "arguments": raw[name.end():] if name else raw}})
    if calls:
        content = content.split("<|tool_call>", 1)[0]
    leak = "<|channel>" in content or CLOSE in content
    out = {"content": content.strip(), "reasoning": reasoning if reasoning else None, "tool_calls": calls}
    if leak:
        out["channel_leak"] = True
    return out


def make_record(row: dict, k: int, gen_ids: list[int], tok, prompt_len: int, prompt_forced: bool,
                seconds: float, batch: int, attn: str) -> dict:
    """One output line from the generated ids (ids after the first stop token are padding and dropped)."""
    ids = list(gen_ids)
    stopped = False
    for i, t in enumerate(ids):
        if t in STOP_IDS:
            ids, stopped = ids[:i + 1], True
            break
    m = row["meta"]
    rec = {"id": m["id"], "family": m["family"], "k": k, "model": MODEL,
           "request_hash": request_hash(row["messages"][:-1], row.get("tools"))}
    rec |= parse_output(tok.decode(ids, skip_special_tokens=False), prompt_forced)
    rec |= {"finish_reason": "stop" if stopped else "length",
            "usage": {"prompt_tokens": prompt_len, "completion_tokens": len(ids),
                      "total_tokens": prompt_len + len(ids)},
            "seconds": round(seconds, 2), "no_choices": False, "source": "gpu", "batch": batch, "attn": attn}
    return rec


def windows(todo: list, batch: int):
    """Batches of the todo list. batch 1: the list as is. Otherwise a window of 4*batch consecutive items of one
    sample index is sorted by prompt length and cut into batches, so padding stays small and the order stays
    nearly sample-major. Items are (k, row, prompt_ids)."""
    if batch <= 1:
        for it in todo:
            yield [it]
        return
    i = 0
    while i < len(todo):
        k0, win = todo[i][0], []
        while i < len(todo) and todo[i][0] == k0 and len(win) < batch * 4:
            win.append(todo[i])
            i += 1
        win.sort(key=lambda it: len(it[2]))
        for j in range(0, len(win), batch):
            yield win[j:j + batch]


def seed_for(items: list) -> int:
    key = ",".join(f"{it[1]['meta']['id']}:{it[0]}" for it in items)
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


# ------------------------------------------------------------------ driver (generate_fn is injectable for tests)
def sha256_file(path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def provenance(base: str, targets: str, attn: str, batch: int, k: int) -> dict:
    """What every sample of this run came from: the model's files, the template, the code and the settings."""
    import render
    b = Path(base)
    out = {"targets": targets, "model": MODEL, "base_dir": b.name, "sampling": SAMPLING, "max_new_tokens": MAX_NEW,
           "stop_ids": list(STOP_IDS), "attn": attn, "batch": batch, "k": k,
           "config_sha256": sha256_file(b / "config.json"), "weights_index_sha256": sha256_file(b / "model.safetensors.index.json"),
           "template_sha256": sha256_file(b / "chat_template.jinja"), "tokenizer_sha256": sha256_file(b / "tokenizer.json"),
           "gen_finals_sha256": sha256_file(__file__), "render_sha256": sha256_file(render.__file__),
           "serving": "installed oMLX text path: prior in-turn reasoning dropped"}
    return out


def run(rows: list[dict], tok, generate_fn, out_dir: str, shard: int, nshards: int, k: int,
        batch: int = 1, attn: str = "math", log=print, prefix: str = "finals") -> dict:
    """generate_fn(list of prompt id lists, seed) -> list of generated id lists (may carry trailing padding)."""
    os.makedirs(out_dir, exist_ok=True)
    mine = shard_rows(rows, shard, nshards)
    done = done_pairs(out_dir, prefix)
    todo = todo_list(mine, k, done)
    log(f"shard {shard}/{nshards}: {len(mine)} prompts x {k} = {len(mine) * k}; "
        f"{sum(1 for r in mine for kk in range(k) if (r['meta']['id'], kk) in done)} done, {len(todo)} to go")
    # render lazily enough: prompts for the whole todo are tokenized once, up front (cheap next to generation)
    items = []
    for kk, r in todo:
        p = prompt_for(tok, r)
        items.append((kk, r, tok(p, add_special_tokens=False).input_ids, p.endswith(FORCED)))
    path = os.path.join(out_dir, f"{prefix}.s{shard}of{nshards}.jsonl")
    t0, n, toks, errors = time.monotonic(), 0, 0, 0
    with open(path, "a", encoding="utf-8") as f:
        for grp in windows(items, batch):
            ts = time.monotonic()
            try:
                outs = generate_fn([g[2] for g in grp], seed_for(grp))
                if len(outs) != len(grp):
                    raise RuntimeError(f"generate returned {len(outs)} sequences for {len(grp)} prompts")
                errors = 0
            except Exception as exc:  # noqa: BLE001  (an OOM on one long prompt must not end the shard)
                errors += 1
                for g in grp:
                    f.write(json.dumps({"id": g[1]["meta"]["id"], "family": g[1]["meta"]["family"], "k": g[0],
                                        "error": repr(exc)[:300]}) + "\n")
                f.flush()
                log(f"error {errors}: {exc!r}"[:300])
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    raise SystemExit(f"stopping after {errors} errors in a row")
                continue
            dt = time.monotonic() - ts
            for g, ids in zip(grp, outs):
                rec = make_record(g[1], g[0], ids, tok, len(g[2]), g[3], dt, len(grp), attn)
                toks += rec["usage"]["completion_tokens"]
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
            f.flush()
            if n and (n % 10 < len(grp) or n == len(items)):
                el = time.monotonic() - t0
                log(f"shard {shard}: {n}/{len(items)}  {el / 60:.1f} min  {toks / el:.1f} tok/s  "
                    f"eta {el / n * (len(items) - n) / 60:.0f} min  last {rec['usage']['completion_tokens']} tok "
                    f"finish={rec['finish_reason']}")
    el = max(time.monotonic() - t0, 1e-9)
    log(f"shard {shard}: finished {n} samples, {toks} tokens, {toks / el:.1f} tok/s")
    return {"samples": n, "tokens": toks, "seconds": el}


# ------------------------------------------------------------------ the real model (torch loaded only here)
def restrict_sdpa_to_math(torch):
    """train.py's restrict_sdpa_to_math: only torch's math SDPA backend stays on (gfx950's fused kernel is wrong for
    Gemma 4's scale 1.0 / head_dim 512)."""
    cuda = torch.backends.cuda
    for name in ("enable_flash_sdp", "enable_mem_efficient_sdp", "enable_cudnn_sdp"):
        if hasattr(cuda, name):
            getattr(cuda, name)(False)
    if hasattr(cuda, "enable_math_sdp"):
        cuda.enable_math_sdp(True)


def attn_implementation(attn: str) -> dict:
    kind = "eager" if attn == "eager" else "sdpa"      # train.py: audio tower is always eager
    return {"": kind, "text_config": kind, "vision_config": kind, "audio_config": "eager"}


class HFGenerator:
    """One GPU, bf16, the same multimodal class and attention choice train.py uses."""

    def __init__(self, base: str, attn: str = "math", device: int = 0):
        import torch
        import transformers
        self.torch = torch
        if attn == "math":
            restrict_sdpa_to_math(torch)
        cls = next(getattr(transformers, n) for n in ("AutoModelForMultimodalLM", "AutoModelForImageTextToText")
                   if hasattr(transformers, n))
        self.model = cls.from_pretrained(base, dtype=torch.bfloat16, attn_implementation=attn_implementation(attn),
                                         device_map={"": device})
        self.model.eval()
        self.device, self.pad = device, 0

    def __call__(self, prompts: list[list[int]], seed: int, greedy: bool = False, max_new: int = MAX_NEW) -> list[list[int]]:
        torch = self.torch
        torch.manual_seed(seed)
        n, width = len(prompts), max(len(p) for p in prompts)
        ids = torch.full((n, width), self.pad, dtype=torch.long)
        mask = torch.zeros((n, width), dtype=torch.long)
        for i, p in enumerate(prompts):          # left padding: every sequence ends at the last column
            ids[i, width - len(p):] = torch.tensor(p)
            mask[i, width - len(p):] = 1
        kw = dict(do_sample=False) if greedy else dict(do_sample=True, **SAMPLING)
        with torch.inference_mode():
            out = self.model.generate(input_ids=ids.to(self.device), attention_mask=mask.to(self.device),
                                      max_new_tokens=max_new, eos_token_id=list(STOP_IDS), pad_token_id=self.pad,
                                      use_cache=True, **kw)
        return out[:, width:].tolist()


def selfcheck(rows: list[dict], tok, gen, count: int = 4, steps: int = 48, agree: int = 24, log=print) -> bool:
    """Greedy decoding of `count` prompts of different lengths, alone and as one left-padded batch: the first
    `agree` tokens must be identical for every prompt."""
    picked = sorted(rows, key=lambda r: len(prompt_for(tok, r)))
    picked = [picked[round(i * (len(picked) - 1) / max(count - 1, 1))] for i in range(min(count, len(picked)))]
    prompts = [tok(prompt_for(tok, r), add_special_tokens=False).input_ids for r in picked]
    alone = [gen([p], 0, greedy=True, max_new=steps)[0] for p in prompts]
    batched = gen(prompts, 0, greedy=True, max_new=steps)
    ok = True
    for r, p, a, b in zip(picked, prompts, alone, batched):
        same = a[:agree] == b[:agree]
        ok &= same
        log(f"selfcheck {r['meta']['id']}: {len(p)} prompt tokens, first {agree} tokens {'match' if same else 'DIFFER'}")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--partial", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--base", required=True, help="the unquantized base model directory")
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--shard", default="0/1", help="I/N: this process takes every N-th row, starting at I")
    ap.add_argument("--gpu", type=int, help="GPU index (sets HIP_VISIBLE_DEVICES and CUDA_VISIBLE_DEVICES)")
    ap.add_argument("--attn", default=os.environ.get("ATTN") or "math", choices=("sdpa", "math", "eager"))
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--targets", default="finals", choices=("finals", "calls"),
                    help="finals: answers; calls: base's own thought at each tool-call step (traces.*.jsonl)")
    ap.add_argument("--families", help="comma-separated meta.family values (default: all)")
    ap.add_argument("--ids", help="file of meta ids, one per line; other rows are skipped (top-up)")
    ap.add_argument("--limit", type=int, default=0, help="first N rows of this shard only (smoke test)")
    ap.add_argument("--selfcheck", action="store_true", help="compare batched and single greedy decoding, then exit")
    a = ap.parse_args(argv)
    if a.gpu is not None:                       # before torch is imported
        os.environ["HIP_VISIBLE_DEVICES"] = os.environ["CUDA_VISIBLE_DEVICES"] = str(a.gpu)
    shard, nshards = (int(x) for x in a.shard.split("/"))
    rows, skipped = select_rows(list(read_jsonl(a.partial)), a.targets)
    prefix = "traces" if a.targets == "calls" else "finals"
    if a.families:
        fams = set(a.families.split(","))
        rows = [r for r in rows if r["meta"]["family"] in fams]
    if a.ids:
        want = {x.strip() for x in open(a.ids) if x.strip()}
        rows = [r for r in rows if r["meta"]["id"] in want]
    if skipped:
        print(f"skipped {len(skipped)} rows with image/audio parts", flush=True)
    import render
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    if render.template_sha(Path(a.base)) != render.TEMPLATE_SHAS["gemma"]:
        sys.exit("the base's chat template differs from the one the dataset is rendered with")
    gen = HFGenerator(a.base, a.attn)
    if a.selfcheck:
        return 0 if selfcheck(rows[:200], tok, gen) else 1
    if a.limit:
        rows = shard_rows(rows, shard, nshards)[:a.limit]
        shard, nshards = 0, 1
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, f"generation.{a.targets}.json"), "w") as f:
        json.dump(provenance(a.base, a.targets, a.attn, a.batch, a.k), f, indent=1)
    run(rows, tok, lambda p, s: gen(p, s), a.out_dir, shard, nshards, a.k, a.batch, a.attn,
        log=lambda s: print(s, flush=True), prefix=prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
