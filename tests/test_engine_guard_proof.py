"""Retained memory-guard evidence with fake limits, clocks and enforcers only.

The module under test is loaded by file path: these checks never import the
service, oMLX, MLX, a model, or a hardware/process/network probe. Actual asyncio
tasks below exercise task lifecycle semantics without starting external work.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROOF_PATH = Path(__file__).resolve().parents[1] / "build-support/engine_guard/proof.py"
FIELDS = {
    "version", "clock_domain", "pid", "guard_id", "tier", "prefill_guard",
    "running", "task_state", "iteration_state", "iteration_seq", "iteration_ns",
    "sample_seq", "sample_ns", "sample_source", "static_bytes", "dynamic_bytes",
    "metal_bytes", "cap_bytes", "poll_interval_ms", "observed_ns", "qualified",
    "reason", "admission_seq", "admission_ns", "admission_sample_seq",
    "admission_cap_bytes",
}
STRING_FIELDS = {"clock_domain", "guard_id", "tier", "task_state",
                 "iteration_state", "sample_source", "reason"}
BOOL_FIELDS = {"prefill_guard", "running", "qualified"}
CANARY = "PRIVATE-DIAGNOSTIC-MUST-NOT-APPEAR"


def breakdown(static=120, dynamic=80, metal_cap=100):
    candidates = [static, dynamic] + ([metal_cap] if metal_cap else [])
    return dict(static=static, dynamic=dynamic, metal_cap=metal_cap,
                hard_limit=min(candidates))


class RunningTask:
    def cancelled(self):
        return False

    def cancelling(self):
        return 0

    def done(self):
        return False

    def exception(self):
        raise AssertionError("A running task's exception must not be inspected")


def fake_enforcer(proof):
    class Enforcer:
        def __init__(self):
            self._memory_guard_tier = "balanced"
            self._prefill_memory_guard = True
            self._running = True
            self._current_poll_interval = 1.0
            self._task = RunningTask()
            self.next_breakdown = breakdown()
            self.source = "host_statistics64"
            self.preview = None
            self.preview_source = "available_memory"
            self.calls = []
            self.delivered = None
            self.propagation_error = False
            self.unresolved_scheduler = False

        @proof.sampled
        def calculate(self):
            self.calls.append("sample")
            proof.note_source(self, self.source)
            return copy.deepcopy(self.next_breakdown)

        @proof.propagating
        def _propagate_memory_limit(self):
            self.calls.append("propagate")
            self.delivered = self.calculate()
            if self.unresolved_scheduler:
                proof.unresolved(self)
            if self.preview is not None:
                self.next_breakdown = self.preview
                self.source = self.preview_source
                self.calculate()
            if self.propagation_error:
                raise RuntimeError(CANARY)

        def load_model(self, *args, **kwargs):
            raise AssertionError("Evidence admission must not load models")

        def warmup(self, *args, **kwargs):
            raise AssertionError("Evidence admission must not warm a model")

        def start(self, *args, **kwargs):
            raise AssertionError("Evidence admission must not restart the guard")

    return Enforcer()


class GuardProofTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("wisp_guard_proof_fixture", PROOF_PATH)
        self.proof = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.proof)
        self.now = 10_000_000_000
        ids = itertools.count(1)
        self.enterContext(patch.multiple(
            self.proof, _now_ns=lambda: self.now, _pid=lambda: 4173,
            _clock_supported=lambda: True))
        self.enterContext(patch.object(self.proof.uuid, "uuid4",
                                      side_effect=lambda: SimpleNamespace(hex=f"{next(ids):032x}")))
        self.guard = fake_enforcer(self.proof)

    def healthy(self):
        self.proof.begin_iteration(self.guard)
        self.guard._propagate_memory_limit()
        self.proof.complete_iteration(self.guard)
        row = self.proof.snapshot(self.guard)
        self.assertTrue(row["qualified"], row)
        return row

    def assert_refused(self, **kwargs):
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            self.proof.require_admission(self.guard, **kwargs)

    def test_snapshot_is_retained_applied_row_not_latest_preview(self):
        self.guard.preview = breakdown(900, 600, 800)
        first = self.healthy()
        self.assertEqual(self.guard.delivered, breakdown())
        self.assertEqual((first["cap_bytes"], first["sample_source"], first["sample_seq"]),
                         (80, "host_statistics64", 1))
        before = list(self.guard.calls)
        self.assertEqual(self.proof.snapshot(self.guard), first)
        self.assertEqual(self.guard.calls, before, "Status must not recalculate limits")

    def test_new_preview_cannot_renew_old_applied_sample(self):
        first = self.healthy()
        self.now += 3_000_000_001
        self.guard.next_breakdown = breakdown(60, 40, 50)
        self.guard.calculate()
        row = self.proof.snapshot(self.guard)
        self.assertEqual(row["sample_ns"], first["sample_ns"])
        self.assertEqual(row["cap_bytes"], 80)
        self.assertEqual(row["reason"], "stale")
        self.assert_refused()

    def test_calculation_without_propagation_cannot_qualify(self):
        self.proof.begin_iteration(self.guard)
        self.guard.calculate()
        self.proof.complete_iteration(self.guard)
        self.assertEqual(self.proof.snapshot(self.guard)["reason"], "unpropagated")
        self.assert_refused()

    def test_manual_propagation_requires_matching_complete_breakdown(self):
        self.proof.begin_iteration(self.guard)
        self.proof.begin_sample(self.guard)
        self.proof.note_source(self.guard, "host_statistics64")
        applied = breakdown()
        self.assertIs(self.proof.record_breakdown(self.guard, applied), applied)
        self.proof.propagated(self.guard, dict(applied, hard_limit=79))
        self.proof.complete_iteration(self.guard)
        self.assertEqual(self.proof.snapshot(self.guard)["reason"], "unpropagated")
        self.proof.propagated(self.guard, applied)
        self.assertTrue(self.proof.snapshot(self.guard)["qualified"])

    def test_missing_guard_and_no_completed_initial_iteration_refuse(self):
        self.assertFalse(self.proof.snapshot(None)["qualified"])
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            self.proof.require_admission(None)
        self.assert_refused()
        self.guard._propagate_memory_limit()
        self.assert_refused()
        self.assertEqual(self.proof.snapshot(self.guard)["reason"], "iteration_pending")
        self.assertEqual(self.guard.calls, ["propagate", "sample"])

    def test_pending_or_failed_iteration_invalidates_earlier_success(self):
        self.healthy()
        self.proof.begin_iteration(self.guard)
        self.assertEqual(self.proof.snapshot(self.guard)["reason"], "iteration_pending")
        before = list(self.guard.calls)
        self.assert_refused()
        self.proof.fail_iteration(self.guard)
        self.assertEqual(self.proof.snapshot(self.guard)["reason"], "iteration_failed")
        self.assert_refused()
        self.assertEqual(self.guard.calls, before, "No admission refresh/retry before a healthy iteration")

    def test_propagation_exception_withdraws_prior_applied_evidence(self):
        self.healthy()
        self.guard.propagation_error = True
        with self.assertRaisesRegex(RuntimeError, CANARY):
            self.guard._propagate_memory_limit()
        row = self.proof.snapshot(self.guard)
        self.assertFalse(row["qualified"])
        self.assertEqual(row["reason"], "unpropagated")
        self.assertNotIn(CANARY, json.dumps(row))

    def test_fallback_sources_never_certify_dynamic_guard(self):
        for source in ("available_memory", "static_fallback", "unknown", CANARY):
            with self.subTest(source=source):
                self.proof.reset(self.guard)
                self.guard.source = source
                self.proof.begin_iteration(self.guard)
                self.guard._propagate_memory_limit()
                self.proof.complete_iteration(self.guard)
                row = self.proof.snapshot(self.guard)
                self.assertEqual(row["reason"], "sample_unavailable")
                self.assertFalse(row["qualified"])
                self.assertNotIn(CANARY, json.dumps(row))
                self.assert_refused()

    def test_static_and_metal_caps_are_part_of_coherent_minimum(self):
        for values, cap in ((breakdown(40, 80, 100), 40),
                            (breakdown(120, 80, 30), 30),
                            (breakdown(120, 80, 0), 80)):
            with self.subTest(values=values):
                self.guard.next_breakdown = values
                self.assertEqual(self.healthy()["cap_bytes"], cap)
        for values in (dict(breakdown(), hard_limit=79),
                       dict(breakdown(), dynamic=0), dict(breakdown(), static=0)):
            with self.subTest(values=values):
                self.guard.next_breakdown = values
                self.proof.begin_iteration(self.guard)
                self.guard._propagate_memory_limit()
                self.proof.complete_iteration(self.guard)
                self.assertEqual(self.proof.snapshot(self.guard)["reason"], "cap_invalid")
                self.assert_refused()

    def test_sample_and_iteration_expire_at_declared_poll_tolerance(self):
        for interval in (1, 10, 30):
            with self.subTest(interval=interval):
                self.proof.reset(self.guard)
                self.guard._current_poll_interval = interval
                self.healthy()
                self.now += (interval + 2) * 1_000_000_000
                self.assertTrue(self.proof.snapshot(self.guard)["qualified"])
                self.now += 1
                self.assertEqual(self.proof.snapshot(self.guard)["reason"], "stale")
                self.assert_refused()

    def test_fresh_sample_does_not_cover_stale_iteration_or_future_clock(self):
        self.healthy()
        self.now += 3_000_000_001
        self.guard._propagate_memory_limit()
        self.assertEqual(self.proof.snapshot(self.guard)["reason"], "stale")
        self.healthy()
        self.now -= 1
        self.assertEqual(self.proof.snapshot(self.guard)["reason"], "stale")
        self.assert_refused()

    def test_unsupported_clock_and_invalid_poll_intervals_fail_closed(self):
        self.healthy()
        with patch.object(self.proof, "_clock_supported", return_value=False):
            with patch.object(self.proof, "_now_ns", side_effect=AssertionError("No clock probe")):
                self.assertEqual(self.proof.snapshot(self.guard)["reason"], "clock_invalid")
                self.assert_refused()
        for interval in (0, -1, 31, True, "1", float("nan"), float("inf")):
            with self.subTest(interval=interval):
                self.guard._current_poll_interval = interval
                self.assertEqual(self.proof.snapshot(self.guard)["reason"], "stale")
                self.assert_refused()

    def test_disabled_and_custom_profiles_are_reported_without_rewriting_policy(self):
        for tier, prefill, reason in (("balanced", False, "disabled"),
                                      ("custom", True, "custom"), ("custom", False, "disabled")):
            with self.subTest(tier=tier, prefill=prefill):
                self.guard._memory_guard_tier = tier
                self.guard._prefill_memory_guard = prefill
                before = list(self.guard.calls)
                row = self.proof.require_admission(self.guard, required=10**12)
                self.assertFalse(row["qualified"])
                self.assertEqual(row["reason"], reason)
                self.assertEqual((self.guard._memory_guard_tier, self.guard._prefill_memory_guard),
                                 (tier, prefill))
                self.assertEqual(self.guard.calls, before)
                self.assertEqual(row["admission_seq"], 0)

    def test_admission_refreshes_applied_capacity_once_and_records_receipt(self):
        original = self.healthy()
        self.guard.calls.clear()
        row = self.proof.require_admission(self.guard, required=50, current=30)
        self.assertEqual(self.guard.calls, ["propagate", "sample"])
        self.assertTrue(row["qualified"])
        self.assertEqual(row["admission_seq"], 1)
        self.assertEqual(row["admission_ns"], self.now)
        self.assertEqual(row["admission_cap_bytes"], 80)
        self.assertEqual(row["admission_sample_seq"], row["sample_seq"])
        self.assertGreater(row["sample_seq"], original["sample_seq"])
        self.assertEqual(row["iteration_seq"], original["iteration_seq"])

    def test_capacity_shrink_refuses_without_retrying_or_marking_admission(self):
        self.healthy()
        self.guard.calls.clear()
        self.guard.next_breakdown = breakdown(120, 60, 100)
        with self.assertRaises(self.proof.GuardCapacityChanged):
            self.proof.require_admission(self.guard, required=40, current=30)
        self.assertEqual(self.guard.calls, ["propagate", "sample"])
        row = self.proof.snapshot(self.guard)
        self.assertTrue(row["qualified"])
        self.assertEqual(row["cap_bytes"], 60)
        self.assertEqual(row["admission_seq"], 0)

    def test_fallback_during_admission_refresh_cannot_reuse_prior_health(self):
        self.healthy()
        self.guard.calls.clear()
        self.guard.source = "available_memory"
        self.assert_refused(required=1)
        self.assertEqual(self.guard.calls, ["propagate", "sample"])
        self.assertEqual(self.proof.snapshot(self.guard)["admission_seq"], 0)

    def test_invalid_allocation_arguments_do_not_receive_admission_receipts(self):
        self.healthy()
        for required, current in ((-1, 0), (0, -1), (True, 0), (0, False), (1.0, 0), (0, "1")):
            with self.subTest(required=required, current=current):
                self.assert_refused(required=required, current=current)
                self.assertEqual(self.proof.snapshot(self.guard)["admission_seq"], 0)

    def test_reset_withdraws_incarnation_and_all_prior_receipts(self):
        self.healthy()
        old = self.proof.require_admission(self.guard, required=1)
        self.proof.reset(self.guard)
        row = self.proof.snapshot(self.guard)
        self.assertNotEqual(row["guard_id"], old["guard_id"])
        for field in ("sample_seq", "iteration_seq", "admission_seq", "admission_ns",
                      "admission_sample_seq", "admission_cap_bytes", "cap_bytes"):
            self.assertEqual(row[field], 0)
        self.assertFalse(row["qualified"])
        self.assert_refused()

    def test_snapshot_is_canonical_typed_and_drops_diagnostic_values(self):
        self.guard.vendor_diagnostic = CANARY
        self.healthy()
        row = self.proof.snapshot(self.guard)
        self.assertEqual(set(row), FIELDS)
        for key, value in row.items():
            self.assertIs(type(value), str if key in STRING_FIELDS else bool if key in BOOL_FIELDS else int)
        self.assertEqual(row["pid"], 4173)
        self.assertEqual(row["clock_domain"], self.proof.CLOCK_DOMAIN)
        self.assertEqual(len(row["guard_id"]), 32)
        self.assertNotIn(CANARY, json.dumps(row))
        row["cap_bytes"] = 1
        self.assertEqual(self.proof.snapshot(self.guard)["cap_bytes"], 80)

    def test_extra_and_boolean_optional_limit_fields_are_unqualified(self):
        for values in (dict(breakdown(), diagnostic=CANARY),
                       dict(breakdown(), metal_cap=False),
                       dict(breakdown(), metal_cap=True)):
            with self.subTest(values=values):
                self.proof.reset(self.guard)
                self.guard.next_breakdown = values
                self.proof.begin_iteration(self.guard)
                self.guard._propagate_memory_limit()
                self.proof.complete_iteration(self.guard)
                row = self.proof.snapshot(self.guard)
                self.assertFalse(row["qualified"])
                self.assertEqual(row["reason"], "cap_invalid")
                self.assertNotIn(CANARY, json.dumps(row))
                self.assert_refused()

    def test_invalid_pid_is_canonical_zero_and_cannot_certify_health(self):
        self.healthy()
        for pid in (0, -1, True, 1.0, CANARY, 2**63):
            with self.subTest(pid=pid), patch.object(self.proof, "_pid", return_value=pid):
                row = self.proof.snapshot(self.guard)
                self.assertEqual(row["pid"], 0)
                self.assertIs(type(row["pid"]), int)
                self.assertFalse(row["qualified"])
                self.assertNotIn(CANARY, json.dumps(row))
                self.assert_refused()

    def test_admission_clock_failure_or_expiry_during_final_publication_refuses(self):
        for failure in (0, self.now + 3_000_000_001):
            with self.subTest(failure=failure):
                self.proof.reset(self.guard)
                self.healthy()
                # Initial snapshot, applied sample, refreshed snapshot,
                # admission stamp and final returned snapshot are separate reads.
                stamps = [self.now, self.now, self.now,
                          0 if failure == 0 else self.now,
                          self.now if failure == 0 else failure]
                with patch.object(self.proof, "_now_ns", side_effect=stamps):
                    self.assert_refused(required=1)

    def test_non_vendor_poll_intervals_refuse_without_rewriting_configuration(self):
        self.healthy()
        for interval in (0.001, 0.5, 2, 10.5, 29.999):
            with self.subTest(interval=interval):
                self.guard._current_poll_interval = interval
                row = self.proof.snapshot(self.guard)
                self.assertEqual(row["poll_interval_ms"], 0)
                self.assertEqual(self.guard._current_poll_interval, interval)
                self.assertEqual(row["reason"], "stale")
                self.assert_refused()

    def test_unresolved_scheduler_withdraws_applied_row_even_on_normal_return(self):
        self.healthy()
        self.guard.unresolved_scheduler = True
        self.assertIsNone(self.guard._propagate_memory_limit())
        row = self.proof.snapshot(self.guard)
        self.assertEqual(row["reason"], "unpropagated")
        self.assertFalse(row["qualified"])
        self.assert_refused()
        self.guard.unresolved_scheduler = False
        self.guard._propagate_memory_limit()
        self.assertTrue(self.proof.snapshot(self.guard)["qualified"],
                        "A new complete propagation clears the old unresolved state")

    def test_explicit_unresolved_hook_revokes_prior_current_evidence(self):
        self.healthy()
        self.proof.unresolved(self.guard)
        self.assertEqual(self.proof.snapshot(self.guard)["reason"], "unpropagated")
        self.assert_refused()

    def run_initial_wait(self, on_sleep=None):
        """Only the target's wait clock/sleep are replaced; no wall-time wait."""
        elapsed, sleeps = [0.0], []
        loop = SimpleNamespace(time=lambda: elapsed[0])

        async def sleep(delay):
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 0.01)
            sleeps.append(delay)
            self.assertLess(len(sleeps), 400, "Initial guard wait must be bounded")
            elapsed[0] += delay
            if on_sleep is not None:
                on_sleep()

        with patch.object(self.proof.asyncio, "get_running_loop", return_value=loop), \
                patch.object(self.proof.asyncio, "sleep", side_effect=sleep):
            result = asyncio.run(self.proof.wait_initial(self.guard))
        return result, elapsed[0], sleeps

    def test_initial_wait_returns_true_only_after_a_qualified_initial_iteration(self):
        self.proof.begin_iteration(self.guard)
        result, elapsed, sleeps = self.run_initial_wait(self.healthy)
        self.assertIs(result, True)
        self.assertEqual(len(sleeps), 1)
        self.assertLess(elapsed, 3)
        before = list(self.guard.calls)
        result, _, sleeps = self.run_initial_wait()
        self.assertIs(result, True)
        self.assertEqual(sleeps, [])
        self.assertEqual(self.guard.calls, before, "A successful wait must not recalculate or restart")

    def test_initial_wait_timeout_or_failed_iteration_returns_false_without_starting(self):
        result, elapsed, sleeps = self.run_initial_wait()
        self.assertIs(result, False)
        self.assertGreaterEqual(elapsed, 3)
        self.assertLess(elapsed, 3.01)
        self.assertTrue(sleeps)
        self.assertEqual(self.guard.calls, [])
        self.proof.fail_iteration(self.guard)
        result, _, sleeps = self.run_initial_wait()
        self.assertIs(result, False)
        self.assertEqual(sleeps, [])
        self.assertEqual(self.guard.calls, [])

    def test_initial_wait_preserves_explicit_disabled_or_custom_policy(self):
        self.proof.complete_iteration(self.guard)
        for tier, prefill in (("balanced", False), ("custom", True)):
            with self.subTest(tier=tier, prefill=prefill):
                self.guard._memory_guard_tier = tier
                self.guard._prefill_memory_guard = prefill
                result, _, sleeps = self.run_initial_wait()
                self.assertIs(result, True)
                self.assertFalse(self.proof.snapshot(self.guard)["qualified"])
                self.assertEqual(sleeps, [])
                self.assertEqual(self.guard.calls, [])
                self.assertEqual((self.guard._memory_guard_tier, self.guard._prefill_memory_guard),
                                 (tier, prefill))

    def fake_engine(self, loaded=False):
        class BatchedEngine:
            def __init__(self):
                self._loaded = loaded
                self.allocations = []

            async def start(self, *args, **kwargs):
                self.allocations.append((args, kwargs))
                self._loaded = True
                return "fake-start-result"

        return BatchedEngine()

    def test_bound_direct_start_checks_before_allocation_and_preserves_arguments(self):
        engine = self.fake_engine()
        seen = []

        def check(candidate):
            self.assertIs(candidate, engine)
            self.assertEqual(candidate.allocations, [])
            seen.append(candidate._loaded)

        self.proof.bind_start(engine, check)
        result = asyncio.run(engine.start("lazy-source", requested=True))
        self.assertEqual(result, "fake-start-result")
        self.assertEqual(seen, [False])
        self.assertEqual(engine.allocations, [(("lazy-source",), {"requested": True})])

    def test_every_bound_start_shape_rejects_before_allocation_without_retry(self):
        async def direct(engine):
            return await engine.start()

        async def lazy(engine):
            async def ensure_loaded():
                if not engine._loaded:
                    return await engine.start()
            return await ensure_loaded()

        async def fallback(engine):
            return await self.proof.start_bound(engine)

        for entry in (direct, lazy, fallback):
            with self.subTest(entry=entry.__name__):
                engine = self.fake_engine()
                checked = []

                def reject(candidate):
                    checked.append(candidate)
                    raise self.proof.GuardEvidenceUnavailable()

                self.proof.bind_start(engine, reject)
                with self.assertRaises(self.proof.GuardEvidenceUnavailable):
                    asyncio.run(entry(engine))
                self.assertEqual(checked, [engine])
                self.assertEqual(engine.allocations, [])
                self.assertIs(engine._loaded, False)

    def test_binding_is_idempotent_and_already_loaded_state_reaches_original_check(self):
        engine = self.fake_engine(loaded=True)
        seen, replaced = [], []
        self.proof.bind_start(engine, lambda candidate: seen.append((candidate, candidate._loaded)))
        bound = engine.start
        self.proof.bind_start(engine, lambda candidate: replaced.append(candidate))
        self.assertIs(engine.start, bound)
        self.assertEqual(asyncio.run(self.proof.start_bound(engine)), "fake-start-result")
        self.assertEqual(seen, [(engine, True)])
        self.assertEqual(replaced, [])
        self.assertEqual(len(engine.allocations), 1)

    def test_unregistered_start_never_allocates(self):
        engine = self.fake_engine()
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            asyncio.run(self.proof.start_bound(engine))
        self.assertEqual(engine.allocations, [])

    def test_loaded_state_uses_only_pinned_constructor_conventions(self):
        for name in ("BatchedEngine", "VLMBatchedEngine", "DFlashEngine", "DistributedBatchedEngine"):
            for loaded in (False, True):
                with self.subTest(name=name, loaded=loaded):
                    engine = type(name, (), {"_loaded": loaded, "_model": object()})()
                    self.assertIs(self.proof.loaded_state(engine), loaded)
            for invalid in (None, 0, 1, "true"):
                with self.subTest(name=name, invalid=invalid):
                    engine = type(name, (), {"_loaded": invalid})()
                    with self.assertRaises(self.proof.GuardEvidenceUnavailable):
                        self.proof.loaded_state(engine)
        for name in ("EmbeddingEngine", "RerankerEngine", "TTSEngine", "STTEngine", "STSEngine"):
            for model in (None, 0, False, "", object()):
                with self.subTest(name=name, model_type=type(model).__name__):
                    engine = type(name, (), {"_model": model, "_loaded": CANARY})()
                    self.assertIs(self.proof.loaded_state(engine), model is not None)
            with self.subTest(name=name, missing_model=True):
                with self.assertRaises(self.proof.GuardEvidenceUnavailable):
                    self.proof.loaded_state(type(name, (), {"_loaded": True})())

    def test_unrecognized_engine_and_subclass_never_reach_allocation(self):
        base = type(self.fake_engine())

        class UnsupportedSubclass(base):
            pass

        for engine in (UnsupportedSubclass(), type("UnknownEngine", (), {"_loaded": False})()):
            with self.subTest(name=type(engine).__name__):
                with self.assertRaises(self.proof.GuardEvidenceUnavailable):
                    self.proof.loaded_state(engine)
        subclass = UnsupportedSubclass()
        self.proof.bind_start(subclass, lambda _: self.fail("Unknown constructor reached admission"))
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            asyncio.run(self.proof.start_bound(subclass))
        self.assertEqual(subclass.allocations, [])

    def fake_nonstreaming(self, name="EmbeddingEngine", error=None):
        class EmbeddingEngine:
            def __init__(self):
                self._model = None
                self._loaded = CANARY  # Deliberately irrelevant for this constructor.
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.allocations = 0
                self.start_calls = 0

            async def start(self):
                self.start_calls += 1
                if self._model is not None:
                    return "already-loaded"
                self.allocations += 1
                self._model = 0  # Falsy assigned state, before asynchronous initialization.
                self.entered.set()
                await self.release.wait()
                if error is not None:
                    raise error
                return "nonstream-ready"

        EmbeddingEngine.__name__ = name
        return EmbeddingEngine()

    def test_concurrent_nonstreaming_starts_wait_for_initialization_and_check_again(self):
        async def exercise(name):
            engine = self.fake_nonstreaming(name)
            checked = []
            self.proof.bind_start(engine, lambda candidate: checked.append((candidate, candidate._model)))
            first = asyncio.create_task(self.proof.start_bound(engine))
            await engine.entered.wait()
            second_entered = asyncio.Event()

            async def second_start():
                second_entered.set()
                return await engine.start()

            second = asyncio.create_task(second_start())
            await second_entered.wait()
            self.assertFalse(second.done(), "Assigned _model cannot bypass in-progress initialization")
            self.assertEqual(engine.allocations, 1)
            self.assertEqual(engine.start_calls, 1)
            self.assertEqual(checked, [(engine, None)])
            engine.release.set()
            self.assertEqual(await first, "nonstream-ready")
            self.assertEqual(await second, "already-loaded")
            self.assertEqual(engine.allocations, 1)
            self.assertEqual(checked, [(engine, None), (engine, 0)],
                             "Already-loaded engines still require a current guard check")

        for name in ("EmbeddingEngine", "RerankerEngine"):
            with self.subTest(name=name):
                asyncio.run(exercise(name))

    def test_original_nonstreaming_failure_poisons_instance_but_not_a_new_engine(self):
        async def exercise():
            original = RuntimeError(CANARY)
            engine = self.fake_nonstreaming(error=original)
            checked = []
            self.proof.bind_start(engine, lambda candidate: checked.append(candidate))
            first = asyncio.create_task(engine.start())
            await engine.entered.wait()
            engine.release.set()
            with self.assertRaises(RuntimeError) as raised:
                await first
            self.assertIs(raised.exception, original)
            self.assertIsNotNone(engine._model)
            with self.assertRaises(self.proof.GuardEvidenceUnavailable) as rejected:
                await self.proof.start_bound(engine)
            self.assertNotIn(CANARY, str(rejected.exception))
            self.assertEqual(checked, [engine])
            self.assertEqual((engine.allocations, engine.start_calls), (1, 1))
            replacement = self.fake_nonstreaming()
            replacement.release.set()
            self.proof.bind_start(replacement, lambda candidate: checked.append(candidate))
            self.assertEqual(await self.proof.start_bound(replacement), "nonstream-ready")
            self.assertEqual(replacement.allocations, 1)

        asyncio.run(exercise())

    def test_cancelled_nonstreaming_initialization_cannot_resume_from_assigned_model(self):
        async def exercise():
            engine = self.fake_nonstreaming("RerankerEngine")
            checked = []
            self.proof.bind_start(engine, lambda candidate: checked.append(candidate))
            first = asyncio.create_task(engine.start())
            await engine.entered.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertIsNotNone(engine._model)
            with self.assertRaises(self.proof.GuardEvidenceUnavailable):
                await engine.start()
            self.assertEqual(checked, [engine])
            self.assertEqual((engine.allocations, engine.start_calls), (1, 1))

        asyncio.run(exercise())

    def test_guard_check_refusal_does_not_poison_an_unstarted_engine(self):
        engine = self.fake_engine()
        allow = [False]
        checked = []

        def check(candidate):
            checked.append(candidate)
            if not allow[0]:
                raise self.proof.GuardCapacityChanged()

        self.proof.bind_start(engine, check)

        async def exercise():
            with self.assertRaises(self.proof.GuardCapacityChanged):
                await engine.start()
            self.assertEqual(engine.allocations, [])
            allow[0] = True
            self.assertEqual(await engine.start(), "fake-start-result")
            self.assertEqual(checked, [engine, engine])
            self.assertEqual(len(engine.allocations), 1)

        asyncio.run(exercise())

    def test_child_start_uses_bound_parent_check_and_child_allocation_state(self):
        async def exercise():
            parent = self.fake_engine(loaded=True)
            child = self.fake_nonstreaming()
            child.release.set()
            checked = []

            def check(candidate, *, force_allocation=False):
                checked.append((candidate, force_allocation))

            self.proof.bind_start(parent, check)
            self.proof.bind_child(parent, child)
            self.assertEqual(await self.proof.start_bound(child), "nonstream-ready")
            self.assertEqual(checked, [(parent, True)],
                             "A resident parent must still reserve a cold child's allocation")
            self.assertEqual(await child.start(), "already-loaded")
            self.assertEqual(checked, [(parent, True), (parent, False)])
            self.assertEqual(child.allocations, 1)
            self.assertEqual(parent.allocations, [])

        asyncio.run(exercise())

    def test_child_start_refusal_prevents_loading_and_unbound_parent_is_rejected(self):
        async def exercise():
            parent = self.fake_engine(loaded=True)
            child = self.fake_nonstreaming()
            with self.assertRaises(self.proof.GuardEvidenceUnavailable):
                self.proof.bind_child(parent, child)
            self.assertFalse(getattr(child, "_wisp_guard_bound", False))
            checked = []

            def reject(candidate, *, force_allocation=False):
                checked.append((candidate, force_allocation))
                raise self.proof.GuardEvidenceUnavailable()

            self.proof.bind_start(parent, reject)
            self.proof.bind_child(parent, child)
            with self.assertRaises(self.proof.GuardEvidenceUnavailable):
                await self.proof.start_bound(child)
            self.assertEqual(checked, [(parent, True)])
            self.assertIsNone(child._model)
            self.assertEqual((child.allocations, child.start_calls), (0, 0))

        asyncio.run(exercise())

    def test_bound_streaming_unknown_loaded_state_never_allocates(self):
        for loaded in (None, 0, 1, "loaded", CANARY):
            with self.subTest(loaded=loaded):
                engine = self.fake_engine(loaded)
                checked = []
                self.proof.bind_start(engine, lambda candidate: checked.append(candidate))
                with self.assertRaises(self.proof.GuardEvidenceUnavailable):
                    asyncio.run(self.proof.start_bound(engine))
                self.assertEqual(checked, [])
                self.assertEqual(engine.allocations, [])
        engine = self.fake_engine()
        del engine._loaded
        self.proof.bind_start(engine, lambda candidate: self.fail("Unknown loaded state reached admission"))
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            asyncio.run(self.proof.start_bound(engine))
        self.assertEqual(engine.allocations, [])

    def test_malformed_profile_and_limits_never_leak_arbitrary_values(self):
        for value in (True, -1, 2**63, 1.5, CANARY, None):
            with self.subTest(value=value):
                self.proof.reset(self.guard)
                self.guard.next_breakdown = dict(breakdown(), dynamic=value)
                self.proof.begin_iteration(self.guard)
                self.guard._propagate_memory_limit()
                self.proof.complete_iteration(self.guard)
                row = self.proof.snapshot(self.guard)
                self.assertFalse(row["qualified"])
                self.assertNotIn(CANARY, json.dumps(row))
        self.guard.next_breakdown = breakdown()
        self.guard._memory_guard_tier = CANARY
        row = self.proof.snapshot(self.guard)
        self.assertEqual(row["tier"], "unknown")
        self.assertNotIn(CANARY, json.dumps(row))
        self.assert_refused()

    def test_running_flag_and_missing_task_do_not_inherit_a_live_iteration(self):
        self.healthy()
        self.guard._running = False
        self.assertEqual(self.proof.snapshot(self.guard)["reason"], "not_running")
        self.assert_refused()
        self.guard._running = True
        self.guard._task = None
        self.assertEqual(self.proof.snapshot(self.guard)["task_state"], "missing")
        self.assert_refused()

    def test_real_task_completion_cancellation_and_failure_withdraw_health(self):
        async def exercise():
            event = asyncio.Event()

            async def pending():
                await event.wait()

            task = asyncio.create_task(pending())
            self.guard._task = task
            self.healthy()
            task.cancel()
            self.assertEqual(self.proof.snapshot(self.guard)["task_state"], "cancelled")
            self.assert_refused()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.proof.snapshot(self.guard)["task_state"], "cancelled")

            async def finish():
                return None

            done = asyncio.create_task(finish())
            await done
            self.guard._task = done
            self.assertEqual(self.proof.snapshot(self.guard)["task_state"], "done")
            self.assert_refused()

            async def fail():
                raise RuntimeError(CANARY)

            failed = asyncio.create_task(fail())
            with self.assertRaisesRegex(RuntimeError, CANARY):
                await failed
            self.guard._task = failed
            row = self.proof.snapshot(self.guard)
            self.assertEqual(row["task_state"], "failed")
            self.assertFalse(row["qualified"])
            self.assertNotIn(CANARY, json.dumps(row))
            self.assert_refused()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
