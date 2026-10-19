#!/usr/bin/env python3
"""Standard-library structural checks for the Wisp assistant task corpus."""

import json
import re
import ast
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
ROWS = ROOT / "cases.jsonl"
EXPECTED_SPLITS = {"smoke": 12, "regression": 24, "challenge": 12}
ALLOWED_CATEGORIES = {
    "calendar", "mail", "messages", "memory", "files", "research",
    "system", "utilities", "ambiguity", "safety",
}
ALLOWED_RISKS = {
    "read_only", "write_authorized", "live_lookup", "confirmation_required",
    "clarification", "source_injection", "refusal",
}


def fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def normalize_prompt(value: str) -> str:
    return " ".join(value.lower().split())


def available_documented_tools() -> set[str]:
    source = REPO_ROOT / "service/tools/short_descriptions.py"
    tree = ast.parse(source.read_text())
    for statement in tree.body:
        if isinstance(statement, ast.AnnAssign) and getattr(statement.target, "id", None) == "SHORT":
            return set(ast.literal_eval(statement.value))
    fail("could not read the documented tool inventory")


def existing_prompt_inventory() -> set[str]:
    prompts = set()
    suite = REPO_ROOT / "test_fixtures/routing_stress/suite.json"
    if suite.exists():
        data = json.loads(suite.read_text())
        prompts.update(normalize_prompt(case["prompt"]) for case in data.get("cases", []))
    demo = REPO_ROOT / "docs/DEMO_TEST_PROMPTS.md"
    if demo.exists():
        prompts.update(normalize_prompt(prompt) for prompt in re.findall(r"`([^`]+)`", demo.read_text()))
    return prompts


def main() -> None:
    schema = json.loads((ROOT / "schema.json").read_text())
    if schema.get("title") != "Wisp assistant task corpus case":
        fail("unexpected schema")
    required_keys = set(schema["required"])
    expected_tools = available_documented_tools()
    existing_prompts = existing_prompt_inventory()

    seen_ids = set()
    seen_prompts = set()
    split_counts = Counter()
    category_counts = Counter()
    rows = []
    for line_number, line in enumerate(ROWS.read_text().splitlines(), 1):
        if not line.strip():
            fail(f"blank JSONL line at {line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            fail(f"invalid JSON on line {line_number}: {exc}")
        if set(row) != required_keys:
            fail(f"unexpected or missing fields for line {line_number}")
        if not re.fullmatch(r"ATC-\d{3}", row.get("id", "")):
            fail(f"invalid id on line {line_number}")
        if row["id"] in seen_ids:
            fail(f"duplicate id {row['id']}")
        seen_ids.add(row["id"])
        if row.get("split") not in schema["properties"]["split"]["enum"]:
            fail(f"invalid split for {row['id']}")
        if row.get("category") not in ALLOWED_CATEGORIES:
            fail(f"invalid category for {row['id']}")
        if row.get("risk") not in ALLOWED_RISKS:
            fail(f"invalid risk for {row['id']}")
        if row.get("difficulty") not in schema["properties"]["difficulty"]["enum"]:
            fail(f"invalid difficulty for {row['id']}")
        conversation = row.get("conversation")
        if not isinstance(conversation, list) or not conversation:
            fail(f"missing conversation for {row['id']}")
        if any(
            set(turn) != {"role", "content"}
            or turn.get("role") not in {"user", "assistant"}
            or not isinstance(turn.get("content"), str)
            or not turn["content"].strip()
            for turn in conversation
        ):
            fail(f"invalid conversation turn for {row['id']}")
        if conversation[-1]["role"] != "user":
            fail(f"case must end with a user request: {row['id']}")
        if not row.get("setup", "").strip():
            fail(f"missing setup for {row['id']}")
        expected = row.get("expected", {})
        if set(expected) != {"tools", "behavior"}:
            fail(f"unexpected or missing expected fields for {row['id']}")
        if not isinstance(expected.get("tools"), list) or not expected.get("behavior"):
            fail(f"missing expected tools/behavior for {row['id']}")
        unknown_tools = set(expected["tools"]) - expected_tools
        if unknown_tools:
            fail(f"unknown or retired tool names for {row['id']}: {sorted(unknown_tools)}")
        if any(not isinstance(item, str) or not item.strip() for item in expected["behavior"]):
            fail(f"invalid behavior rubric for {row['id']}")

        prompt = " ".join(normalize_prompt(turn["content"]) for turn in conversation if turn["role"] == "user")
        if prompt in seen_prompts:
            fail(f"duplicate user conversation for {row['id']}")
        individual_prompts = {normalize_prompt(turn["content"]) for turn in conversation if turn["role"] == "user"}
        overlap = individual_prompts & existing_prompts
        if overlap:
            fail(f"exact prompt already exists in Wisp test material for {row['id']}: {sorted(overlap)[0]}")
        seen_prompts.add(prompt)
        split_counts[row["split"]] += 1
        category_counts[row["category"]] += 1
        rows.append(row)

    if len(rows) != 48:
        fail(f"expected 48 rows, found {len(rows)}")
    if dict(split_counts) != EXPECTED_SPLITS:
        fail(f"split counts are {dict(split_counts)}, expected {EXPECTED_SPLITS}")
    missing = ALLOWED_CATEGORIES - category_counts.keys()
    if missing:
        fail(f"categories with no cases: {sorted(missing)}")
    ids = [row["id"] for row in rows]
    if ids != [f"ATC-{number:03d}" for number in range(1, 49)]:
        fail("IDs must be contiguous and ordered from ATC-001 through ATC-048")

    print(
        f"PASS: {len(rows)} cases; splits={dict(split_counts)}; "
        f"categories={dict(category_counts)}; {len(expected_tools)} documented tools checked; "
        f"no exact prompt overlap with existing demo/routing suite"
    )


if __name__ == "__main__":
    main()
