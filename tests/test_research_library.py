"""Saved-job navigation and explicit resume; no web, models, or live data."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from service.research.orchestrator import ResearchManager, _fallback_plan
from service.research.store import ResearchStore
from tests.research_library_fixtures import fixtures


class ResearchLibraryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="wisp-library-tests-")
        self.store = ResearchStore(Path(self.directory.name) / "research.db")
        self.manager = ResearchManager(self.store)

    async def asyncTearDown(self):
        for task in self.manager._tasks.values():
            task.cancel()
        await asyncio.gather(*self.manager._tasks.values(), return_exceptions=True)
        self.store.close()
        self.directory.cleanup()

    def job(self, index, state="complete"):
        with patch("service.research.store.time.time", return_value=1000 + index):
            return self.store.create_job(f"Fixture research {index}",
                _fallback_plan(f"Fixture research {index}", "quick"), state=state)

    async def test_old_pins_remain_beyond_thirty_recent_jobs(self):
        old = self.job(0)
        self.store.set_pinned(old["id"], True)
        # Pinning updates time; restore an old timestamp via the fixture clock.
        with patch("service.research.store.time.time", return_value=1000):
            self.store.set_pinned(old["id"], True)
        recent = [self.job(i) for i in range(1, 36)]
        rows = self.store.list_jobs()
        self.assertEqual(len(rows), 31)
        self.assertEqual(rows[-1]["id"], old["id"])
        self.assertEqual({r["id"] for r in rows}, {old["id"], *(r["id"] for r in recent[-30:])})
        self.assertEqual(rows[0]["id"], recent[-1]["id"])

    async def test_recent_pin_is_not_duplicated_and_wire_shape_is_preserved(self):
        row = self.job(1)
        self.store.update_job(row["id"], pinned=True, report_md="Exact saved report",
                              report={"sources": [{"evidence_id": "E1"}]})
        rows = self.store.list_jobs()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["pinned"])
        self.assertEqual(rows[0]["report_md"], "Exact saved report")
        self.assertEqual(rows[0]["report"]["sources"][0]["evidence_id"], "E1")

    async def test_zero_recent_limit_still_lists_pinned_jobs(self):
        self.job(0)
        pinned = self.job(1)
        self.store.set_pinned(pinned["id"], True)
        self.assertEqual([row["id"] for row in self.store.list_jobs(limit=0)], [pinned["id"]])

    async def test_listing_and_detail_never_start_work(self):
        for state in ("running", "paused", "awaiting_approval", "complete"):
            row = self.job(1, state)
            self.assertEqual(self.manager.detail(row["id"])["state"], state)
        self.assertEqual(len(self.store.list_jobs()), 4)
        self.assertFalse(self.manager._tasks)

    async def test_restart_recovers_running_job_without_automatically_resuming(self):
        row = self.job(1, "running")
        manager = ResearchManager(self.store)
        self.assertEqual(manager.detail(row["id"])["state"], "paused")
        self.assertFalse(manager._tasks)

    async def test_restart_recovers_interrupted_plan_for_review_without_work(self):
        row = self.store.create_job("Compare study tools", state="planning")
        manager = ResearchManager(self.store)
        recovered = manager.detail(row["id"])
        self.assertEqual(recovered["state"], "awaiting_approval")
        self.assertEqual(recovered["plan"]["objective"], "Compare study tools")
        self.assertGreaterEqual(len(recovered["plan"]["subquestions"]), 2)
        self.assertFalse(manager._tasks)
        self.assertEqual(recovered["model_calls"], 0)

    async def test_cancel_recovered_paused_job_finishes_without_worker(self):
        row = self.job(1, "running")
        manager = ResearchManager(self.store)
        result = manager.cancel(row["id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["stop_reason"], "cancelled")
        self.assertEqual(self.store.events_after(row["id"], 0)[-1]["type"], "done")
        self.assertFalse(manager._tasks)

    async def test_stale_cancel_preserves_finished_report(self):
        row = self.job(1, "complete")
        self.store.update_job(row["id"], report_md="Preserved report")
        result = self.manager.cancel(row["id"])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["report_md"], "Preserved report")

    async def test_stale_resume_or_pause_cannot_revive_finished_job(self):
        for state in ("complete", "partial", "cancelled", "failed"):
            with self.subTest(state=state):
                row = self.job(1, state)
                self.assertEqual(self.manager.start(row["id"], object())["state"], state)
                self.assertEqual(self.manager.pause(row["id"], False)["state"], state)
                self.assertFalse(self.manager._tasks)

    async def test_start_resumes_existing_paused_worker_without_duplicate(self):
        row = self.job(1, "awaiting_approval")
        started = asyncio.Event()

        async def blocked(*_args):
            started.set()
            await asyncio.Event().wait()

        with patch.object(self.manager, "_run", side_effect=blocked) as run:
            self.manager.start(row["id"], object())
            await asyncio.wait_for(started.wait(), 1)
            original = self.manager._tasks[row["id"]]
            self.manager.pause(row["id"], True)
            result = self.manager.start(row["id"], object())
            self.assertEqual(result["state"], "running")
            self.assertFalse(result["pause_requested"])
            self.assertIs(self.manager._tasks[row["id"]], original)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(self.store.events_after(row["id"], 0)[-1]["stage"], "resumed")

    async def test_start_recreates_orphaned_paused_worker_once(self):
        row = self.job(1, "running")
        self.manager = ResearchManager(self.store)
        started = asyncio.Event()

        async def blocked(*_args):
            started.set()
            await asyncio.Event().wait()

        with patch.object(self.manager, "_run", side_effect=blocked) as run:
            first = self.manager.start(row["id"], object())
            self.manager.start(row["id"], object())
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(first["state"], "running")
            self.assertFalse(first["pause_requested"])
            self.assertEqual(run.call_count, 1)

    async def test_real_wire_fixtures_cover_persisted_states_and_report_citations(self):
        data = fixtures()
        self.assertEqual(len(data["jobs"]), 8)
        complete = data["snapshots"]["complete"]
        self.assertIn("Saved report", complete["report_md"])
        self.assertEqual(complete["report"]["sources"][0]["quote"], "Exact fixture evidence.")
        self.assertTrue(complete["pinned"])


if __name__ == "__main__":
    unittest.main()
