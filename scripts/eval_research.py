"""Offline evaluation harness for Wisp Research.

Unlike eval_core.py/eval_ling.py (which drive the live backend + a real oMLX
model over HTTP), this harness runs ResearchManager in-process against a
temporary SQLite store and a small deterministic FixtureModelClient — no
running backend, no oMLX server, no network. That is deliberate: the
properties this harness checks (citation grounding, SSRF refusal, contradiction
detection) are host-side guarantees that must hold regardless of what a real
model says, so a fixture that tries to defeat them is a more honest test than
one that hopes a real model behaves.

Each fixture supplies its own subquestions, mock sources, and (for the SSRF
fixture) real malicious URLs that fall through to the REAL fetch_page/
validate_public_url — that specific code path is not mocked, so this harness
actually exercises the live SSRF boundary rather than assuming it.

Usage:
    python -m scripts.eval_research                    # run every fixture
    python -m scripts.eval_research --fixture ssrf_localhost
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from service.research import coverage as coverage_lib
from service.research.orchestrator import ResearchManager
from service.research.store import ResearchStore
from service.research.web import Page, SearchHit
from service.research.web import fetch_page as real_fetch_page

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "test_fixtures" / "research"


class FixtureModelClient:
    """Deterministic stand-in for oMLX, driven entirely by fixture data.

    The point is not to simulate Ornith's prose quality — it's to feed the
    orchestrator adversarial or straightforward JSON on cue so the HOST'S
    validation (exact-quote matching, SSRF checks, contradiction detection)
    is what gets graded, not a real model's mood that day.
    """

    def __init__(self, fixture: dict) -> None:
        self.fixture = fixture
        self.exclusive_loads: list[bool] = []

    async def ensure_only(self, _model: str, *, exclusive: bool = False, **_kwargs) -> None:
        self.exclusive_loads.append(exclusive)

    async def models(self) -> list[str]:
        return ["fixture-model"]

    async def chat(self, _model: str, messages: list[dict], **_kwargs) -> dict:
        system = messages[0]["content"]
        user = messages[1]["content"]
        if system.startswith("Create a compact research plan"):
            content = json.dumps({
                "title": self.fixture["id"], "objective": self.fixture["prompt"],
                "subquestions": [{"question": q["question"]} for q in self.fixture["subquestions"]],
            })
        elif system.startswith("Generate precise public-web search queries"):
            content = self._queries(user)
        elif system.startswith("Extract atomic evidence"):
            content = self._extract(user)
        elif system.startswith("You are a strict fact-checking critic"):
            content = self._critic(user)
        else:
            content = self._synthesize(user)
        return {"choices": [{"message": {"content": content}}]}

    def _queries(self, user: str) -> str:
        payload = json.loads(user)
        already = {q.lower() for q in payload.get("already_tried", [])}
        out = []
        for question in payload.get("subquestions", []):
            qid = question["id"]
            issued = 0
            for source in self.fixture.get("sources", []):
                if issued >= 2:
                    break
                if not any(e["subquestion_id"] == qid for e in source.get("evidence", [])):
                    continue
                keywords = source.get("match_keywords") or [question["question"]]
                query = keywords[0]
                if query.lower() in already:
                    continue
                out.append({"subquestion_id": qid, "query": query})
                issued += 1
        return json.dumps(out)

    def _extract(self, user: str) -> str:
        payload = json.loads(user)
        chunk = " ".join(str(payload.get("SOURCE", "")).split())
        valid_ids = {q["id"] for q in payload.get("SUBQUESTIONS", [])}
        for source in self.fixture.get("sources", []):
            normalized = " ".join(source["text"].split())
            if chunk and chunk[:80] not in normalized:
                continue
            items = [dict(e, confidence=0.9) for e in source.get("evidence", [])
                     if e["subquestion_id"] in valid_ids]
            fabricated = source.get("fabricated_claim")
            if fabricated and fabricated["subquestion_id"] in valid_ids:
                items.append({**fabricated, "stance": "supports", "confidence": 0.95})
            return json.dumps(items)
        return "[]"

    def _critic(self, user: str) -> str:
        payload = json.loads(user)
        return json.dumps([{"index": c["index"], "label": "supported"} for c in payload.get("claims", [])])

    def _synthesize(self, user: str) -> str:
        payload = json.loads(user)
        lines = [f"# {self.fixture['id']}", ""]
        for packet in payload.get("evidence", []):
            lines.append(f"- {packet['source_domain']} reports on {packet['subquestion_id']} [E:{packet['id']}].")
        return "\n".join(lines)


@dataclass
class FixtureResult:
    id: str
    passed: bool
    elapsed_s: float
    state: str
    coverage_ratio: float
    contradictions: int
    evidence_count: int
    model_calls: int
    sources_read: int
    sources_failed: int
    failures: list[str] = field(default_factory=list)


async def run_fixture(fixture: dict) -> FixtureResult:
    tmp = tempfile.TemporaryDirectory()
    try:
        store = ResearchStore(Path(tmp.name) / "research.db")
        manager = ResearchManager(store)
        client = FixtureModelClient(fixture)
        sources_by_url = {s["url"]: s for s in fixture.get("sources", [])}
        for url in fixture.get("off_topic_urls", []):
            sources_by_url[url] = {"title": "Leader in Cyber Security Solutions",
                "text": "Our next-generation firewall secures your network traffic with AI-driven threat prevention.",
                "published_at": ""}

        async def fake_search(query: str, *, limit: int = 8) -> list[SearchHit]:
            qlow = query.lower()
            hits = []
            for source in fixture.get("sources", []):
                if any(k.lower() in qlow for k in source.get("match_keywords", [])):
                    hits.append(SearchHit(title=source["title"], url=source["url"],
                        domain=source["domain"], rank=len(hits) + 1, query=query))
            for url in fixture.get("malicious_urls", []):
                hits.append(SearchHit(title="Suspicious result", url=url,
                    domain=urlparse(url).hostname or "unknown", rank=len(hits) + 1, query=query))
            # A real, unrelated search result riding along on every query — the
            # relevance filter, not domain/SSRF logic, is what must catch this.
            for url in fixture.get("off_topic_urls", []):
                hits.append(SearchHit(title="Leader in Cyber Security Solutions", url=url,
                    domain=urlparse(url).hostname or "unknown", rank=len(hits) + 1, query=query))
            return hits

        async def hybrid_fetch(url: str) -> Page:
            source = sources_by_url.get(url)
            if source:
                return Page(url=url, canonical_url=url, title=source["title"], text=source["text"],
                           content_type="text/html", published_at=source.get("published_at", ""))
            # Deliberately NOT mocked: a malicious_urls entry falls through to
            # the real fetch_page, so this harness exercises the actual SSRF
            # boundary rather than assuming it holds.
            return await real_fetch_page(url)

        started = time.monotonic()
        with patch("service.research.orchestrator.search_web", fake_search), \
             patch("service.research.orchestrator.fetch_page", hybrid_fetch):
            created = await manager.create(client, fixture["prompt"], depth=fixture.get("depth", "standard"))
            jid = created["id"]
            manager.start(jid, client)
            await manager._tasks[jid]
        elapsed = time.monotonic() - started

        result = manager.detail(jid)
        evidence_rows = store.evidence(jid)
        plan = result["plan"]
        coverage_map = coverage_lib.coverage(evidence_rows, plan)
        covered = sum(1 for v in coverage_map.values() if v["independent_sources"] > 0)
        coverage_ratio = covered / max(1, len(coverage_map))
        source_rows = store.sources(jid)
        failures: list[str] = []

        expect = fixture.get("expect", {})
        if result["state"] not in expect.get("final_state_in", [result["state"]]):
            failures.append(f"state={result['state']!r}, expected one of {expect.get('final_state_in')}")
        if coverage_ratio < expect.get("min_coverage_ratio", 0):
            failures.append(f"coverage_ratio={coverage_ratio:.2f} < {expect['min_coverage_ratio']}")
        if len(result["contradictions"]) < expect.get("min_contradictions", 0):
            failures.append(f"contradictions={len(result['contradictions'])} < {expect['min_contradictions']}")
        for url in expect.get("blocked_urls", []):
            if any(row["url"] == url and row["status"] == "read" for row in source_rows):
                failures.append(f"SSRF boundary failed: {url} was fetched and read")
        for question in expect.get("gap_questions", []):
            if f"- {question}" not in result["report_md"]:
                failures.append(f"expected gap not reported honestly: {question!r}")
        if expect.get("fabricated_claims_rejected"):
            for source in fixture.get("sources", []):
                fabricated = source.get("fabricated_claim")
                if fabricated and any(row["quote"] == fabricated["quote"] for row in evidence_rows):
                    failures.append("a fabricated (non-verbatim) claim was accepted as evidence")
        # Regression guard: every retained evidence quote must still be an
        # exact substring of the source body actually stored for it.
        bodies = {row["source_id"]: row["body"] for row in source_rows}
        for row in evidence_rows:
            body = " ".join(bodies.get(row["source_id"], "").split())
            if row["quote"] not in body:
                failures.append(f"{row['evidence_id']} quote is not an exact substring of its stored source")

        store.close()
        return FixtureResult(
            id=fixture["id"], passed=not failures, elapsed_s=elapsed, state=result["state"],
            coverage_ratio=coverage_ratio, contradictions=len(result["contradictions"]),
            evidence_count=len(evidence_rows), model_calls=result.get("model_calls", 0),
            sources_read=len([s for s in source_rows if s["status"] in {"read", "extracted"}]),
            sources_failed=len([s for s in source_rows if s["status"] == "failed"]),
            failures=failures)
    finally:
        tmp.cleanup()


def load_fixtures(only: str | None) -> list[dict]:
    fixtures = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        fixture = json.loads(path.read_text("utf-8"))
        if only and fixture["id"] != only:
            continue
        fixtures.append(fixture)
    return fixtures


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", help="Run only the fixture with this id")
    args = parser.parse_args()

    fixtures = load_fixtures(args.fixture)
    if not fixtures:
        print(f"No fixtures found in {FIXTURES_DIR}" + (f" matching {args.fixture!r}" if args.fixture else ""))
        return 1

    results = [await run_fixture(f) for f in fixtures]

    print(f"\n{'FIXTURE':<28} {'STATE':<10} {'COV':>5} {'CONTR':>6} {'EVID':>5} "
          f"{'CALLS':>6} {'READ':>5} {'FAIL':>5} {'TIME':>6}  RESULT")
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"{r.id:<28} {r.state:<10} {r.coverage_ratio:>5.0%} {r.contradictions:>6} "
              f"{r.evidence_count:>5} {r.model_calls:>6} {r.sources_read:>5} {r.sources_failed:>5} "
              f"{r.elapsed_s:>5.2f}s  {mark}")
        for failure in r.failures:
            print(f"    - {failure}")

    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed}/{len(results)} fixtures passed.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
