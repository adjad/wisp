#!/usr/bin/env python3
"""Ling V3 tokenizer + label-mask preflight.

Renders every row with the REAL Ling chat template (tools passed so schemas render
exactly as the template does, thinking disabled), counts real tokens, and computes
the INTENDED label mask: exactly the tokens of the final assistant message, i.e.
everything after the generation prompt ("<role>ASSISTANT</role>\\n<think></think>")
through and including the end-of-turn token "<|role_end|>". Loads tokenizer files and
the chat template only; no model weights, no network with --local-files-only.

NOTE: the Ling template calls tool_call.arguments.items(), so OpenAI-style JSON-string
arguments must be parsed to dicts before rendering (raw strings raise UndefinedError).
This script does that; shipped data now stores dict arguments (workaround kept; rows needing it are counted in preflight.json).

Usage:
  python preflight.py --local-files-only [--tokenizer-dir DIR] [--data-dir DIR]
"""
import argparse, copy, hashlib, json, re, statistics, sys
from pathlib import Path

DEFAULT_TOKENIZER_DIR = "/Users/adijain/Downloads/Ling-3.0-tiny-Wisp-V2-merged"
MAX_LEN = 16384
HEADER = "<role>ASSISTANT</role>\n<think></think>"
END = "<|role_end|>"


def load_tokenizer(tokenizer_dir, local_files_only=True):
    from transformers import PreTrainedTokenizerFast
    return PreTrainedTokenizerFast.from_pretrained(str(tokenizer_dir), local_files_only=local_files_only)


STRING_ARG_ROWS = set()


def prepare_messages(row):
    """Strip the training flag and parse tool-call argument strings into dicts."""
    msgs = copy.deepcopy(row["messages"])
    for m in msgs:
        m.pop("training", None)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", tc)
            if isinstance(fn.get("arguments"), str):
                STRING_ARG_ROWS.add(row["id"])
                fn["arguments"] = json.loads(fn["arguments"])
    return msgs


def render(tok, msgs, tools, generation_prompt):
    return tok.apply_chat_template(msgs, tools=tools or None, tokenize=False,
                                   add_generation_prompt=generation_prompt, enable_thinking=False)


def intended(tok, row):
    """Return dict(ids, labels, n_prefix, text) for one row, raising AssertionError on any mask problem."""
    rid = row["id"]
    flags = [m.get("training") for m in row["messages"]]
    assert flags[-1] is True and row["messages"][-1]["role"] == "assistant", rid + ": last message is not the training assistant"
    assert not any(flags[:-1]), rid + ": an earlier message carries training:true"
    msgs = prepare_messages(row)
    full = render(tok, msgs, row["tools"], False)
    prefix = render(tok, msgs[:-1], row["tools"], True)
    assert full.startswith(prefix), rid + ": generation-prompt prefix is not a prefix of the full render"
    assert prefix.endswith(HEADER), rid + ": prefix does not end with the assistant header"
    target_text = full[len(prefix):]
    assert target_text.endswith(END) and target_text.count(END) == 1, rid + ": target must contain exactly one trailing " + END
    n_asst = sum(1 for m in msgs if m["role"] == "assistant")
    assert full.count("<role>ASSISTANT</role>") == n_asst, rid + ": assistant header count mismatch"
    # the final assistant span (header .. role_end) found independently must equal HEADER + target_text
    spans = [(m.start(), m.end()) for m in re.finditer(r"<role>ASSISTANT</role>.*?" + re.escape(END), full, re.S)]
    assert len(spans) == n_asst, rid + ": assistant span count mismatch"
    assert spans[-1][1] == len(full) and spans[-1][0] == len(prefix) - len(HEADER), rid + ": final span is not the tail"
    enc = tok(full, add_special_tokens=False, return_offsets_mapping=True)
    ids, offs = enc["input_ids"], enc["offset_mapping"]
    P = len(prefix)
    labels = [-100] * len(ids)
    n_prefix = None
    for i, (s, e) in enumerate(offs):
        assert not (s < P < e), rid + ": a token straddles the context/target boundary"
        if s >= P:
            if n_prefix is None:
                n_prefix = i
            labels[i] = ids[i]
    assert n_prefix is not None and n_prefix < len(ids), rid + ": empty target span"
    assert tok(prefix, add_special_tokens=False)["input_ids"] == ids[:n_prefix], rid + ": prefix tokenization differs inside full render"
    assert tok.convert_ids_to_tokens(ids[-1]) == END, rid + ": last token is not " + END
    return {"ids": ids, "labels": labels, "n_prefix": n_prefix, "text": full}


def pct(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--tokenizer-dir", default=DEFAULT_TOKENIZER_DIR)
    ap.add_argument("--local-files-only", action="store_true")
    ap.add_argument("--max-len", type=int, default=MAX_LEN)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    tok = load_tokenizer(a.tokenizer_dir, a.local_files_only)
    template = tok.chat_template or ""
    assert "<role>ASSISTANT</role>" in template, "tokenizer has no Ling chat template"
    result, problems, over = {}, [], []
    for name in ["train", "dev"]:
        rows = [json.loads(x) for x in (a.data_dir / (name + ".jsonl")).read_text().splitlines() if x]
        lengths, targets = [], []
        for row in rows:
            try:
                r = intended(tok, row)
            except AssertionError as e:
                problems.append(str(e)); continue
            n = len(r["ids"]); t = n - r["n_prefix"]
            lengths.append(n); targets.append(t)
            if n > a.max_len:
                over.append({"split": name, "id": row["id"], "tokens": n})
        s = sorted(lengths); ts = sorted(targets)
        result[name] = {
            "rows": len(rows), "rows_ok": len(lengths),
            "tokens_total": sum(lengths), "tokens_max": s[-1], "tokens_p95": pct(s, 0.95),
            "tokens_median": int(statistics.median(s)), "tokens_mean": round(statistics.mean(s), 1),
            "labeled_tokens_total": sum(targets), "target_tokens_min": ts[0],
            "target_tokens_median": int(statistics.median(ts)), "target_tokens_p95": pct(ts, 0.95), "target_tokens_max": ts[-1],
            "rows_over_max_len": sum(1 for o in over if o["split"] == name),
        }
    status = "PASS" if not problems and not over else "FAIL"
    receipt = {
        "status": status, "max_len": a.max_len, "thinking": "disabled (enable_thinking=False)",
        "tools_passed_to_template": True, "rows_needing_string_to_dict_workaround": len(STRING_ARG_ROWS),
        "label_definition": "tokens after '" + HEADER.replace("\n", "\\n") + "' through '" + END + "' of the final assistant message; everything else -100",
        "checks": ["last message is training:true assistant, no earlier training:true", "full render startswith generation-prompt render",
                   "exactly one trailing " + END + " in target", "final assistant span is the tail (independent regex)",
                   "no token straddles the boundary; prefix tokens identical inside full render", "target non-empty, last token is " + END],
        "splits": result, "rows_over_max_len": over, "problems": problems[:50], "problem_count": len(problems),
        "tokenizer_dir_files_sha256": {f: sha(Path(a.tokenizer_dir) / f) for f in ["tokenizer.json", "tokenizer_config.json", "chat_template.jinja"] if (Path(a.tokenizer_dir) / f).exists()},
        "chat_template_sha256": hashlib.sha256(template.encode()).hexdigest(),
        "data_sha256": {f: sha(a.data_dir / f) for f in ["train.jsonl", "dev.jsonl"]},
        "tokenizer_vocab_size": len(tok),
        "actual_axolotl_prepared_label_audit_still_required": True,
    }
    out = a.out or (a.data_dir / "preflight.json")
    out.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    sys.exit(0 if status == "PASS" else 1)


if __name__ == "__main__":
    main()
