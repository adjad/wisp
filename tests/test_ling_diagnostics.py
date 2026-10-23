"""Offline contract/negative controls. No service imports, real tools or inference."""
from __future__ import annotations

import asyncio
import copy
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.ling_diagnostics import core
from scripts.ling_diagnostics import live
from scripts.ling_diagnostics.__main__ import select


def response(text="53", calls=None, model="offline-fixture", finish=None):
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": text,
            **({"tool_calls": calls} if calls else {})},
            "finish_reason": finish or ("tool_calls" if calls else "stop")}]}


def tool_call(name="lookup_contact", arguments=None, ident="call-1"):
    return {"id": ident, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {"name": "Mira Chen"})}}


class CorpusChecks(unittest.TestCase):
    def setUp(self):
        self.cases = core.load_cases()

    def test_corpus_has_distinct_categories_and_reuse_provenance(self):
        self.assertEqual(len(self.cases), 44)
        self.assertEqual(len({c["category"] for c in self.cases}), 13)
        self.assertTrue(any("ATC-048" in c["origin"] for c in self.cases))
        self.assertTrue(all(c["rubric"] and c["origin"] for c in self.cases))

    def test_oracles_never_enter_request_or_fixture_context_early(self):
        case = copy.deepcopy(next(c for c in self.cases if c["id"] == "TOOL-001"))
        case["rubric"] = "ORACLE_SENTINEL"
        case["checks"] = [{"kind": "exact_text", "value": "ORACLE_SENTINEL"}]
        case["fixtures"][0]["result"] = "FIXTURE_NOT_YET_READ"
        messages, tools = core.request_inputs(case, "baseline")
        payload = core.canonical([messages, tools])
        self.assertNotIn("ORACLE_SENTINEL", payload)
        self.assertNotIn("FIXTURE_NOT_YET_READ", payload)
        messages[1]["content"] = "changed"
        self.assertNotEqual(messages[1], case["messages"][0])

    def test_exact_number_does_not_accept_substring_or_explanation(self):
        case = next(c for c in self.cases if c["id"] == "MATH-001")
        for answer in ["153", "The answer is 53", "53 or 54"]:
            self.assertEqual(core.grade(case, answer, [])[0]["status"], "fail")

    def test_json_types_fences_and_extra_fields_are_not_accepted(self):
        case = next(c for c in self.cases if c["id"] == "FMT-002")
        for text in ['{"ready":1,"count":3}', '```json\n{"ready":true,"count":3}\n```',
                     '{"ready":true,"count":3,"extra":1}']:
            self.assertEqual(core.grade(case, text, [])[0]["status"], "fail")
        self.assertEqual(core.grade(case, '{"count":3,"ready":true}', [])[0]["status"], "pass")

    def test_wrong_arguments_and_duplicate_calls_fail_even_with_right_name(self):
        case = next(c for c in self.cases if c["id"] == "TOOL-001")
        for calls in [[{"name": "lookup_contact", "arguments": {"name": "Other Person"}}],
                      [{"name": "lookup_contact", "arguments": {"name": "Mira Chen"}}] * 2]:
            self.assertEqual(core.grade(case, "+1-202-555-0138", calls)[0]["status"], "fail")

    def test_wrong_order_and_missing_second_source_fail(self):
        case = next(c for c in self.cases if c["id"] == "CHAIN-001")
        expected = case["checks"][0]["value"]
        self.assertEqual(core.grade(case, "BETA", expected)[0]["status"], "pass")
        self.assertEqual(core.grade(case, "BETA", expected[:1])[0]["status"], "fail")
        self.assertEqual(core.grade(case, "BETA", list(reversed(expected)))[0]["status"], "fail")

    def test_invalid_corpus_or_scorer_is_rejected(self):
        for check in [{"kind": "contains", "value": "53"}, {"kind": "word_count", "value": True},
                      {"kind": "manual", "value": "pass"}]:
            with self.assertRaises(ValueError):
                core.validate_check(check)
        data = json.loads(core.CORPUS.read_text())
        data["cases"].append(data["cases"][0])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                core.load_cases(path)

    def test_unknown_selection_and_empty_intersection_rejected(self):
        for ids, categories in [("missing", ""), ("", "missing"), ("MATH-001", "format")]:
            with self.assertRaises(ValueError):
                select(self.cases, ids, categories, 0)

    def test_response_identity_and_protocol_fail_closed(self):
        candidates = [response(model="other"), {"model": "offline-fixture", "choices": []},
                      response(calls=[tool_call()], finish="length"),
                      response(calls=[tool_call(), tool_call()]),
                      response(finish="error")]
        array_call = tool_call()
        array_call["function"]["arguments"] = "[]"
        candidates.append(response(calls=[array_call]))
        for data in candidates:
            with self.assertRaises(ValueError):
                core.decode_message(data, "offline-fixture")

    def test_incomplete_and_replay_reports_cannot_imply_ling_pass(self):
        meta = {"mode": "replay", "corpus_sha256": "test", "state": "incomplete", "expected_rows": 44}
        report = core.markdown_report([], meta)
        self.assertIn("INCOMPLETE", report)
        self.assertIn("no model inference occurred", report)
        self.assertIn("Executed 0 of 44", report)


class RunnerChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cases = {c["id"]: c for c in core.load_cases()}

    async def run_record(self, ident, responses, **kwargs):
        return await core.run_case(self.cases[ident], core.ReplayTransport(responses),
                                   model="offline-fixture", mode="replay", **kwargs)

    async def test_known_answer_and_fixture_round_trip(self):
        math = await self.run_record("MATH-001", [response()])
        self.assertEqual(math["status"], "contract_pass")
        record = await self.run_record("TOOL-001", [response(None, [tool_call()]), response("+1-202-555-0138")])
        self.assertEqual(record["status"], "contract_pass")
        self.assertEqual(record["request_count"], 2)

    async def test_replay_is_not_live_and_manual_is_never_pass(self):
        record = await self.run_record("UNKNOWN-001", [response("I do not have account data.")])
        self.assertEqual(record["mode"], "replay")
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["quality_review"], "pending")

    async def test_right_tool_wrong_answer_fails(self):
        r = await self.run_record("TOOL-001", [response(None, [tool_call()]), response("+1-202-555-9999")])
        self.assertEqual(r["status"], "fail")
        self.assertIn("answer_contract", r["failure_stages"])

    async def test_unknown_tool_is_never_dispatched(self):
        r = await self.run_record("TOOL-001", [response(None, [tool_call("send_email")])])
        self.assertEqual(r["request_count"], 1)
        self.assertIn("tool_selection", r["failure_stages"])

    async def test_wrong_argument_is_never_dispatched(self):
        r = await self.run_record("TOOL-001", [response(None, [tool_call(arguments={"name": "Other"})])])
        self.assertEqual(r["request_count"], 1)
        self.assertIn("tool_arguments", r["failure_stages"])

    async def test_fixture_is_delivered_only_after_matching_call(self):
        seen = []
        async def transport(**kw):
            seen.append(kw)
            return response(None, [tool_call()]) if len(seen) == 1 else response("+1-202-555-0138")
        await core.run_case(self.cases["TOOL-001"], transport, model="offline-fixture", mode="replay")
        self.assertFalse(any(m["role"] == "tool" for m in seen[0]["messages"]))
        self.assertIn("+1-202-555-0138", seen[1]["messages"][-1]["content"])

    async def test_failed_action_cannot_be_scored_success_from_tool_name(self):
        call = tool_call("add_reminder", {"title": "Cedar review", "when_iso": "2026-10-23T10:00:00-07:00"})
        r = await self.run_record("CHAIN-003", [response(None, [call]), response("Created your reminder.")])
        self.assertEqual(r["status"], "fail")
        self.assertIn("answer_contract", r["failure_stages"])

    async def test_step_budget_and_truncation_are_failures(self):
        r = await self.run_record("TOOL-001", [response(None, [tool_call()])] * 3)
        self.assertIn("step_limit", r["failure_stages"])
        truncated = await self.run_record("MATH-001", [response("53", finish="length")])
        self.assertEqual(truncated["status"], "fail")
        self.assertIn("generation_limit", truncated["failure_stages"])

    async def test_exception_is_redacted_and_unequal_model_fails(self):
        async def bad(**kwargs):
            raise RuntimeError("PRIVATE_AUTH_TOKEN_SENTINEL")
        r = await core.run_case(self.cases["MATH-001"], bad, model="offline-fixture", mode="replay")
        self.assertNotIn("PRIVATE_AUTH_TOKEN_SENTINEL", core.canonical(r))
        self.assertEqual(r["error_type"], "RuntimeError")
        mismatch = await self.run_record("MATH-001", [response(model="other")])
        self.assertEqual(mismatch["status"], "fail")

    async def test_timeout_is_a_failure(self):
        async def slow(**kwargs):
            await asyncio.sleep(2)
        r = await core.run_case(self.cases["MATH-001"], slow, model="offline-fixture", mode="replay", timeout=1)
        self.assertEqual(r["error_type"], "TimeoutError")
        self.assertIn("inference", r["failure_stages"])

    async def test_pairs_and_instability_remain_separate_by_model_and_mode(self):
        good = await self.run_record("MATH-001", [response()])
        bad = await self.run_record("MATH-001", [response("54")], repetition=2)
        grounded = await self.run_record("MATH-001", [response("54")], variant="grounded")
        summary = core.summarize([good, bad, grounded])
        self.assertEqual(len(summary["unstable_cases"]), 1)
        self.assertEqual(len(summary["paired_variants"]), 1)
        self.assertTrue(summary["paired_variants"][0]["changed"])

    async def test_no_client_load_unload_or_chat_when_model_not_resident(self):
        class Client:
            async def loaded_models(self):
                return []
            async def chat(self, **kw):
                raise AssertionError("Must not generate for nonresident model")
        t = live.LocalTransport(Client(), "Ling-Test")
        with self.assertRaises(RuntimeError):
            await t(model="Ling-Test")


class IsolationChecks(unittest.TestCase):
    def test_unconfined_home_is_rejected_before_service_import(self):
        with tempfile.TemporaryDirectory() as d:
            original = Path(d) / "real"
            scratch = Path(d) / "scratch"
            original.mkdir()
            (scratch / "state").mkdir(parents=True)
            with patch.object(live.sys, "platform", "darwin"), patch.dict(os.environ, {
                    "HOME": str(scratch), "WISP_HOME": str(scratch / "state")}):
                with self.assertRaisesRegex(ValueError, "does not deny"):
                    live.require_isolation(original)

    def test_non_permission_network_error_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as d:
            original = Path(d) / "real"
            scratch = Path(d) / "scratch"
            original.mkdir()
            (scratch / "state").mkdir(parents=True)
            with patch.object(live.sys, "platform", "darwin"), patch.dict(os.environ, {
                    "HOME": str(scratch), "WISP_HOME": str(scratch / "state")}), \
                    patch.object(live.os, "open", side_effect=PermissionError(errno.EPERM, "denied")), \
                    patch.object(live.socket, "socket") as factory:
                factory.return_value.__enter__.return_value.connect.side_effect = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
                with self.assertRaisesRegex(ValueError, "inconclusive"):
                    live.require_isolation(original)

    def test_offline_cli_in_fresh_process_never_imports_service(self):
        with tempfile.TemporaryDirectory() as d:
            code = "from scripts.ling_diagnostics.__main__ import main; import sys; assert main(['validate']) == 0; assert not any(n == 'service' or n.startswith('service.') for n in sys.modules)"
            env = dict(os.environ, HOME=d, WISP_HOME=d, PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run([sys.executable, "-B", "-c", code], cwd=core.ROOT, env=env,
                                  capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_cli_demo_and_incomplete_recording_preserve_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "demo"
            args = [sys.executable, "-B", "-m", "scripts.ling_diagnostics", "replay", "--ids",
                    "FMT-001,MATH-001,TOOL-001", "--responses",
                    str(core.CORPUS.parent / "offline-demo.json"), "--out", str(out)]
            env = dict(os.environ, HOME=d, WISP_HOME=d, PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run(args, cwd=core.ROOT, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads((out / "manifest.json").read_text())["state"], "complete")
            self.assertIn("not Ling scores", (out / "report.md").read_text())
            # A second attempt cannot replace the earlier result directory.
            proc = subprocess.run(args, cwd=core.ROOT, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(len((out / "results.jsonl").read_text().splitlines()), 3)
            partial_args = list(args)
            partial_args[partial_args.index("FMT-001,MATH-001,TOOL-001")] += ",ATTR-001"
            partial = Path(d) / "partial"
            partial_args[-1] = str(partial)
            proc = subprocess.run(partial_args, cwd=core.ROOT, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(json.loads((partial / "manifest.json").read_text())["state"], "incomplete")
            self.assertIn("INCOMPLETE", (partial / "report.md").read_text())
            self.assertEqual(len((partial / "results.jsonl").read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
