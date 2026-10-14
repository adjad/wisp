#!/usr/bin/env python3
"""Convert the Ling V3.1 train/valid rows to the documented LFM2.5 chat format.

The source checkout omits generated train/dev and V3 replay JSONL files. The CLI
recreates those in a temporary staging tree from the checked-in source scripts,
then converts them without changing the source directory.
"""
import argparse
import ast
from collections import Counter
import importlib.util
import json
import keyword
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE.parent / "ling-dataset-v3.1-20261013"
EXPECTED_COUNTS = {"train": 2426, "valid": 279}
EXPECTED_COMPOSITION = {
    "train": {"v31": 1205, "v3r": 621, "v2r": 600},
    "valid": {"v31": 143, "v3r": 76, "v2r": 60},
}
TOOL_CALL_START = "<|tool_call_start|>"
TOOL_CALL_END = "<|tool_call_end|>"
THINK_START = "<think>"
THINK_END = "</think>"


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def write_jsonl(path, rows):
    Path(path).write_text("".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    ))


def _run(command, cwd, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(map(str, command))}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def materialize_source_rows(source_dir):
    """Regenerate ignored source rows and the V3 replay in a temporary copy."""
    source_dir = Path(source_dir).resolve()
    source_v3 = source_dir.parent / "ling-dataset-v3-20261012"
    for required in (source_dir / "generate.py", source_dir / "v3_replay.py",
                     source_dir / "v2_replay.py", source_v3 / "generate.py",
                     source_dir / "v2_replay.train.jsonl", source_dir / "v2_replay.dev.jsonl"):
        if not required.is_file():
            raise FileNotFoundError(f"Required source input is missing: {required}")

    with tempfile.TemporaryDirectory(prefix=".source-build-", dir=HERE) as tmp:
        training_dir = Path(tmp) / "training"
        training_dir.mkdir()
        subprocess_tmp = Path(tmp) / "tmp"
        subprocess_tmp.mkdir()
        env = os.environ.copy()
        env["TMPDIR"] = str(subprocess_tmp)
        env["TMP"] = str(subprocess_tmp)
        env["TEMP"] = str(subprocess_tmp)
        staged_source = training_dir / source_dir.name
        staged_v3 = training_dir / source_v3.name
        shutil.copytree(source_dir, staged_source)
        shutil.copytree(source_v3, staged_v3)

        _run([sys.executable, str(staged_source / "v3_replay.py")], staged_source, env=env)

        generated = Path(tmp) / "generated"
        _run([sys.executable, str(staged_source / "generate.py"),
              "--output-dir", str(generated)], staged_source, env=env)
        return {
            "train": read_jsonl(generated / "train.jsonl"),
            "valid": read_jsonl(generated / "dev.jsonl"),
        }


def _python_literal(value):
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not JSON values")
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_python_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("tool argument object keys must be strings")
        return "{" + ", ".join(
            f"{_python_literal(key)}: {_python_literal(item)}" for key, item in value.items()
        ) + "}"
    raise ValueError(f"unsupported JSON argument value: {type(value).__name__}")


def _as_arguments(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("tool-call arguments must be a JSON object")
    return value


def format_tool_call(name, arguments):
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError(f"tool name is not a Python identifier: {name!r}")
    arguments = _as_arguments(arguments)
    parts = []
    for key, value in arguments.items():
        if not isinstance(key, str) or not key.isidentifier() or keyword.iskeyword(key):
            raise ValueError(f"tool argument name is not a Python keyword name: {key!r}")
        parts.append(f"{key}={_python_literal(value)}")
    return f"{name}({', '.join(parts)})"


def parse_tool_call_list(payload):
    """Parse the Python list payload and return ordered (name, arguments) pairs."""
    parsed = ast.parse(payload, mode="eval").body
    if not isinstance(parsed, ast.List):
        raise ValueError("tool-call payload must be a Python list")
    calls = []
    for item in parsed.elts:
        if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Name):
            raise ValueError("tool calls must use bare Python function names")
        if item.args or any(arg.arg is None for arg in item.keywords):
            raise ValueError("tool calls must use keyword arguments only")
        name = item.func.id
        if keyword.iskeyword(name):
            raise ValueError(f"tool name is a Python keyword: {name!r}")
        arguments = {}
        for item_kw in item.keywords:
            if item_kw.arg in arguments:
                raise ValueError(f"duplicate tool argument: {item_kw.arg}")
            arguments[item_kw.arg] = ast.literal_eval(item_kw.value)
        calls.append((name, arguments))
    return calls


def parse_message_tool_calls(content):
    calls = []
    cursor = 0
    while True:
        start = content.find(TOOL_CALL_START, cursor)
        if start < 0:
            break
        end = content.find(TOOL_CALL_END, start + len(TOOL_CALL_START))
        if end < 0:
            raise ValueError("unterminated LFM2 tool-call block")
        payload = content[start + len(TOOL_CALL_START):end]
        calls.extend(parse_tool_call_list(payload))
        cursor = end + len(TOOL_CALL_END)
    if TOOL_CALL_END in content[cursor:]:
        raise ValueError("LFM2 tool-call end token without a start token")
    return calls


def _schema_section(tools):
    schemas = json.dumps(tools, ensure_ascii=False, indent=2)
    return "Available tools for this step (JSON Schema):\n" + schemas


def _think_for(message):
    if message.get("tool_calls"):
        return "I should use the listed tool definitions and conversation to form this call."
    return "I should answer using only the conversation context."


def _source_calls(message):
    calls = []
    for tool_call in message.get("tool_calls", []):
        function = tool_call["function"]
        calls.append((function["name"], _as_arguments(function.get("arguments", {}))))
    return calls


def _assistant_content(message):
    original = message.get("content", "") or ""
    source_calls = _source_calls(message)
    if source_calls:
        rendered_calls = ", ".join(format_tool_call(name, args) for name, args in source_calls)
        content = f"{TOOL_CALL_START}[{rendered_calls}]{TOOL_CALL_END}"
        if original:
            content += "\n" + original
    else:
        if TOOL_CALL_START in original or TOOL_CALL_END in original:
            raise ValueError("source assistant text contains an LFM2 tool-call marker")
        content = original
    return f"{THINK_START}\n{_think_for(message)}\n{THINK_END}\n{content}"


def convert_row(row):
    source_messages = row.get("messages")
    if not isinstance(source_messages, list) or not source_messages:
        raise ValueError(f"{row.get('id', '<unknown>')}: missing source messages")
    tools = row.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError(f"{row.get('id', '<unknown>')}: tools must be a list")

    tool_names = {}
    for message in source_messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls", []):
            call_id = tool_call.get("id")
            name = tool_call.get("function", {}).get("name")
            if not call_id or not name or call_id in tool_names:
                raise ValueError(f"{row.get('id', '<unknown>')}: invalid or duplicate tool-call id")
            tool_names[call_id] = name

    system_parts = []
    converted = []
    expected_call_groups = []
    actual_call_groups = []
    for message in source_messages:
        role = message.get("role")
        if role == "system":
            content = message.get("content", "")
            if not isinstance(content, str):
                raise ValueError("system message content must be text")
            system_parts.append(content)
            continue
        if role == "assistant":
            content = _assistant_content(message)
            expected = _source_calls(message)
            actual = parse_message_tool_calls(content)
            if actual != expected:
                raise ValueError(f"{row.get('id', '<unknown>')}: tool-call round-trip mismatch")
            expected_call_groups.append(expected)
            actual_call_groups.append(actual)
            converted.append({"role": role, "content": content})
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in tool_names:
                raise ValueError(f"{row.get('id', '<unknown>')}: tool result has no matching call")
            converted.append({"role": "tool", "name": tool_names[call_id],
                              "content": message.get("content", "") or ""})
            continue
        if role not in ("user", "developer"):
            raise ValueError(f"{row.get('id', '<unknown>')}: unsupported role {role!r}")
        content = message.get("content", "")
        if not isinstance(content, str):
            raise ValueError(f"{row.get('id', '<unknown>')}: {role} content must be text")
        converted.append({"role": role, "content": content})

    if not system_parts:
        raise ValueError(f"{row.get('id', '<unknown>')}: source row has no system message")
    if not converted or converted[-1]["role"] != "assistant":
        raise ValueError(f"{row.get('id', '<unknown>')}: target message must be the final assistant turn")
    system_content = "\n\n".join(part for part in system_parts if part)
    if tools:
        system_content += ("\n\n" if system_content else "") + _schema_section(tools)
    converted.insert(0, {"role": "system", "content": system_content})

    if expected_call_groups != actual_call_groups:
        raise ValueError(f"{row.get('id', '<unknown>')}: tool-call sequence mismatch")
    out = {"messages": converted}
    if "id" in row:
        out["id"] = row["id"]
    if "conversation_id" in row:
        out["conversation_id"] = row["conversation_id"]
    return out


def convert_splits(source_splits):
    return {split: [convert_row(row) for row in rows] for split, rows in source_splits.items()}


def check_source_exclusion(converted_splits, source_dir):
    """Reuse the V3.1 generator's exact held-out prompt overlap function and limit."""
    source_dir = Path(source_dir).resolve()
    module_name = "_lfm_dataset_source_generate"
    spec = importlib.util.spec_from_file_location(module_name, source_dir / "generate.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load source generator: {source_dir / 'generate.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        maxima = {}
        for split, rows in converted_splits.items():
            best = 0.0
            for row in rows:
                for message in row["messages"]:
                    if message["role"] != "user":
                        continue
                    overlap = module.max_heldout_overlap(message["content"])
                    if overlap >= module.OVERLAP_LIMIT:
                        raise ValueError(
                            f"{split}/{row.get('id', '<unknown>')}: held-out prompt overlap "
                            f"{overlap:.3f} reaches source limit {module.OVERLAP_LIMIT:.1f}"
                        )
                    best = max(best, overlap)
            maxima[split] = best
        return maxima
    finally:
        sys.modules.pop(module_name, None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    args = parser.parse_args(argv)

    source_splits = materialize_source_rows(args.source_dir)
    counts = {split: len(rows) for split, rows in source_splits.items()}
    if counts != EXPECTED_COUNTS:
        raise ValueError(f"source row counts changed: expected {EXPECTED_COUNTS}, got {counts}")
    composition = {
        split: dict(sorted(Counter(row["id"].split("-", 1)[0] for row in rows).items()))
        for split, rows in source_splits.items()
    }
    if composition != EXPECTED_COMPOSITION:
        raise ValueError(
            f"source row composition changed: expected {EXPECTED_COMPOSITION}, got {composition}"
        )
    converted = convert_splits(source_splits)
    maxima = check_source_exclusion(converted, args.source_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.output_dir / "train.jsonl", converted["train"])
    write_jsonl(args.output_dir / "valid.jsonl", converted["valid"])
    print(json.dumps({"rows": counts, "composition": composition,
                      "heldout_max_jaccard": maxima}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
