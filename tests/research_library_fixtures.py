"""Generate native-test wire fixtures from the real isolated research store."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.research.orchestrator import ResearchManager, _fallback_plan
from service.research.store import ResearchStore


def fixtures() -> dict:
    with tempfile.TemporaryDirectory(prefix="wisp-library-fixtures-") as directory:
        store = ResearchStore(Path(directory) / "research.db")
        manager = ResearchManager(store)
        snapshots = {}
        for state in ("complete", "partial", "awaiting_approval", "paused", "running",
                      "cancelled", "failed", "planning"):
            prompt = f"Compare local study tools: {state}"
            plan = _fallback_plan(prompt, "standard")
            plan["title"] = f"Study tools — {state}"
            row = store.create_job(prompt, plan, state=state)
            if state in {"complete", "partial"}:
                store.update_job(row["id"], report_md="# Saved report\n\nA useful finding [1].",
                    report={"sources": [{"evidence_id": "E1", "n": 1, "title": "Fixture source",
                        "url": "https://example.invalid/study", "claim": "A useful finding",
                        "quote": "Exact fixture evidence."}]}, pinned=state == "complete")
            if state == "failed":
                store.update_job(row["id"], error="Fixture service unavailable")
            snapshots[state] = manager.detail(row["id"])
        data = {"jobs": store.list_jobs(), "snapshots": snapshots}
        store.close()
        return data


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(fixtures()), encoding="utf-8")
