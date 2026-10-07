#!/usr/bin/env python3
"""Build offline development requests using the serving request module.

No model, tools, native state, credentials, network or inference is accessed.
The output is a reproducible input bundle, never a performance result.
"""
import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REQUEST_PATH = ROOT / "service/router/intent/request.py"
SCHEMA_PATH = ROOT / "service/router/intent/intent.schema.v1.json"
# Load just the pure-stdlib builder, without service package import side effects.
spec = importlib.util.spec_from_file_location("wisp_offline_read_request", REQUEST_PATH)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
SCHEMA = json.loads(SCHEMA_PATH.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases():
    rows = [json.loads(line) for line in (HERE / "cases.jsonl").read_text().splitlines()]
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate development IDs")
    if not all(r.get("synthetic") is True and r.get("development_only") is True for r in rows):
        raise ValueError("Only explicitly synthetic development fixtures are supported")
    return rows


def build_variants(case):
    messages = policy.build_messages(case["prompt"], context=case["history"],
                                     prior_tools=case["prior_tools"],
                                     now=datetime.fromisoformat(case["now"]))
    # Schema grammar stays declared identically in every API variant. Changing
    # schema text here isolates prompt-text differences, not decoder differences.
    rows = {}
    for variant in ("core", "serving", "schema_text"):
        body = {"messages": deepcopy(messages), **policy.completion_options(SCHEMA)}
        if variant == "core":
            body["messages"][0]["content"] = policy.SYSTEM
        elif variant == "schema_text":
            body["messages"][0]["content"] += "\nIntent schema: " + json.dumps(
                SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        rows[variant] = body
    return rows


def write_bundle(output):
    output = Path(output)
    # Never overwrite an original dataset, previous bundle or measured result.
    cases = load_cases()
    output.mkdir(parents=True, exist_ok=False)
    files = []
    for variant in ("core", "serving", "schema_text"):
        path = output / (variant + ".jsonl")
        with path.open("x") as stream:
            for case in cases:
                body = build_variants(case)[variant]
                row = {"id": case["id"], "request": body,
                       "expected": case["expected"], "development_only": True,
                       "hf_generation": {"do_sample": False,
                           "max_new_tokens": body["max_tokens"],
                           "chat_template_kwargs": body["chat_template_kwargs"]},
                       "decoder_note": "HF greedy generation is unconstrained; API schema enforcement is unqualified."}
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        files.append(path)
    manifest = {"request_version": policy.REQUEST_VERSION, "cases": len(cases),
                "variants": [p.stem for p in files], "inference_performed": False,
                "heldout_qualification": False, "source_hashes": {
                    "request.py": sha(REQUEST_PATH), "intent.schema.v1.json": sha(SCHEMA_PATH),
                    "cases.jsonl": sha(HERE / "cases.jsonl"), "build_requests.py": sha(Path(__file__))},
                "output_hashes": {p.name: sha(p) for p in files},
                "serving_transport_fix": "pending retained inference file-owner handoff",
                "limits": ["No tokenization or model measurements", "No original corpus/results changed",
                           "Input construction aligns; decoder/runtime/deadline must be qualified separately"]}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(write_bundle(args.output), indent=2))
