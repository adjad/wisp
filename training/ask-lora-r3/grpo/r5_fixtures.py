"""Synthetic builder output + a fake renderer, so export_prompts.main() runs end to end in the torch-free tests
(test_export_r5.py). No tokenizer, no model, no network. Not a test module (pytest does not collect it).

A "build" is a directory with all.jsonl + meta.jsonl (and train/valid pairs), the shape the builder writes.
Builder meta fields RL-SCOPE C6/C7 add (clarify, unsafe_reads, thread, qkind, recall_refs, recall_absent, recall_ok)
are passed through `meta=` so each test decides whether the build has them."""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_prompts as X  # noqa: E402

CLOCK = {"role": "system", "content": "\nThe current date and time is Tuesday, April 6, 2027 at 10:37 PM."}
SKILLS = ("Installed skills (read a skill with read_skill):\n- idea-refine: Refine a rough idea.\n"
          "- mac-controls: Volume, Wi-Fi and focus.\n- travel-notes: Trip planning notes.\n")
RULES = {"role": "system", "content": "Be helpful. " * 40 + "\n\nInstalled skills\n" + SKILLS.split("\n", 1)[1]}

_SYL = ["ba", "ko", "di", "mu", "se", "ra", "ti", "lo", "pe", "nu", "fa", "gi", "ho", "ve", "za", "wu"]


def word(i: int) -> str:
    """Distinct alphabetic pseudo-words: digits would fold to the same 'n' and collapse the prompts."""
    return _SYL[i % 16] + _SYL[(i // 16) % 16] + _SYL[(i // 256) % 16] + _SYL[(i // 4096) % 16] + "n"


def call_msg(name, args, cid="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
                                                                "function": {"name": name, "arguments": args}}]}


def scenario(family, idx, user, steps, earlier=(), meta=None, final_meta=None, writes=()):
    """Examples + metas for one turn. steps: ("call", name, args, result) | ("final", text) | ("ask", text)."""
    base = [RULES, CLOCK]
    for u, a in earlier:
        base += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    base.append({"role": "user", "content": user})
    hist, out = list(base), []
    sid = f"{family}-{idx:03d}"
    for i, st in enumerate(steps):
        if st[0] == "call":
            target = call_msg(st[1], st[2], f"c{i}")
        else:
            target = {"role": "assistant", "content": st[1]}
        m = {"id": f"{sid}-s{i}", "family": family, "paraphrase_of": None, "notes": "", "step": i,
             "steps_in_turn": len(steps), "target_kind": "call" if st[0] == "call" else "final", "prompt": user,
             "clock": "Tue 2027-04-06 22:37", "writes": [], "approve": None, "effects": [],
             "turn_writes": list(writes), "think": False, "thought": None}
        m.update(meta or {})
        if st[0] != "call":
            m.update(final_meta or {})
        out.append(({"messages": hist + [target], "tools": [{"type": "function", "function": {"name": "send_message"}},
                                                           {"type": "function", "function": {"name": "lookup_contact"}}]}, m))
        if st[0] == "call":
            hist = hist + [target, {"role": "tool", "tool_call_id": f"c{i}", "content": st[3]}]
    return out


def write_build(path: Path, pairs, valid_ids=(), pair_names=("all.jsonl", "meta.jsonl")) -> Path:
    """all/meta for every pair, plus train/valid splits by id (valid_ids)."""
    path.mkdir(parents=True, exist_ok=True)
    v = set(valid_ids)
    sets = {"all": list(pairs), "train": [p for p in pairs if p[1]["id"] not in v], "valid": [p for p in pairs if p[1]["id"] in v]}
    for name, rows in sets.items():
        (path / (f"{name}.jsonl" if name != "all" else "all.jsonl")).write_text("".join(json.dumps(e) + "\n" for e, _ in rows))
        (path / ("meta.jsonl" if name == "all" else f"{name}.meta.jsonl")).write_text("".join(json.dumps(m) + "\n" for _, m in rows))
    return path


# ---------------------------------------------------------------------------------------------- the fake renderer
class FakeTok:
    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": text.split()}

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=False, **kw):
        def txt(m):
            c = m["content"]
            return "".join("<|audio|>" if p.get("type") == "audio" else p.get("text", "") for p in c) if isinstance(c, list) else c
        done = msgs[-1]["role"] == "assistant" and not add_generation_prompt
        ctx = msgs[:-1] if done else msgs
        out = "\n".join(f"<{m['role']}>{txt(m)}" for m in ctx) + "\n<|turn>model\n"
        return out + txt(msgs[-1]) + "<turn|>\n" if done else out


def _gv(v):
    """One argument value in Gemma's tool-call syntax, as the production template renders it (reward.parse reads it back)."""
    if isinstance(v, str):
        return '<|"|>' + v + '<|"|>'
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_gv(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{_gv(x)}" for k, x in v.items()) + "}"
    return '<|"|>' + str(v) + '<|"|>'


def gemma_call(name, args) -> str:
    if isinstance(args, str):
        args = json.loads(args)
    return f"<|tool_call>call:{name}{{" + ",".join(f"{k}:{_gv(v)}" for k, v in (args or {}).items()) + "}<tool_call|>"


class FakeRender:
    FORCED = "<|channel>thought\n"
    WITH_ARGS = False        # round 5 scores each row's ideal (ideal_trips): the completion must carry the real args. Rounds 3 and 4
                             # keep the old argument-less completion, whose digests test_rounds_3_and_4_unchanged pins
    TEMPLATE_SHAS = {"gemma": "fake"}

    @staticmethod
    def template_sha(path):
        return "fake"

    @staticmethod
    def render(tok, ex, meta):
        hist, target = ex["messages"][:-1], ex["messages"][-1]
        body = "\n".join(f"[{m['role']}] {X._msg_text(m)}" + "".join(
            f" CALL {tc['function']['name']} {json.dumps(tc['function']['arguments'], sort_keys=True)}" for tc in m.get("tool_calls") or ())
            for m in hist)
        forced = hist[-1]["role"] == "tool"
        prompt = body + "\n[model]\n" + (FakeRender.FORCED if forced else "")
        if target.get("tool_calls"):
            text = "".join(gemma_call(tc["function"]["name"], tc["function"]["arguments"]) if FakeRender.WITH_ARGS else
                           f"<|tool_call>call:{tc['function']['name']}{{}}<tool_call|>" for tc in target["tool_calls"])
        else:
            text = target.get("content") or ""
        return {"id": meta["id"], "family": meta["family"], "kind": meta.get("target_kind"), "think": False, "prompt": prompt,
                "completion": ("\n<channel|>" if forced else "") + text + "<turn|>"}

    @staticmethod
    def render_vision(tok, row, rel):
        q = "".join(f"<|image|>" for _ in rel) + " " + " ".join(str(m.get("content")) for m in row.get("messages") or ())
        return {"prompt": q, "completion": row["target"] + "<turn|>"}

    @staticmethod
    def vision_is_dev(sid, meta=None):
        return (meta or {}).get("split") == "dev"

    @staticmethod
    def audio_is_dev(row):
        return (row.get("meta") or {}).get("split") == "dev"


def run_main(argv, extra_patches=()):
    """export_prompts.main(argv) with the renderer, tokenizer and live registry faked. Returns its exit code.
    main() updates module-level weight tables for round 4/5 (VISION_WEIGHTS, PICTURE_READ_WEIGHTS, WRITE_TOOLS_R4); they are
    restored afterwards so a later round-3 run in the same process is not affected (a quirk of the original export)."""
    saved = (dict(X.VISION_WEIGHTS), dict(X.PICTURE_READ_WEIGHTS), X.WRITE_TOOLS_R4)
    argv = list(argv)
    FakeRender.WITH_ARGS = "--round" in argv and argv[argv.index("--round") + 1] == "5"
    try:
        with ExitStack() as st:
            st.enter_context(mock.patch.object(X, "load_render", lambda: FakeRender))
            st.enter_context(mock.patch.object(X, "load_tokenizer", lambda p: FakeTok()))
            st.enter_context(mock.patch.object(X, "live_effect_tools", lambda *a, **k: None))
            for p in extra_patches:
                st.enter_context(p)
            return X.main(argv)
    finally:
        X.VISION_WEIGHTS.clear()
        X.VISION_WEIGHTS.update(saved[0])
        X.PICTURE_READ_WEIGHTS.clear()
        X.PICTURE_READ_WEIGHTS.update(saved[1])
        X.WRITE_TOOLS_R4 = saved[2]
