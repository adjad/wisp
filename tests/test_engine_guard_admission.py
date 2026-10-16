"""Run Wisp's injected pool closure with fakes, not the vendor or engine.

Only the newly added closure is compiled from its checked-in exact patch. No
upstream method, vendor import, model library, native code or probe is run.
"""
import ast
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_engine_guard_proof as fixtures

ROOT = Path(__file__).resolve().parents[1]


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
                                    _process_memory_enforcer=self.guard)
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


if __name__ == "__main__":
    unittest.main()
