"""Run Wisp's injected pool closure with fakes, not the vendor or engine.

Only the newly added closure is compiled from its checked-in exact patch. No
upstream method, vendor import, model library, native code or probe is run.
"""
import ast
import asyncio
import importlib.util
import json
import hashlib
import textwrap
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_engine_guard_proof as fixtures

ROOT = Path(__file__).resolve().parents[1]


def canonical_ast(value):
    if isinstance(value, ast.AST):
        return {"node": type(value).__name__, "fields": [[k, canonical_ast(v)] for k, v in ast.iter_fields(value)]}
    if isinstance(value, list):
        return [canonical_ast(v) for v in value]
    return value


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("admission_proof", fixtures.PROOF_PATH)
        self.proof = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.proof)
        self.enterContext(patch.multiple(self.proof, _now_ns=lambda: 10_000_000_000,
                                        _pid=lambda: 4173, _clock_supported=lambda: True))
        self.guard = fixtures.fake_enforcer(self.proof)
        self.proof.begin_iteration(self.guard)
        self.guard._propagate_memory_limit()
        self.proof.complete_iteration(self.guard)

        class DFlashEngine:
            _loaded = True
            async def start(self):
                return None

        self.parent = DFlashEngine()
        self.entry = SimpleNamespace(engine=self.parent, is_loading=False, _wisp_active_engine=self.parent)
        self.pool = SimpleNamespace(_entries={"model": self.entry}, _current_model_memory=10,
                                    _process_memory_enforcer=self.guard, _unloading_models=set())
        patch_data = json.loads((ROOT / "build-support/engine_guard/engine_pool.py.patch.json").read_text())
        additions = [e["new"] for e in patch_data["edits"] if "def _wisp_check_load(" in e["new"]]
        self.assertEqual(len(additions), 1)
        tree = ast.parse("class Injected:\n    def scope(self):\n" +
                         "\n".join("    " + line for line in additions[0].splitlines()))
        functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_wisp_check_load"]
        self.assertEqual(len(functions), 1)
        isolated = ast.Module(body=functions, type_ignores=[])
        ast.fix_missing_locations(isolated)
        self.context = {"self": self.pool, "entry": self.entry, "model_id": "model",
                        "_wisp_size": 50, "_wisp_committed": False,
                        "mx": SimpleNamespace(get_active_memory=lambda: 0),
                        "_settled_phys_footprint": lambda: 10,
                        "VLMBatchedEngine": type("VLMBatchedEngine", (), {}), "_wisp_guard": self.proof}
        exec(compile(isolated, "<Wisp-injected-pool-closure-only>", "exec"), self.context)
        self.check = self.context["_wisp_check_load"]
        self.proof.bind_start(self.parent, self.check)

    def child(self, loaded=False):
        class BatchedEngine:
            def __init__(self):
                self._loaded = loaded
                self.allocations = 0
            async def start(self):
                if not self._loaded:
                    self.allocations += 1
                    self._loaded = True
        child = BatchedEngine()
        self.proof.bind_child(self.parent, child)
        return child

    def assert_refused_before_allocation(self, child, error=None):
        with self.assertRaises(error or self.proof.GuardEvidenceUnavailable):
            asyncio.run(child.start())
        self.assertEqual(child.allocations, 0)

    def test_detached_parent_cannot_admit_child(self):
        child = self.child()
        self.pool._entries["model"] = SimpleNamespace()
        self.assert_refused_before_allocation(child)

    def test_replaced_active_parent_cannot_admit_child(self):
        child = self.child()
        self.entry._wisp_active_engine = object()
        self.assert_refused_before_allocation(child)

    def test_removed_resident_parent_cannot_admit_child(self):
        child = self.child()
        self.entry.engine = None
        self.assert_refused_before_allocation(child)

    def test_cold_child_checks_full_budget_even_while_parent_loaded(self):
        child = self.child()
        self.guard.next_breakdown = fixtures.breakdown(dynamic=40)
        self.assert_refused_before_allocation(child, self.proof.GuardCapacityChanged)
        self.assertIs(self.parent._loaded, True)

    def test_warm_child_does_not_charge_resident_weights_again(self):
        child = self.child(loaded=True)
        self.guard.next_breakdown = fixtures.breakdown(dynamic=40)
        asyncio.run(child.start())
        self.assertEqual(child.allocations, 0)
        self.assertTrue(self.proof.snapshot(self.guard)["qualified"])

    def test_cold_child_has_one_admitted_fake_allocation(self):
        child = self.child()
        asyncio.run(child.start())
        self.assertEqual(child.allocations, 1)
        self.assertGreater(self.proof.snapshot(self.guard)["admission_seq"], 0)

    def test_fresh_cap_uses_existing_eviction_baseline_policy(self):
        self.context["_settled_phys_footprint"] = lambda: 100
        self.assert_refused_before_allocation(self.child(), self.proof.GuardCapacityChanged)
        self.context["_wisp_committed"] = True
        child = self.child()
        asyncio.run(child.start())
        self.assertEqual(child.allocations, 1)

    def test_deliberate_profiles_stay_unqualified_without_rewrites(self):
        for tier, prefill in (("balanced", False), ("custom", True)):
            with self.subTest(tier=tier, prefill=prefill):
                self.guard._memory_guard_tier = tier
                self.guard._prefill_memory_guard = prefill
                child = self.child()
                asyncio.run(child.start())
                self.assertEqual(child.allocations, 1)
                self.assertFalse(self.proof.snapshot(self.guard)["qualified"])
                self.assertEqual((self.guard._memory_guard_tier, self.guard._prefill_memory_guard), (tier, prefill))

    def source_propagation(self, loaded, target, *, prefill=True):
        """Only an extracted plain-Python decision branch runs with fake entries.

        Its AST is independently compared to the exact patched vendor output
        in source-bound mechanical evidence. No vendor module is imported.
        """
        fixture = json.loads((ROOT / "tests/fixtures/engine_guard/propagation_branch.json").read_text())
        manifest = json.loads((ROOT / "build-support/engine_guard/manifest.json").read_text())
        pinned = next(r for r in manifest["patches"] if r["path"] == "omlx/process_memory_enforcer.py")
        self.assertEqual((fixture["input_sha256"], fixture["output_sha256"]),
                         (pinned["input_sha256"], pinned["output_sha256"]))
        self.assertEqual(hashlib.sha256(fixture["python"].encode()).hexdigest(), fixture["source_sha256"])
        parsed = ast.parse(fixture["python"])
        branch = parsed.body[0].body[0]
        self.assertEqual(hashlib.sha256(json.dumps(canonical_ast(branch), sort_keys=True).encode()).hexdigest(),
                         fixture["branch_ast_sha256"])
        context = {"_wisp_guard": self.proof, "BaseNonStreamingEngine": type("BaseNonStreamingEngine", (), {}),
                   "soft_limit": 20, "logger": SimpleNamespace(warning=lambda *a, **kw: None)}
        exec(compile(parsed, "<pinned-scheduler-decision-branch-with-fakes>", "exec"), context)
        class DFlashEngine:
            _fallback_engine = None
        engine = DFlashEngine()
        engine._loaded = loaded
        engine._prefill_guard = target
        self.guard._engine_pool = SimpleNamespace(_entries={"model": SimpleNamespace(engine=engine)})
        self.guard._resolve_scheduler = lambda entry: entry.engine._prefill_guard
        self.guard._scheduler_resolve_warned = set()
        self.guard._prefill_memory_guard = prefill
        @self.proof.propagating
        def branch_propagation(enforcer):
            # Use the existing sampled fake, then the exact decision branch.
            enforcer.calculate()
            context["source_scheduler_branch"](enforcer)
        self.guard._propagate_memory_limit = lambda: branch_propagation(self.guard)
        self.proof.begin_iteration(self.guard)
        self.guard._propagate_memory_limit()
        self.proof.complete_iteration(self.guard)
        return self.proof.snapshot(self.guard)

    def test_loaded_primary_dflash_without_guard_cannot_qualify(self):
        row = self.source_propagation(True, None)
        self.assertFalse(row["qualified"])
        self.assertEqual(row["reason"], "unpropagated")
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            self.proof.require_admission(self.guard)

    def test_genuinely_unloaded_dflash_is_not_a_missing_loaded_target(self):
        self.assertTrue(self.source_propagation(False, None)["qualified"])

    def test_loaded_dflash_with_resolved_guard_remains_eligible(self):
        self.assertTrue(self.source_propagation(True, object())["qualified"])

    def test_disabled_profile_never_claims_healthy_missing_dflash_guard(self):
        row = self.source_propagation(True, None, prefill=False)
        self.assertFalse(row["qualified"])
        self.assertEqual(row["reason"], "disabled")
        self.assertFalse(self.proof.require_admission(self.guard)["qualified"])
        self.assertIs(self.guard._prefill_memory_guard, False)

    def first_request(self, *, cleanup_error=False):
        """Exercise the exact Wisp post-load handoff, then fake generation.

        The already attached fake represents the first cold request after the
        original pool's propagation. Its existing teardown is a synthetic stub.
        """
        patch_data = json.loads((ROOT / "build-support/engine_guard/engine_pool.py.patch.json").read_text())
        hooks = [e["new"] for e in patch_data["edits"] if "async def _wisp_cleanup_rejected_load(" in e["new"]]
        self.assertEqual(len(hooks), 1)
        hook = hooks[0].split("            logger.info(", 1)[0]
        # Only Wisp's added block is compiled; no upstream method executes.
        source = "async def first_request():\n    load_completed = True\n"
        source += "\n".join(line[8:] if line.startswith("        ") else line for line in hook.splitlines())
        source += "\n    generated.append(engine)\n    return load_completed\n"
        self.generated, self.cleanup = [], []

        async def unload(model_id, *, _wisp_expected):
            self.assertEqual(_wisp_expected, (self.entry, self.parent))
            self.cleanup.append(model_id)
            if cleanup_error:
                raise RuntimeError("private teardown diagnostic")
            self.entry.engine = None

        self.pool._unload_engine = unload
        context = dict(self=self.pool, entry=self.entry, engine=self.parent, model_id="model",
                       _wisp_guard=self.proof, _wisp_check_load=self.check, generated=self.generated)
        exec(compile(ast.parse(source), "<Wisp-post-load-handoff-with-fakes>", "exec"), context)
        return context["first_request"]()

    def test_first_cold_request_missing_guard_refuses_before_generation_and_unloads(self):
        self.source_propagation(True, None)
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            asyncio.run(self.first_request())
        self.assertEqual(self.generated, [])
        self.assertEqual(self.cleanup, ["model"])
        self.assertIsNone(self.entry.engine)
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            self.proof.require_usable(self.parent)

    def test_failed_teardown_cannot_reuse_or_start_rejected_engine(self):
        self.source_propagation(True, None)
        with self.assertRaises(self.proof.GuardEvidenceUnavailable) as raised:
            asyncio.run(self.first_request(cleanup_error=True))
        self.assertNotIn("private teardown diagnostic", str(raised.exception))
        self.assertEqual(self.generated, [])
        self.assertIs(self.entry.engine, self.parent)
        self.assertEqual(self.pool._current_model_memory, 10)
        # Even recovery of telemetry cannot reopen this quarantined instance.
        self.source_propagation(True, object())
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            self.proof.require_usable(self.parent)
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            asyncio.run(self.parent.start())

    def test_first_cold_request_with_guard_reaches_generation_without_cleanup(self):
        self.source_propagation(True, object())
        self.assertTrue(asyncio.run(self.first_request()))
        self.assertEqual(self.generated, [self.parent])
        self.assertEqual(self.cleanup, [])
        self.assertFalse(getattr(self.parent, "_wisp_guard_load_rejected", False))

    def test_first_request_preserves_deliberately_disabled_profile(self):
        self.source_propagation(True, None, prefill=False)
        self.assertTrue(asyncio.run(self.first_request()))
        self.assertEqual(self.generated, [self.parent])
        self.assertEqual(self.cleanup, [])
        self.assertIs(self.guard._prefill_memory_guard, False)
        self.assertFalse(self.proof.snapshot(self.guard)["qualified"])

    def test_rejected_handoff_does_not_unload_replaced_registry_owner(self):
        replacement = SimpleNamespace(engine=object())
        self.pool._entries["model"] = replacement
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            asyncio.run(self.first_request())
        self.assertEqual(self.generated, [])
        self.assertEqual(self.cleanup, ["model"], "Cleanup still owns the original engine")
        self.assertIs(self.pool._entries["model"], replacement)

    def test_unloading_model_refuses_cold_child_before_allocation(self):
        child = self.child()
        self.pool._unloading_models.add("model")
        self.assert_refused_before_allocation(child)

    def test_tearing_down_parent_cannot_restart_through_child(self):
        child = self.child()
        self.parent._wisp_guard_unloading = True
        self.assert_refused_before_allocation(child)

    def test_post_settlement_budget_guards_preserve_replacement_and_reset_counters(self):
        data = json.loads((ROOT / "build-support/engine_guard/engine_pool.py.patch.json").read_text())
        blocks = [e["new"] for e in data["edits"] if e["old"].startswith("        # Release memory tracking AFTER barrier")
                  or e["old"].startswith("            self._current_model_memory = max(")]
        self.assertEqual(len(blocks), 2, "Both local and distributed release paths must be fenced")
        for block in blocks:
            code = compile(ast.parse(textwrap.dedent(block)), "<Wisp-budget-release-only>", "exec")
            for state in ("registry-reset", "same-entry-rebound", "owned-stopped"):
                with self.subTest(state=state, block=blocks.index(block)):
                    self.pool._current_model_memory = 100
                    self.entry.engine = self.parent if state == "same-entry-rebound" else None
                    self.pool._entries["model"] = SimpleNamespace(engine=object()) if state == "registry-reset" else self.entry
                    exec(code, {"self": self.pool, "entry": self.entry, "model_id": "model", "resident_size": 50})
                    self.assertEqual(self.pool._current_model_memory, 50 if state == "owned-stopped" else 100)


if __name__ == "__main__":
    unittest.main()
