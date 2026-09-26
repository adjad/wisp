#!/usr/bin/env python3
"""Run the same synthetic acceptance, rejection and round-trip cases in three runtimes.

No network, browser, credentials, database, or app launch. Missing runtimes fail.
--sync-schema updates only the owned JS/Swift schema and type mirrors.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from service.browser.contracts import SCHEMA, ContractViolation, negotiate, validate
from service.discovery.contracts import validate_completion, validate_proposal

JS = ROOT / "browser-extension/shared/contracts.js"
SWIFT = ROOT / "app/Sources/WispApp/BrowserContracts.swift"


def swift_type(s):
    kind = s["type"]
    if kind == "nullable": return swift_type(s["item"]) + "?"
    if kind == "ref": return s["name"]
    if kind == "array": return "[" + swift_type(s["item"]) + "]"
    if kind == "enum": return {str: "String", int: "Int64", bool: "Bool"}[type(s["values"][0])]
    return {"boolean": "Bool", "string": "String", "integer": "Int64"}[kind]


def swift_records():
    lines = ["extension WispBrowserContracts {"]
    for name, spec in SCHEMA.items():
        fields = spec["fields"]
        lines += [f"    struct {name}: BrowserWireRecord {{", f'        static let contractName = "{name}"']
        lines += [f"        let {k}: {swift_type(s)}" for k, s in fields.items()]
        lines += ["        enum CodingKeys: String, CodingKey {", "            case " + ", ".join(fields), "        }",
                  "        func encode(to encoder: Encoder) throws {", "            var c = encoder.container(keyedBy: CodingKeys.self)"]
        for k, s in fields.items():
            if s["type"] == "nullable":
                lines += [f"            if let value = {k} {{ try c.encode(value, forKey: .{k}) }} else {{ try c.encodeNil(forKey: .{k}) }}"]
            else: lines += [f"            try c.encode({k}, forKey: .{k})"]
        lines += ["        }", "    }"]
    lines += ["    static func roundTrip(_ name: String, _ data: Data) throws -> Any {", "        let encoded: Data", "        switch name {"]
    for name in SCHEMA:
        lines += [f'        case "{name}": encoded = try JSONEncoder().encode(decode({name}.self, from: data))']
    lines += ['        default: throw BrowserContractViolation(message: "Unknown contract", code: "invalid_payload")',
              "        }", "        return try JSONSerialization.jsonObject(with: encoded)", "    }", "}"]
    return "\n".join(lines)


def js_type(s):
    kind = s["type"]
    if kind == "nullable": return "(" + js_type(s["item"]) + "|null)"
    if kind == "ref": return s["name"]
    if kind == "array": return "Array<" + js_type(s["item"]) + ">"
    if kind == "enum": return "(" + "|".join(json.dumps(v) for v in s["values"]) + ")"
    return {"boolean": "boolean", "string": "string", "integer": "number"}[kind]


def js_records():
    return "\n".join("/**\n * @typedef {Object} " + name + "\n" + "\n".join(
        " * @property {" + js_type(s) + "} " + k for k, s in spec["fields"].items()) + "\n */"
        for name, spec in SCHEMA.items())


def mirror(path, marker, content, sync):
    text = path.read_text()
    start, end = "// BEGIN " + marker + "\n", "\n// END " + marker
    before, rest = text.split(start, 1)
    old, after = rest.split(end, 1)
    if old != content:
        if not sync: raise AssertionError(f"Stale {path.relative_to(ROOT)} {marker}; run --sync-schema")
        path.write_text(before + start + content + end + after)


def check_mirrors(sync=False):
    raw = json.dumps(SCHEMA, sort_keys=True, separators=(",", ":"))
    mirror(JS, "SCHEMA", "const SCHEMA = " + raw + ";", sync)
    mirror(SWIFT, "SCHEMA", 'private let browserSchemaJSON = #"' + raw + '"#', sync)
    mirror(SWIFT, "RECORDS", swift_records(), sync)
    mirror(JS, "RECORDS", js_records(), sync)


def cases():
    result = []
    for path in sorted((ROOT / "test_fixtures/browser_contracts").glob("*.json")):
        result.extend(json.loads(path.read_text()))
    assert result and len({c['name'] for c in result}) == len(result), "Missing/duplicate cases"
    return result


def run_case(c):
    try:
        op = c["operation"]
        if op == "validate": result = validate(c["contract"], c["payload"])
        elif op == "negotiate": result = negotiate(c["local"], c["remote"])
        elif op == "proposal": validate_proposal(c["item"], c["proposal"]); result = None
        elif op == "completion": validate_completion(c["item"], c["receipt"], c["proposal"]); result = None
        else: raise AssertionError("Unknown operation")
        return {"name": c["name"], "valid": True, "result": result}
    except ContractViolation as exc:
        return {"name": c["name"], "valid": False, "error": exc.code}


def assert_expected(corpus, results):
    assert len(corpus) == len(results)
    for c, actual in zip(corpus, results):
        expected = {"name": c["name"], "valid": c["valid"]}
        if not c["valid"]: expected["error"] = c["error"]
        else: expected["result"] = c.get("payload") if c["operation"] == "validate" else c.get("result")
        assert actual == expected, f"{c['name']}: {actual} != {expected}"


NODE_RUNNER = r'''
const fs = require('fs'), C = require(process.argv[1]);
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
// JSON cannot represent holes. Exercise the exported in-memory boundary before
// the shared wire corpus, including inherited indexes that are not own elements.
const assert = require('node:assert/strict');
const completion = cases.find(c => c.name === 'verified_completion');
const evidence = completion.receipt.evidence[0];
const inherited = new Array(1);
Object.setPrototypeOf(inherited, Object.assign(Object.create(Array.prototype), {0: evidence}));
const sparseArrays = [new Array(1), [, evidence], [evidence, ,], [evidence, , evidence], inherited];
for (const evidenceArray of sparseArrays) {
  const receipt = {...completion.receipt, evidence: evidenceArray};
  const invalid = e => e instanceof C.ContractViolation && e.code === 'invalid_payload';
  assert.throws(() => C.validate('ActionReceipt', receipt), invalid);
  assert.throws(() => C.validateCompletion(completion.item, receipt, completion.proposal), invalid);
}
process.stdout.write(JSON.stringify(cases.map(c => {
  try {
    let result = null;
    if (c.operation === 'validate') result = C.validate(c.contract, c.payload);
    else if (c.operation === 'negotiate') result = C.negotiate(c.local, c.remote);
    else if (c.operation === 'proposal') C.validateProposal(c.item, c.proposal);
    else if (c.operation === 'completion') C.validateCompletion(c.item, c.receipt, c.proposal);
    else throw Error('Unknown operation');
    return {name:c.name, valid:true, result};
  } catch (e) { if (!e.code) throw e; return {name:c.name, valid:false, error:e.code}; }
})));
'''
SWIFT_RUNNER = r'''
import Foundation
@main struct ContractCheck {
    static func main() throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let cases = try JSONSerialization.jsonObject(with: data) as! [[String: Any]]
        var results = [[String: Any]]()
        for c in cases {
            do {
                var result: Any = NSNull()
                switch c["operation"] as! String {
                case "validate":
                    result = try WispBrowserContracts.roundTrip(c["contract"] as! String,
                        JSONSerialization.data(withJSONObject: c["payload"]!))
                case "negotiate": result = try WispBrowserContracts.negotiate(c["local"]!, c["remote"]!)
                case "proposal": try WispBrowserContracts.validateProposal(c["item"]!, c["proposal"]!)
                case "completion": try WispBrowserContracts.validateCompletion(c["item"]!, c["receipt"]!, c["proposal"]!)
                default: fatalError("Unknown operation")
                }
                results.append(["name": c["name"]!, "valid": true, "result": result])
            } catch let error as BrowserContractViolation {
                results.append(["name": c["name"]!, "valid": false, "error": error.code])
            }
        }
        FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]))
    }
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync-schema", action="store_true")
    args = parser.parse_args()
    check_mirrors(args.sync_schema)
    corpus = cases()
    python_results = [run_case(c) for c in corpus]
    assert_expected(corpus, python_results)
    print(f"Python: {len(corpus)} cases passed", flush=True)
    with tempfile.TemporaryDirectory(prefix="wisp-browser-contracts-") as temp:
        folder = Path(temp)
        fixture = folder / "cases.json"
        fixture.write_text(json.dumps(corpus, ensure_ascii=False))
        node = subprocess.run(["node", "-e", NODE_RUNNER, str(JS), str(fixture)], check=True, capture_output=True, text=True)
        assert_expected(corpus, json.loads(node.stdout))
        print(f"JavaScript: {len(corpus)} shared cases + 10 in-memory sparse-array assertions passed", flush=True)
        harness = folder / "ContractCheck.swift"
        harness.write_text(SWIFT_RUNNER)
        binary = folder / "contract-check"
        subprocess.run(["swiftc", "-swift-version", "5", "-parse-as-library", "-module-cache-path", str(folder / "cache"),
                        str(SWIFT), str(harness), "-o", str(binary)], check=True)
        swift = subprocess.run([str(binary), str(fixture)], check=True, capture_output=True, text=True)
        assert_expected(corpus, json.loads(swift.stdout))
        print(f"Swift: {len(corpus)} typed decode/encode cases passed", flush=True)
    print("Schema mirrors and cross-language compatibility passed.")


if __name__ == "__main__": main()
