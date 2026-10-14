import json
import importlib.util
from pathlib import Path

import pytest


CONVERTER_PATH = Path(__file__).resolve().parents[1] / "training" / "lfm-dataset-v1-20261014" / "convert.py"
SPEC = importlib.util.spec_from_file_location("lfm_dataset_convert", CONVERTER_PATH)
convert = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(convert)


def _row(row_id, messages, tools=None):
    return {"id": row_id, "conversation_id": row_id, "messages": messages, "tools": tools or []}


def test_convert_tool_calls_preserves_python_literals_and_tool_results():
    args = {
        "name": "Sam O'Neil",
        "options": {"days": [1, 2], "enabled": True, "note": None},
    }
    row = _row("synthetic-tool", [
        {"role": "system", "content": "Use only available tools."},
        {"role": "user", "content": "Find Sam's calendar."},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "lookup_contact", "arguments": args},
        }]},
        {"role": "tool", "tool_call_id": "call-1", "content": "Contact found."},
        {"role": "assistant", "content": "I found the contact."},
    ], [{"type": "function", "function": {"name": "lookup_contact"}}])

    converted = convert.convert_row(row)
    assistant = converted["messages"][2]
    assert assistant["reasoning_content"] and "<think>" not in assistant["content"]
    assert assistant["tool_calls"][0]["function"] == {"name": "lookup_contact", "arguments": args}
    assert convert.parse_message_tool_calls(convert._mirror_rendered_calls(assistant)) == [
        ("lookup_contact", args)]
    assert converted["messages"][3] == {
        "role": "tool", "name": "lookup_contact", "content": "Contact found."
    }
    # Tools travel in the row's top-level `tools` (MLX-LM forwards it to the template).
    assert converted["tools"] == row["tools"]
    assert converted["messages"][0] == {"role": "system", "content": "Use only available tools."}


def test_convert_multiple_calls_keeps_order():
    row = _row("synthetic-multi", [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "Check both sources."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read_first", "arguments": {"count": 2}}},
            {"id": "b", "type": "function", "function": {"name": "read_second", "arguments": {"query": "today"}}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "one"},
        {"role": "tool", "tool_call_id": "b", "content": "two"},
        {"role": "assistant", "content": "Both sources were checked."},
    ])

    converted = convert.convert_row(row)
    assert convert.parse_message_tool_calls(convert._mirror_rendered_calls(converted["messages"][2])) == [
        ("read_first", {"count": 2}), ("read_second", {"query": "today"})
    ]
    assert [m["name"] for m in converted["messages"] if m["role"] == "tool"] == [
        "read_first", "read_second"
    ]


def test_convert_plain_and_v2_style_rows_add_think_and_keep_chat_roles(tmp_path):
    rows = [
        _row("synthetic-plain", [
            {"role": "system", "content": "Answer directly."},
            {"role": "user", "content": "What is 2 + 2?"},
            {"role": "assistant", "content": "4."},
        ]),
        _row("synthetic-v2", [
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": "Read my calendar."},
            {"role": "assistant", "content": json.dumps({"kind": "read"})},
        ]),
    ]

    converted = convert.convert_splits({"train": rows})["train"]
    assert len(converted) == 2
    assert [m["role"] for m in converted[0]["messages"]] == ["system", "user", "assistant"]
    assert converted[0]["messages"][-1]["content"] == "4."
    assert converted[1]["messages"][-1]["content"] == '{"kind": "read"}'
    assert all(item["messages"][-1]["reasoning_content"] for item in converted)
    assert all("tools" not in item for item in converted)

    # tmp_path, not the repo root: the Simulation QA sandbox cannot write there.
    path = tmp_path / "train.jsonl"
    convert.write_jsonl(path, converted)
    first = path.read_bytes()
    convert.write_jsonl(path, converted)
    assert path.read_bytes() == first


def test_argument_literal_mirrors_template_semantics():
    # Top-level strings are single-quoted; nested containers are JSON.
    assert convert._argument_literal("it's\na\\b") == "'it\\'s\\na\\\\b'"
    assert convert._argument_literal(None) == "None"
    assert convert._argument_literal(True) == "True"
    assert convert._argument_literal({"a": [True, None, "x"]}) == '{"a": [true, null, "x"]}'
    args = {"s": "it's", "n": 3, "flag": False, "nest": {"a": [True, None, 1.5, "q\"uote"]}}
    call = convert.format_tool_call("f", args)
    assert convert.parse_tool_call_list(f"[{call}]") == [("f", args)]


def test_assistant_text_with_template_markers_is_rejected():
    for text in ("<think>x", "a</think>", "<|tool_call_start|>[f()]", "done CONTINUE_FINAL_MESSAGE_TAG"):
        with pytest.raises(ValueError):
            convert._assistant_message({"role": "assistant", "content": text})


REAL_MODEL_DIR = Path("/Users/adijain/Desktop/OMLX_Model_Files/LiquidAI/LFM2.5-24B-A2B-MLX-4bit")


@pytest.mark.skipif(not (REAL_MODEL_DIR / "chat_template.jinja").is_file(),
                    reason="LFM2.5 tokenizer files are not available locally")
def test_rows_render_through_the_real_lfm_chat_template():
    transformers = pytest.importorskip("transformers")
    tok = transformers.AutoTokenizer.from_pretrained(str(REAL_MODEL_DIR), local_files_only=True)
    tools = [{"type": "function", "function": {"name": "lookup_contact", "parameters": {"type": "object"}}}]
    args = {"name": "Sam O'Neil", "options": {"days": [1, 2], "enabled": True, "note": None}, "n": 3}
    row = convert.convert_row(_row("synthetic-real", [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Earlier question."},
        {"role": "assistant", "content": "Earlier answer."},
        {"role": "user", "content": "Find Sam."},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "lookup_contact", "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "Found."},
        {"role": "assistant", "content": "Sam is found."},
    ], tools))
    messages, row_tools = row["messages"], row["tools"]
    full = tok.apply_chat_template(messages, tools=row_tools, tokenize=False)
    prompt = tok.apply_chat_template(messages[:-1], tools=row_tools,
                                     add_generation_prompt=True, tokenize=False)

    # Tools come from the template's own `tools=` mechanism, appended to the system prompt.
    assert full.startswith("<|startoftext|><|im_start|>system\nBe brief.\nList of tools: [")
    assert '"lookup_contact"' in full.split("<|im_end|>")[0]
    # The tool-call turn emits exactly the pythonic list the converter mirrors.
    call_turn = convert._mirror_rendered_calls(messages[4])
    assert call_turn in full
    assert full.count("<|tool_call_start|>") == 1
    assert "<|im_start|>tool\nFound.<|im_end|>" in full
    # Thinking is dropped for turns before the last user message, kept after it.
    assert "Earlier answer.<|im_end|>" in full and "<think>" in full
    assert full.count("<think>") == 2
    # mask_prompt semantics: the prompt (with generation prefix) is an exact prefix of
    # the full render, and the masked-in target is the final thinking + answer.
    assert full.startswith(prompt) and prompt.endswith("<|im_start|>assistant\n<think>")
    assert full[len(prompt):] == f"{messages[-1]['reasoning_content']}</think>Sam is found.<|im_end|>\n"
    ids = tok.apply_chat_template(messages, tools=row_tools, return_dict=False)
    prompt_ids = tok.apply_chat_template(messages[:-1], tools=row_tools,
                                         add_generation_prompt=True, return_dict=False)
    assert ids[:len(prompt_ids)] == prompt_ids
