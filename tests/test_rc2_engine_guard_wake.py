"""The engine guard after the Mac sleeps: fake clocks only, never a real sleep.

Darwin's CLOCK_MONOTONIC keeps counting while the Mac sleeps. The enforcer's
poll loop waits on asyncio's clock (time.monotonic, which is mach_absolute_time,
the same as CLOCK_UPTIME_RAW), and that clock pauses. With proofs stamped on
CLOCK_MONOTONIC, every proof read hours old the moment the Mac woke, so each
model load (and each request to a loaded model) was refused as "stale" until
the next poll, up to 30 s of awake time later. The guard now ages its evidence
in awake time. A loop that really stops, a wrong cap, or a changed overlay file
still refuses.

proof.py is loaded by file path. No service, oMLX, MLX, model or engine.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
GUARD_DIR = ROOT / "build-support/engine_guard"
PROOF_PATH = GUARD_DIR / "proof.py"
sys.path.insert(0, str(ROOT / "build-support"))
import engine_guard_overlay as overlay  # noqa: E402

SECOND = 1_000_000_000
HOUR = 3600 * SECOND


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MacClock:
    """Two Darwin clocks: awake time, and awake plus asleep time."""

    def __init__(self):
        self.awake = 50 * SECOND
        self.asleep = 0

    def run(self, ns):
        self.awake += ns

    def sleep(self, ns):
        self.asleep += ns

    def clock_gettime_ns(self, clock_id):
        if clock_id == time.CLOCK_UPTIME_RAW:
            return self.awake
        if clock_id == time.CLOCK_MONOTONIC:
            return self.awake + self.asleep
        raise AssertionError(f"unexpected clock {clock_id}")


class RunningTask:
    def cancelled(self):
        return False

    def cancelling(self):
        return 0

    def done(self):
        return False


def breakdown(static=120, dynamic=80, metal_cap=100):
    return dict(static=static, dynamic=dynamic, metal_cap=metal_cap,
                hard_limit=min(static, dynamic, metal_cap))


def enforcer(proof, interval):
    class Enforcer:
        def __init__(self):
            self._memory_guard_tier = "balanced"
            self._prefill_memory_guard = True
            self._running = True
            self._current_poll_interval = interval
            self._task = RunningTask()
            self.next_breakdown = breakdown()
            self.samples = 0

        @proof.sampled
        def calculate(self):
            self.samples += 1
            proof.note_source(self, "host_statistics64")
            return copy.deepcopy(self.next_breakdown)

        @proof.propagating
        def _propagate_memory_limit(self):
            self.calculate()

    return Enforcer()


def load_proof():
    spec = importlib.util.spec_from_file_location("wisp_guard_wake_fixture", PROOF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuardAfterWakeTests(unittest.TestCase):
    def setUp(self):
        self.proof = load_proof()
        self.clock = MacClock()
        # The real _now_ns runs; only the kernel clock read is faked.
        self.enterContext(patch.object(self.proof.time, "clock_gettime_ns", self.clock.clock_gettime_ns))
        self.enterContext(patch.object(self.proof, "_clock_supported", lambda: True))
        self.enterContext(patch.object(self.proof, "_pid", lambda: 4173))

    def poll(self, guard):
        """One background enforcer pass, as the patched loop runs it."""
        self.proof.begin_iteration(guard)
        guard._propagate_memory_limit()
        self.proof.complete_iteration(guard)

    def test_guard_stamps_awake_time(self):
        seen = []
        with patch.object(self.proof.time, "clock_gettime_ns", lambda cid: seen.append(cid) or 7):
            self.assertEqual(self.proof._now_ns(), 7)
        self.assertEqual(seen, [time.CLOCK_UPTIME_RAW])
        self.assertIn("UPTIME_RAW", self.proof.CLOCK_DOMAIN)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin clock")
    def test_clock_support_needs_the_awake_clock(self):
        fresh = load_proof()
        self.assertTrue(fresh._clock_supported())
        with patch.object(fresh, "time", SimpleNamespace()):
            self.assertFalse(fresh._clock_supported())

    def test_a_load_right_after_a_long_sleep_is_admitted_on_a_fresh_proof(self):
        for interval in (1, 10, 30):
            with self.subTest(interval=interval):
                guard = enforcer(self.proof, interval)
                self.poll(guard)
                before = self.proof.snapshot(guard)
                self.assertTrue(before["qualified"], before)
                self.clock.run(interval * SECOND // 2)     # half a poll, then the lid closes
                self.clock.sleep(8 * HOUR)
                self.clock.run(SECOND // 5)                 # woken; the loop hasn't polled yet
                row = self.proof.require_admission(guard, required=10, current=20)
                self.assertTrue(row["qualified"], row)
                # The admission re-propagated the limit: a fresh sample, not the old one.
                self.assertGreater(row["sample_seq"], before["sample_seq"])
                self.assertEqual(row["sample_ns"], self.clock.awake)
                self.assertEqual(row["admission_sample_seq"], row["sample_seq"])

    def test_the_old_sleep_inclusive_stamp_refused_that_same_load(self):
        # The mechanism, shown directly: on CLOCK_MONOTONIC the same wake reads as stale.
        with patch.object(self.proof, "_now_ns",
                          lambda: self.clock.clock_gettime_ns(time.CLOCK_MONOTONIC)):
            guard = enforcer(self.proof, 30)
            self.poll(guard)
            self.assertTrue(self.proof.snapshot(guard)["qualified"])
            self.clock.sleep(8 * HOUR)
            self.clock.run(SECOND // 5)
            self.assertEqual(self.proof.snapshot(guard)["reason"], "stale")
            with self.assertRaises(self.proof.GuardEvidenceUnavailable):
                self.proof.require_admission(guard)

    def test_a_loop_that_stops_after_wake_still_goes_stale(self):
        guard = enforcer(self.proof, 10)
        self.poll(guard)
        self.clock.sleep(8 * HOUR)
        self.clock.run((10 + 2) * SECOND)
        self.assertTrue(self.proof.snapshot(guard)["qualified"])
        self.clock.run(1)                                   # past the bound, in awake time
        self.assertEqual(self.proof.snapshot(guard)["reason"], "stale")
        with self.assertRaises(self.proof.GuardEvidenceUnavailable):
            self.proof.require_admission(guard)
        self.poll(guard)                                    # the loop runs again
        self.assertTrue(self.proof.require_admission(guard)["qualified"])

    def test_a_load_that_no_longer_fits_after_wake_still_refuses(self):
        guard = enforcer(self.proof, 10)
        self.poll(guard)
        self.clock.sleep(8 * HOUR)
        self.clock.run(SECOND)
        guard.next_breakdown = breakdown(60, 40, 50)        # memory shrank while asleep
        with self.assertRaises(self.proof.GuardCapacityChanged):
            self.proof.require_admission(guard, required=30, current=20)

    def test_a_clock_that_runs_backwards_still_refuses(self):
        guard = enforcer(self.proof, 10)
        self.poll(guard)
        self.clock.awake -= 1
        self.assertEqual(self.proof.snapshot(guard)["reason"], "stale")


class OverlayPinTests(unittest.TestCase):
    """A changed guard file refuses at packaging, before any engine runs it."""

    def test_the_manifest_pins_the_reviewed_proof_bytes(self):
        manifest, _ = overlay._manifest(GUARD_DIR)
        row = next(a for a in manifest["additions"] if a["source"] == "proof.py")
        self.assertEqual(row["path"], "omlx/wisp_guard.py")
        self.assertEqual(row["sha256"], sha(PROOF_PATH.read_bytes()))

    def _synthetic(self, base: Path):
        engine, root = base / "engine", base / "overlay"
        (engine / "omlx").mkdir(parents=True)
        root.mkdir()
        rows = []
        for index, target in enumerate(sorted(overlay.TARGETS)):
            original, output = f"source = {index}\n".encode(), f"source = {index + 10}\n".encode()
            (engine / target).parent.mkdir(parents=True, exist_ok=True)
            (engine / target).write_bytes(original)
            patch_doc = {"path": target, "input_sha256": sha(original), "output_sha256": sha(output),
                         "edits": [{"old": original.decode(), "new": output.decode(), "count": 1}]}
            name = Path(target).name + ".patch.json"
            raw = (json.dumps(patch_doc) + "\n").encode()
            (root / name).write_bytes(raw)
            rows.append({k: patch_doc[k] for k in ("path", "input_sha256", "output_sha256")}
                        | {"patch": name, "patch_sha256": sha(raw)})
        additions = []
        for path, source in sorted(overlay.ADDITIONS.items()):
            data = PROOF_PATH.read_bytes() if source == "proof.py" else b"# synthetic, never run\n"
            (root / source).write_bytes(data)
            additions.append({"path": path, "source": source, "sha256": sha(data)})
        manifest = {"version": 2, "id": "wisp-engine-overlay-v2", "upstream_tree_sha256": "a" * 64,
                    "patches": rows, "additions": additions}
        (root / "manifest.json").write_text(json.dumps(manifest))
        return engine, root

    def test_a_tampered_proof_refuses_before_and_after_install(self):
        with tempfile.TemporaryDirectory() as temp:
            engine, root = self._synthetic(Path(temp))
            receipt = overlay.apply(engine, "a" * 64, root=root)
            # A changed installed guard no longer verifies.
            installed = engine / "omlx/wisp_guard.py"
            good = installed.read_bytes()
            installed.write_bytes(good.replace(b"CLOCK_UPTIME_RAW", b"CLOCK_MONOTONIC", 1))
            with self.assertRaises(overlay.OverlayError):
                overlay.verify(engine, receipt, root=root)
            installed.write_bytes(good)
            overlay.verify(engine, receipt, root=root)
        with tempfile.TemporaryDirectory() as temp:
            engine, root = self._synthetic(Path(temp))
            # A changed source guard is never applied.
            (root / "proof.py").write_bytes(PROOF_PATH.read_bytes() + b"\n# unreviewed\n")
            with self.assertRaises(overlay.OverlayError):
                overlay.apply(engine, "a" * 64, root=root)
            self.assertFalse((engine / "omlx/wisp_guard.py").exists())


if __name__ == "__main__":
    unittest.main()
