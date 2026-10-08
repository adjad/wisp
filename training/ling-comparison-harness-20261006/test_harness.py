"""Offline negative controls: no model imports, downloads, credentials or real sockets."""
import copy
import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backends
import runner
from scoring import canonical, schema_errors, score, semantic_errors, strict_json

ROOT = Path(__file__).resolve().parent
SCHEMA = json.loads((ROOT / "intent.schema.v1.json").read_text())
GOLD = {"version": 1, "kind": "read", "sources": [{"domain": "calendar", "operation": "overview", "time": {"named": "this week"}}], "excluded_sources": ["email"], "unsupported_constraints": []}


class ScoreTests(unittest.TestCase):
    def test_strict_json_duplicates_fences_and_nonfinite(self):
        for text in ('{"a":1,"a":2}', '```json\n{}\n```', '{"a":NaN}', '{} {}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                strict_json(text)

    def test_bool_is_not_integer(self):
        altered = {**GOLD, "version": True}
        self.assertTrue(schema_errors(altered, SCHEMA))
        self.assertFalse(score(canonical(altered), GOLD, SCHEMA)["metrics"]["schema_valid"])

    def test_valid_json_is_not_correct_routing(self):
        altered = copy.deepcopy(GOLD)
        altered["sources"][0]["time"] = {"named": "next week"}
        metrics = score(canonical(altered), GOLD, SCHEMA)["metrics"]
        self.assertTrue(metrics["schema_valid"])
        self.assertTrue(metrics["semantic_valid"])
        self.assertTrue(metrics["source_match"])
        self.assertFalse(metrics["arguments_match"])
        self.assertFalse(metrics["exact"])

    def test_dates_and_capability_semantics_not_just_schema(self):
        bad_times = ({"date": "2026-02-30"}, {"month": "2026-13"}, {"start": "2026-10-08", "end": "2026-10-06"}, {"named": "today", "date": "2026-10-06"}, {"start": "2026-10-06"}, {})
        for bad in bad_times:
            altered = copy.deepcopy(GOLD)
            altered["sources"][0]["time"] = bad
            self.assertFalse(schema_errors(altered, SCHEMA))
            self.assertTrue(semantic_errors(altered))
        altered = copy.deepcopy(GOLD)
        altered["sources"] = [{"domain": "messages", "operation": "overview", "unread": True}]
        self.assertFalse(schema_errors(altered, SCHEMA))
        self.assertTrue(semantic_errors(altered))

    def test_excluded_and_unexpected_source_intents(self):
        altered = copy.deepcopy(GOLD)
        altered["sources"].append({"domain": "email", "operation": "overview"})
        result = score(canonical(altered), GOLD, SCHEMA)
        self.assertTrue(result["unexpected_source_intent"])
        self.assertTrue(result["excluded_source_intent"])
        self.assertFalse(result["metrics"]["semantic_valid"])

    def test_permutations_separate_from_exact_and_duplicates(self):
        gold = copy.deepcopy(GOLD)
        gold["sources"].append({"domain": "reminders", "operation": "overview"})
        altered = copy.deepcopy(gold)
        altered["sources"].reverse()
        result = score(canonical(altered), gold, SCHEMA)
        self.assertFalse(result["metrics"]["exact"])
        self.assertTrue(result["metrics"]["normalized_exact"])
        altered["sources"].append(altered["sources"][0])
        self.assertFalse(score(canonical(altered), gold, SCHEMA)["metrics"]["normalized_exact"])

    def test_truncated_valid_json_cannot_earn_exact_credit(self):
        result = score(canonical(GOLD), GOLD, SCHEMA, usable=False)
        self.assertTrue(result["metrics"]["schema_valid"])
        self.assertFalse(result["metrics"]["exact"])

    def test_nonread_requires_empty_sources(self):
        altered = {**GOLD, "kind": "inline"}
        self.assertTrue(semantic_errors(altered))


class ProvenanceTests(unittest.TestCase):
    def probe_fixture(self, folder, source="VALUE = 'selected'\n"):
        path = Path(folder)
        pins = {k: 'test' for k in ("model_id", "model_revision", "tokenizer_revision", "chat_template_sha256", "axolotl_revision")}
        (path / "pins.json").write_text(json.dumps(pins))
        if source is not None:
            (path / "cloud_probe.py").write_text(source)
        return {"backend": "hf_probe", "probe_dir": folder}

    def test_probe_requires_entrypoint_without_ambient_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.probe_fixture(folder, None)
            with patch.dict(sys.modules, {"cloud_probe": object(), "torch": None}):
                with self.assertRaisesRegex(ValueError, "canonical cloud_probe"):
                    backends.describe(config)

    def test_probe_loads_exact_inert_source_and_records_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            descriptor = backends.describe(self.probe_fixture(folder))
            with patch.dict(sys.modules, {"torch": None}):
                probe, actual = backends.load_probe_source(descriptor)
            path = str((Path(folder) / "cloud_probe.py").resolve())
            self.assertEqual(probe.VALUE, 'selected')
            self.assertEqual(probe.__file__, path)
            self.assertEqual(actual["path"], path)
            self.assertEqual(actual["sha256"], descriptor["probe_entrypoint"]["sha256"])
            self.assertEqual(actual["source_bytes"], (Path(folder) / "cloud_probe.py").stat().st_size)

    def test_probe_ignores_unrelated_search_path_and_cached_module(self):
        with tempfile.TemporaryDirectory() as selected, tempfile.TemporaryDirectory() as ambient:
            descriptor = backends.describe(self.probe_fixture(selected))
            (Path(ambient) / "cloud_probe.py").write_text("raise AssertionError('ambient source executed')")
            cached = object()
            with patch.object(sys, 'path', [ambient, *sys.path]), patch.dict(sys.modules, {"cloud_probe": cached, "torch": None}):
                before = list(sys.path)
                probe, actual = backends.load_probe_source(descriptor)
                self.assertIs(sys.modules['cloud_probe'], cached)
                self.assertEqual(sys.path, before)
            self.assertEqual(probe.VALUE, 'selected')
            self.assertEqual(actual["path"], descriptor["probe_entrypoint"]["path"])

    def test_probe_rejects_changed_source_and_mismatched_descriptor(self):
        with tempfile.TemporaryDirectory() as folder:
            descriptor = backends.describe(self.probe_fixture(folder))
            altered = copy.deepcopy(descriptor)
            altered['probe_entrypoint']['path'] = str(Path(folder) / 'other.py')
            with self.assertRaisesRegex(ValueError, "origin"):
                backends.load_probe_source(altered)
            (Path(folder) / 'cloud_probe.py').write_text("raise AssertionError('changed source executed')")
            with self.assertRaisesRegex(ValueError, "source changed"):
                backends.load_probe_source(descriptor)

    def test_probe_rejects_loaded_origin_reassignment(self):
        with tempfile.TemporaryDirectory() as folder:
            descriptor = backends.describe(self.probe_fixture(folder, "__file__ = 'unrelated.py'\n"))
            with self.assertRaisesRegex(ValueError, "loaded probe origin"):
                backends.load_probe_source(descriptor)

    def test_real_request_never_receives_gold_or_rubric(self):
        cases, schema = runner.load_cases(ROOT / "cases.jsonl")
        for row in cases:
            request = runner.request_for(row, schema, 1)
            self.assertNotIn("expected", request)
            self.assertNotIn("mock_expected", request)
            self.assertNotIn("rubric", request)
            self.assertEqual(request["messages"][1:], row["messages"])

    def test_journal_integrity_duplicate_partial_and_request_change(self):
        manifest = {"fingerprint": "abc", "requests": {"R001": "prompt"}, "descriptor": {"backend": "mock"}}
        receipt = {"id": "R001", "fingerprint": "abc", "request_sha256": "prompt", "status": "error"}
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            file = folder / "raw.jsonl"
            runtime = folder / 'runtime-0.json'
            runner.dump(runtime, runner.seal_runtime({'status': 'ready', 'runtime': {'backend': 'mock'}, 'startup_s': 0}, 'abc'))
            receipt.update(runtime_receipt='runtime-0.json', runtime_sha256=backends.sha(runtime))
            runner.append_receipt(file, receipt)
            self.assertEqual(runner.read_receipts(folder, manifest), [receipt])
            with self.assertRaises(ValueError):
                runner.read_receipts(folder, {**manifest, "requests": {"R001": "changed"}})
            runner.append_receipt(file, receipt)
            with self.assertRaises(ValueError):
                runner.read_receipts(folder, manifest)
            file.write_text('{"partial":')
            with self.assertRaises(ValueError):
                runner.read_receipts(folder, manifest)
            file.write_text(canonical({**receipt, "receipt_sha256": "wrong"}) + '\n')
            with self.assertRaises(ValueError):
                runner.read_receipts(folder, manifest)

    def test_endpoint_identity_and_no_network_during_description(self):
        for value in ("https://evil.test/v1", "http://localhost/v1", "http://127.0.0.1:8000/v1?token=secret", "http://user:pass@127.0.0.1/v1", "http://127.0.0.1/v2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                backends.endpoint_url(value)
        with patch.dict('sys.modules', {"torch": None, "urllib.request": None}):
            d = backends.describe({"backend": "http", "endpoint": "http://127.0.0.1:8000/v1", "model": "test"})
            backend = backends.HTTPBackend(d)
            self.assertIn("not verified", backend.runtime["binding"])
            backends.describe({"backend": "mock"})
        with self.assertRaises(ValueError):
            backends.describe({"backend": "http", "endpoint": "http://127.0.0.1/v1", "model": "test", "token": "not allowed"})

    def test_missing_loader_pins_and_adapter_are_rejected_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            (p / "cloud_probe.py").write_text("raise Exception('must not import')")
            (p / "pins.json").write_text('{}')
            with self.assertRaises(ValueError):
                backends.describe({"backend": "hf_probe", "probe_dir": folder})
            pins = {k: 'test' for k in ("model_id", "model_revision", "tokenizer_revision", "chat_template_sha256", "axolotl_revision")}
            (p / "pins.json").write_text(json.dumps(pins))
            descriptor = backends.describe({"backend": "hf_probe", "probe_dir": folder})
            self.assertEqual(descriptor["adapter"], None)
            with self.assertRaises(ValueError):
                backends.describe({"backend": "hf_probe", "probe_dir": folder, "adapter": folder})

    def test_failure_denominator_includes_unrecorded_cases(self):
        cases = [{"id": 'a', "lane": "routing"}, {"id": 'b', "lane": "routing"}]
        manifest = {"case_ids": ['a', 'b'], "descriptor": {"simulation": True}}
        receipt = {"id": 'a', "lane": "routing", "family": 'week', "status": "error", "first_request_after_load": True, "score": score('', GOLD, SCHEMA, False)}
        summary = runner.summarize([receipt], manifest, cases)
        self.assertEqual(summary["routing_denominator"], 2)
        self.assertEqual(summary["failed_or_incomplete"], 1)
        self.assertEqual(summary["unrecorded"], 1)
        self.assertIsNone(summary["warm_p50_s"])

    def test_lock_blocks_overlapping_arms_in_same_output_root(self):
        with tempfile.TemporaryDirectory() as folder:
            with runner.window(Path(folder)):
                with self.assertRaises(ValueError):
                    with runner.window(Path(folder)):
                        self.fail('must not overlap')

    def test_timeout_and_own_worker_cleanup_use_inert_objects(self):
        class Connection:
            def poll(self, timeout):
                return False
            def send(self, value):
                pass
            def close(self):
                self.closed = True
        class Process:
            def __init__(self):
                self.alive = True
                self.terminated = False
                self.killed = False
            def join(self, timeout):
                pass
            def is_alive(self):
                return self.alive
            def terminate(self):
                self.terminated = True
            def kill(self):
                self.killed = True
                self.alive = False
        connection, process = Connection(), Process()
        self.assertEqual(runner.receive(connection, .001)["status"], 'timeout')
        runner.cleanup(process, connection)
        self.assertTrue(process.terminated and process.killed and connection.closed)

    def test_full_corpus_count_and_truthful_origins(self):
        cases, _ = runner.load_cases(ROOT / 'cases.jsonl')
        self.assertEqual(len(cases), 100)
        self.assertEqual(sum(c['lane'] == 'routing' for c in cases), 80)
        quoted = [c for c in cases if c['origin'] == 'user_phrase']
        self.assertEqual(len(quoted), 1)
        self.assertEqual(quoted[0]['messages'][-1]['content'], 'what is up for the week')



class HTTPTransportTests(unittest.TestCase):
    def response(self, payload):
        class InertResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return json.dumps(payload).encode()
        return InertResponse()

    def test_http_stub_identity_tool_and_completion_failures(self):
        from unittest.mock import MagicMock
        backend = backends.HTTPBackend(backends.describe({"backend": "http", "endpoint": "http://127.0.0.1:8000/v1", "model": "synthetic-model"}))
        request = {"messages": [{"role": "user", "content": "synthetic only"}], "max_tokens": 10, "timeout_s": 1}
        good = {"model": "synthetic-model", "choices": [{"message": {"content": '{}'}, "finish_reason": "stop"}], "usage": {"completion_tokens": 1}}
        opener = MagicMock()
        opener.open.return_value = self.response(good)
        with patch('urllib.request.build_opener', return_value=opener):
            result = backend.generate(request)
            self.assertEqual(result['text'], '{}')
            self.assertIsNone(result['peak_allocated_bytes'])
            body = json.loads(opener.open.call_args[0][0].data)
            self.assertNotIn('tools', body)
            self.assertFalse(body['chat_template_kwargs']['enable_thinking'])
            for bad in ({**good, 'model': 'wrong'}, {**good, 'choices': []}, {**good, 'choices': [{'message': {'content': '{}', 'tool_calls': [{}]}}]}):
                opener.open.return_value = self.response(bad)
                with self.assertRaises(ValueError):
                    backend.generate(request)

    def test_transport_error_is_not_retried(self):
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.side_effect = TimeoutError('inert stub timeout')
        backend = backends.HTTPBackend(backends.describe({"backend": "http", "endpoint": "http://127.0.0.1:8000/v1", "model": "synthetic-model"}))
        request = {"messages": [{"role": "user", "content": "synthetic"}], "max_tokens": 10, "timeout_s": 1}
        with patch('urllib.request.build_opener', return_value=opener), self.assertRaises(TimeoutError):
            backend.generate(request)
        self.assertEqual(opener.open.call_count, 1)



class RuntimeBindingTests(unittest.TestCase):
    def test_runtime_missing_changed_or_nonready_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            manifest = {'fingerprint': 'abc', 'descriptor': {'backend': 'mock'}}
            path = folder / 'runtime-0.json'
            payload = {'status': 'ready', 'runtime': {'backend': 'mock'}, 'startup_s': 0}
            runner.dump(path, runner.seal_runtime(payload, 'abc'))
            checksum = backends.sha(path)
            self.assertEqual(runner.read_runtime(folder, path.name, manifest, checksum, True)['status'], 'ready')
            runner.dump(path, runner.seal_runtime({**payload, 'startup_s': 99}, 'abc'))
            with self.assertRaises(ValueError):
                runner.read_runtime(folder, path.name, manifest, checksum, True)
            runner.dump(path, runner.seal_runtime({'status': 'error', 'error_type': 'SyntheticFailure'}, 'abc'))
            with self.assertRaises(ValueError):
                runner.read_runtime(folder, path.name, manifest, backends.sha(path), True)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                runner.read_runtime(folder, path.name, manifest, checksum, True)

    def test_startup_failed_arms_without_journals_report_as_partial(self):
        import argparse
        import contextlib
        import io
        cases_path = ROOT / 'cases.jsonl'
        cases, _ = runner.load_cases(cases_path)
        descriptor = backends.describe({'backend': 'mock'})
        identity = {'format': 1, 'hashes': runner.source_hashes(cases_path), 'descriptor': descriptor,
                    'policy': runner.POLICY, 'lane': 'all', 'case_ids': [r['id'] for r in cases],
                    'timeout_s': 1, 'startup_timeout_s': 1}
        fingerprint = runner.digest(identity)
        manifest = {**identity, 'fingerprint': fingerprint, 'requests': {}, 'tools_executed': False}
        with tempfile.TemporaryDirectory() as output:
            output = Path(output)
            for arm in ('base', 'tuned'):
                folder = output / arm
                folder.mkdir()
                runner.dump(folder / 'manifest.json', manifest)
                runner.dump(folder / 'runtime-0.json', runner.seal_runtime({'status': 'error', 'error_type': 'SyntheticLoadFailure'}, fingerprint))
            with contextlib.redirect_stdout(io.StringIO()):
                runner.report(argparse.Namespace(cases=cases_path, output=output, left='base', right='tuned'))
            comparison = json.loads((output / 'comparison.json').read_text())
            self.assertEqual(comparison['paired_routes'], 0)
            self.assertEqual(comparison['arms']['base']['unrecorded'], 100)
            self.assertEqual(comparison['arms']['base']['routing_denominator'], 80)
            self.assertTrue(list(output.glob('overview-review-template-*.json')))

    def test_worker_marker_blocks_resume_before_backend_start(self):
        import argparse
        with tempfile.TemporaryDirectory() as output:
            output = Path(output)
            config = output / 'config.json'
            config.write_text('{"backend":"mock"}')
            cases_path = ROOT / 'cases.jsonl'
            rows, schema = runner.load_cases(cases_path)
            descriptor = backends.describe({'backend': 'mock'})
            identity = {'format': 1, 'hashes': runner.source_hashes(cases_path), 'descriptor': descriptor,
                        'policy': runner.POLICY, 'lane': 'all', 'case_ids': [r['id'] for r in rows],
                        'timeout_s': 1, 'startup_timeout_s': 1}
            manifest = {**identity, 'fingerprint': runner.digest(identity),
                        'requests': {r['id']: runner.digest(runner.request_for(r, schema, 1, True)) for r in rows}}
            folder = output / 'base'
            folder.mkdir()
            runner.dump(folder / 'manifest.json', manifest)
            runner.dump(folder / 'worker-active.json', {'phase': 'launching'})
            args = argparse.Namespace(cases=cases_path, lane='all', limit=None, config=config, output=output,
                                      arm='base', resume=True, timeout=1, startup_timeout=1)
            with patch('multiprocessing.get_context', side_effect=AssertionError('must not start a worker')):
                with self.assertRaisesRegex(ValueError, 'unclosed request'):
                    runner.run(args)



class OutputWindowTests(unittest.TestCase):
    def test_orphan_base_marker_blocks_new_tuned_arm_without_spawn(self):
        import argparse
        with tempfile.TemporaryDirectory() as output:
            output = Path(output)
            base = output / 'base'
            base.mkdir()
            runner.dump(base / 'worker-active.json', {'phase': 'launching'})
            config = output / 'config.json'
            config.write_text('{"backend":"mock"}')
            args = argparse.Namespace(cases=ROOT / 'cases.jsonl', lane='all', limit=None, config=config,
                                      output=output, arm='tuned', resume=False, timeout=1, startup_timeout=1)
            with patch('multiprocessing.get_context', side_effect=AssertionError('must not start worker')):
                with self.assertRaisesRegex(ValueError, 'unclosed request in output root'):
                    runner.run(args)
            self.assertFalse((output / 'tuned').exists())

    def test_remote_unknown_old_arm_blocks_different_new_arm(self):
        with tempfile.TemporaryDirectory() as output:
            output = Path(output)
            base = output / 'base'
            base.mkdir()
            manifest = {'fingerprint': 'abc', 'requests': {'R001': 'prompt'}, 'descriptor': {'backend': 'http'}}
            runner.dump(base / 'manifest.json', manifest)
            runtime = base / 'runtime-0.json'
            runner.dump(runtime, runner.seal_runtime({'status': 'ready', 'runtime': {'backend': 'http'}, 'startup_s': 0}, 'abc'))
            runner.append_receipt(base / 'raw.jsonl', {'id': 'R001', 'fingerprint': 'abc', 'request_sha256': 'prompt',
                'runtime_receipt': runtime.name, 'runtime_sha256': backends.sha(runtime), 'status': 'timeout', 'remote_completion_unknown': True})
            with self.assertRaisesRegex(ValueError, 'remote completion unknown in output root'):
                runner.assert_quiescent(output)


if __name__ == '__main__':
    unittest.main()
