"""Synthetic upstream/overlay bytes only; never run a staged engine."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build-support"))
import engine_guard_overlay as overlay


def sha(data):
    return hashlib.sha256(data).hexdigest()


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.engine = self.base / "engine"
        self.engine.mkdir()
        (self.engine / "omlx").mkdir()
        self.root = self.base / "overlay"
        self.root.mkdir()
        self.upstream = "a" * 64
        rows = []
        for index, target in enumerate(sorted(overlay.TARGETS)):
            original = f"source = {index}\n".encode()
            output = f"source = {index + 10}\n".encode()
            (self.engine / target).parent.mkdir(parents=True, exist_ok=True)
            (self.engine / target).write_bytes(original)
            patch = {"path": target, "input_sha256": sha(original), "output_sha256": sha(output),
                     "edits": [{"old": original.decode(), "new": output.decode(), "count": 1}]}
            name = Path(target).name + ".patch.json"
            raw = (json.dumps(patch) + "\n").encode()
            (self.root / name).write_bytes(raw)
            rows.append({k: patch[k] for k in ("path", "input_sha256", "output_sha256")} |
                        {"patch": name, "patch_sha256": sha(raw)})
        addition = b"# synthetic proof, never executed\n"
        (self.root / "proof.py").write_bytes(addition)
        self.manifest = {"version": 1, "id": "wisp-engine-guard-v1", "upstream_tree_sha256": self.upstream,
                         "patches": rows, "addition": {"path": overlay.ADDITION, "source": "proof.py", "sha256": sha(addition)}}
        self.save()

    def save(self):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))

    def tree(self):
        return {str(p.relative_to(self.engine)): p.read_bytes() for p in self.engine.rglob("*") if p.is_file()}

    def apply(self):
        return overlay.apply(self.engine, self.upstream, root=self.root)

    def refuse_without_mutation(self):
        before = self.tree()
        with self.assertRaises((overlay.OverlayError, OSError, ValueError, SyntaxError)):
            self.apply()
        self.assertEqual(before, self.tree())

    def test_exact_application_and_receipt(self):
        receipt = self.apply()
        self.assertEqual(set(receipt["files"]), overlay.TARGETS | {overlay.ADDITION})
        self.assertEqual(receipt["manifest_sha256"], sha((self.root / "manifest.json").read_bytes()))
        overlay.verify(self.engine, receipt, root=self.root)
        for name, digest in receipt["files"].items():
            self.assertEqual(sha((self.engine / name).read_bytes()), digest)

    def test_second_application_refused(self):
        self.apply()
        self.refuse_without_mutation()

    def test_changed_upstream_is_not_fuzzily_patched(self):
        (self.engine / self.manifest["patches"][-1]["path"]).write_text("# changed upstream\n")
        self.refuse_without_mutation()

    def test_patch_tampering_refused(self):
        path = self.root / self.manifest["patches"][-1]["patch"]
        path.write_bytes(path.read_bytes() + b" ")
        self.refuse_without_mutation()

    def test_duplicate_target_refused(self):
        self.manifest["patches"][1] = copy.deepcopy(self.manifest["patches"][0])
        self.save()
        self.refuse_without_mutation()

    def test_patch_path_escape_refused(self):
        self.manifest["patches"][0]["patch"] = "../outside.json"
        self.save()
        self.refuse_without_mutation()

    def test_added_file_cannot_overwrite_existing(self):
        (self.engine / overlay.ADDITION).write_text("# existing\n")
        self.refuse_without_mutation()

    def test_addition_hash_must_match(self):
        (self.root / "proof.py").write_text("# not reviewed\n")
        self.refuse_without_mutation()

    def test_upstream_tree_is_independently_bound(self):
        self.manifest["upstream_tree_sha256"] = "b" * 64
        self.save()
        self.refuse_without_mutation()

    def test_input_hardlink_refused(self):
        path = self.engine / self.manifest["patches"][0]["path"]
        os.link(path, self.base / "alias")
        self.refuse_without_mutation()

    def test_input_fifo_refused_without_waiting_for_a_writer(self):
        path = self.engine / self.manifest["patches"][0]["path"]
        path.unlink()
        os.mkfifo(path)
        self.refuse_without_mutation()

    def test_input_symlink_refused(self):
        path = self.engine / self.manifest["patches"][0]["path"]
        elsewhere = self.base / "elsewhere"
        path.rename(elsewhere)
        path.symlink_to(elsewhere)
        self.refuse_without_mutation()

    def test_parent_symlink_refused(self):
        directory = self.engine / "omlx"
        directory.rename(self.base / "elsewhere")
        directory.symlink_to(self.base / "elsewhere", target_is_directory=True)
        with self.assertRaises(overlay.OverlayError):
            self.apply()
        self.assertFalse((self.base / "elsewhere/wisp_guard.py").exists())

    def test_receipt_cannot_relabel_changed_output(self):
        receipt = self.apply()
        name = next(iter(overlay.TARGETS))
        (self.engine / name).write_text("# changed\n")
        receipt["files"][name] = sha((self.engine / name).read_bytes())
        with self.assertRaises(overlay.OverlayError):
            overlay.verify(self.engine, receipt, root=self.root)

    def test_duplicate_json_keys_refused_even_with_matching_patch_hash(self):
        row = self.manifest["patches"][0]
        path = self.root / row["patch"]
        raw = path.read_text().replace('{', '{"path":"duplicate",', 1).encode()
        path.write_bytes(raw)
        row["patch_sha256"] = sha(raw)
        self.save()
        self.refuse_without_mutation()

    def test_output_hash_is_required_independently_of_edit_counts(self):
        row = self.manifest["patches"][0]
        path = self.root / row["patch"]
        patch = json.loads(path.read_bytes())
        patch["edits"][0]["new"] = "source = 999\n"
        raw = json.dumps(patch).encode()
        path.write_bytes(raw)
        row["patch_sha256"] = sha(raw)
        self.save()
        self.refuse_without_mutation()


class PinnedEnforcerLoopTests(unittest.TestCase):
    """The shipped patch, not synthetic bytes: found by a real fresh install."""

    def test_pass_completes_before_the_ttl_sweep_takes_the_pool_lock(self):
        # The TTL sweep takes the engine pool lock, which a model load holds
        # through its post-load admission check. Completing the pass after the
        # sweep left it "pending" during every load longer than one poll, so a
        # fresh install's first model was always refused (engine_guard_unavailable).
        root = Path(__file__).resolve().parents[1] / "build-support" / "engine_guard"
        patch = json.loads((root / "process_memory_enforcer.py.patch.json").read_text())
        loop = [e["new"] for e in patch["edits"] if "begin_iteration" in e["new"]]
        self.assertEqual(len(loop), 1)
        body = loop[0]
        self.assertLess(body.index("await self._check_and_enforce()"),
                        body.index("_wisp_guard.complete_iteration(self)"))
        self.assertLess(body.index("_wisp_guard.complete_iteration(self)"),
                        body.index("await self._check_ttl()"))

    def test_ttl_sweep_never_waits_on_a_load_held_pool_lock(self):
        # Waiting on the lock stalled the evidence through a slow load (a model
        # swap right after a reply polls at 1s), so the post-load check refused
        # the new model as stale. The sweep now yields to a held lock.
        root = Path(__file__).resolve().parents[1] / "build-support" / "engine_guard"
        patch = json.loads((root / "process_memory_enforcer.py.patch.json").read_text())
        ttl = [e["new"] for e in patch["edits"] if "check_ttl_expirations(" in e["new"]]
        self.assertEqual(len(ttl), 1)
        body = ttl[0]
        self.assertLess(body.index("lock.locked()"), body.index("check_ttl_expirations("))

    def test_gpu_power_cap_is_released_without_upstream_linger(self):
        # Low Power Mode caps the whole Mac's GPU only while Wisp generates;
        # upstream holds the cap 15 s after the last capped request ends.
        root = Path(__file__).resolve().parents[1] / "build-support" / "engine_guard"
        patch = json.loads((root / "server.py.patch.json").read_text())
        limiter = [e for e in patch["edits"] if "GpuPowerLimiter(" in e["old"]]
        self.assertEqual(len(limiter), 1)
        self.assertEqual(limiter[0]["count"], 1)
        self.assertIn("GpuPowerLimiter(base_path, release_delay_s=0.0)", limiter[0]["new"])


if __name__ == "__main__":
    unittest.main()
