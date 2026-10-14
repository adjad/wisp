import json
import importlib.util
from pathlib import Path


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
    assistant = converted["messages"][2]["content"]
    assert "<think>" in assistant and "</think>" in assistant
    assert convert.parse_message_tool_calls(assistant) == [("lookup_contact", args)]
    assert converted["messages"][3] == {
        "role": "tool", "name": "lookup_contact", "content": "Contact found."
    }
    assert "Available tools for this step (JSON Schema):" in converted["messages"][0]["content"]


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
    assert convert.parse_message_tool_calls(converted["messages"][2]["content"]) == [
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
    assert converted[0]["messages"][-1]["content"].endswith("4.")
    assert converted[1]["messages"][-1]["content"].endswith('{"kind": "read"}')
    assert all("<think>" in item["messages"][-1]["content"] for item in converted)

    # tmp_path, not the repo root: the Simulation QA sandbox cannot write there.
    path = tmp_path / "train.jsonl"
    convert.write_jsonl(path, converted)
    first = path.read_bytes()
    convert.write_jsonl(path, converted)
    assert path.read_bytes() == first
