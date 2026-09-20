"""QA-only request contract; staged as service/main.py, never production main."""
from __future__ import annotations

try:  # build-support package during offline tests
    from .harness import OneShotHarness, QAError
except ImportError:  # staged as service/main.py
    from .qa_harness import OneShotHarness, QAError


class ManagedQABackend:
    def __init__(self, harness: OneShotHarness, manifest_id: str, capability: str):
        self.harness, self.manifest_id, self.capability = harness, manifest_id, capability

    async def post(self, body: dict, run, readiness: dict[str, bool]):
        if type(body) is not dict or set(body) != {"manifest_id", "capability"}:
            raise QAError("invalid_request")
        if body["manifest_id"] != self.manifest_id:
            raise QAError("invalid_manifest")
        return await self.harness.consume(body["capability"] == self.capability, readiness, run)
