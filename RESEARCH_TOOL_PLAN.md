# Wisp Research — Product and Implementation Plan

**Status:** MVP + Phases 2–4 implemented (evidence chunking with persisted offsets, an iterative multi-round research loop with independent-source coverage and deterministic contradiction detection, a two-pass citation critic, source-quality classification, mid-run domain edits, cross-job fetch caching, retention/pin/delete, and orphaned-job recovery on backend restart). A starter Phase 5 eval harness (`scripts/eval_research.py` + `test_fixtures/research/`) covers the happy path, a conflicting-sources case, and two adversarial cases (SSRF via a real malicious URL, and a fabricated non-verbatim claim). Not yet built: `research_plans`/`research_sections` version history as separate tables (plan edits/steering already live in the event log), embedding-based rerank, PDF/Word export, and the full 20-task fixture suite the release bar calls for.  
**Target model:** the abliterated Ornith 1.5 9B checkpoint assigned to Wisp's `coding` role (or an explicit `research` role) through oMLX  
**Target machine:** M5 Pro MacBook Pro, 24 GB unified memory  
**Goal:** give Wisp a research mode with the useful parts of Claude Research and ChatGPT Deep Research: an editable plan, visible progress, iterative web investigation, source control, interruption, and a reusable report with auditable citations.

## 1. Product decision

Build Research as a **dedicated, resumable workflow**, not as another tool inside Wisp's existing eight-step chat agent loop.

The existing loop is optimized for short actions and a small tool menu. Research is different: it can take many minutes, needs persistent intermediate state, must survive a UI disconnect, and should make progress deterministically even when the model makes a weak tool choice. A Python state machine should own the workflow. Ornith should perform small, bounded reasoning jobs inside that state machine.

The goal is feature parity in experience, not frontier-model scale. ChatGPT exposes a plan that can be edited, live progress, steering, source selection, and a cited report. Claude describes progressive searches that build on earlier results, decomposition into smaller investigations, and reports that cite original material. Wisp should offer those same controls, but its standard run should examine roughly **8–15 strong sources**, not hundreds.

### How to use the implemented MVP

1. Open Wisp with **⌥Space** and turn on the **Research** chip.
2. Enter a research question. A separate Wisp Research window opens after Ornith drafts the plan.
3. Edit the title, objective, questions, or depth. **Quick** reads up to 5 pages, **Standard** up to 12, and **Deep** up to 22.
4. Select **Start research**. Keep the window open to watch queries, page reads, evidence counts, and failures. Closing the window does not erase the SQLite job.
5. Use **Pause** between atomic steps, **Cancel** to stop without deleting gathered data, or the steering field to append guidance such as “prioritize primary studies after 2024.”
6. In the finished report, use the citation drawer to inspect the exact source passage, click a citation card to open the original URL, and choose **Export Markdown** to save the report in Downloads.

Research mode deliberately gives Ornith no shell, mail, message, calendar, or file-write tools. The host owns search, URL validation, fetch limits, quote verification, and citation rendering. Search terms and page requests leave the Mac; model calls and the research database remain local.

References:

- [ChatGPT Deep Research overview](https://help.openai.com/en/articles/10500283-deep-research-faq)
- [Claude Research overview](https://www.anthropic.com/news/research)
- [Claude advanced Research](https://www.anthropic.com/news/integrations)
- [Ornith 1.5 9B model card](https://huggingface.co/ornith-ai/Ornith-1.5-9B)

## 2. User experience

### Starting a run

Add a **Research** toggle beside Wisp's existing Super control. With Research enabled, submitting a prompt opens a plan card rather than immediately browsing.

The plan card contains:

- the research goal and intended audience;
- 3–6 subquestions;
- requested timeframe and geography;
- permitted sources: open web, selected domains, attached local files, or Wisp's local data;
- output shape: brief, comparison, literature review, recommendation, or custom outline;
- depth: Quick, Standard, or Deep;
- an explicit **Start research** button and an **Edit plan** action.

Ask a clarifying question only when a missing decision materially changes the research. Otherwise, show an editable best-effort plan.

### While it runs

Use a dedicated report window rather than forcing a long report into the 640-point notch panel. The notch can show compact status and reopen the report window.

The window should show:

- current stage and elapsed time;
- completed and active subquestions;
- queries being searched;
- sources found, accepted, rejected, and failed;
- emerging findings and unresolved contradictions;
- **Pause**, **Cancel**, and **Steer** controls;
- source-domain controls that can be changed mid-run.

Steering text such as “focus on primary studies after 2024” should become a persisted instruction. It should not discard completed work unless the user explicitly restarts.

### Finished report

The final view should contain:

- an executive summary;
- the requested sections;
- inline, clickable citations;
- a source drawer showing title, publisher/domain, publication date, retrieval time, and the exact supporting passage;
- disagreements and evidence gaps;
- a short methods note: queries issued, pages read, pages rejected, and known limitations;
- export to Markdown in the MVP, then PDF and Word later.

The report must distinguish among:

- **source fact** — directly supported by a cited passage;
- **inference** — Wisp's conclusion from multiple cited facts;
- **uncertain** — evidence is incomplete or conflicting.

## 3. Workflow architecture

```text
User request
    |
    v
Plan draft -> user review -> approved plan
    |
    v
Query generation -> search -> URL dedupe/rank
    |                         |
    |                         v
    +--------------------> fetch/normalize
                              |
                              v
                    chunk + evidence extraction
                              |
                              v
                    coverage/contradiction matrix
                         |               |
                  gaps remain       budget exhausted
                         |               |
                         +-> new queries |
                                         v
                              outline -> section drafts
                                         |
                                         v
                             citation and claim verifier
                                         |
                                         v
                              report + source appendix
```

The orchestrator, not the model, decides which state comes next. Every state writes to SQLite before advancing, so a backend restart or closed window can resume the job.

### Model jobs

Ornith should receive only one narrow job per call:

1. turn the request into a structured plan;
2. generate search queries for one subquestion;
3. judge the relevance of a small batch of search results;
4. extract atomic evidence from one bounded document chunk;
5. identify gaps or conflicts from compact evidence records;
6. produce an outline;
7. draft one report section from selected evidence records;
8. revise claims that fail verification.

Do not give the model raw browser control or Wisp's action tools during research. Search and fetch are host functions with typed inputs and outputs.

## 4. Model-specific design for Ornith 1.5 9B oQ6e Abliterated

The upstream Ornith 1.5 9B card advertises a 262,144-token context window, Qwen-style reasoning, and structured tool calling. Those properties must be re-tested on the exact **abliterated oQ6e** checkpoint; abliteration and quantization can change instruction following, parser behavior, and refusal behavior.

### Practical context policy

Do not design around the advertised 256K window. On this 24 GB machine, KV cache is the expensive part and Wisp already has evidence that oversized tool results can crash local inference. Use:

- a 24K–32K working request ceiling initially;
- 2K–4K-character normalized chunks with overlap;
- at most 6–10 evidence packets in a section-drafting call;
- 600–1,200 output tokens for extraction or planning;
- 1,500–2,500 output tokens for a report section;
- no call containing all fetched documents or the entire report history.

The report is assembled section by section in Python. A final model pass may improve transitions using only section summaries, never all raw sources.

### Inference behavior

- Preserve the checkpoint's exact chat template.
- Configure and verify oMLX's Qwen reasoning/tool parser before relying on structured calls.
- Prefer schema-constrained JSON for planner and extractor calls. Validate with Pydantic and retry once with a compact repair prompt.
- Keep reasoning enabled for query planning, gap analysis, and conflict resolution.
- Disable reasoning only for proven transformation tasks such as formatting already-validated evidence; add the model to Wisp's `no_thinking_capable` list only after a measured test.
- Do not assume the base model's recommended sampling is optimal for the abliterated oQ6e build. Run a small sweep and choose settings by structured-output validity, citation recall, and repetition rate—not by subjective prose quality.
- Serialize model calls. Wisp has already measured that concurrent generations against one resident oMLX model are slower and unstable. Network fetches may use bounded concurrency because they do not invoke the model.

### Abliterated-model safety boundary

No safety property may depend on Ornith refusing an instruction.

Fetched pages are untrusted data and may contain prompt injection. The host must enforce these rules:

- research mode exposes only search/fetch/read operations, never mail, messages, shell, file writes, calendar writes, or self-authored tools;
- instructions found inside sources are stored as content and never promoted to system/user messages;
- URLs are validated before fetching; block localhost, link-local, private-network, and non-HTTP(S) targets to prevent SSRF;
- cap redirects, response bytes, decompressed bytes, fetch time, and per-domain requests;
- never place private Wisp data into a public-web query unless the plan explicitly enables it and the UI previews the query;
- keep web content separated with source IDs and clear data delimiters;
- citation verification is deterministic and cannot be bypassed by model prose.

## 5. Backend design

Create a new package instead of expanding `service/tools/web_tools.py`:

```text
service/research/
  models.py          # Pydantic request, plan, source, evidence, claim models
  store.py           # SQLite persistence and migrations
  orchestrator.py    # resumable state machine and budgets
  providers.py       # search-provider interface and implementations
  fetcher.py         # safe HTTP fetch, redirects, limits, cache
  normalize.py       # HTML, RSS, text, and PDF extraction
  rank.py            # dedupe, domain quality, recency, embedding rerank
  extract.py         # chunking and atomic evidence extraction
  coverage.py        # subquestion coverage and contradiction matrix
  synthesize.py      # outline and section-by-section drafting
  citations.py       # claim/citation rendering and verification
  prompts.py         # versioned, small task prompts
  events.py          # progress event types
```

Add endpoints to `service/main.py`:

| Endpoint | Purpose |
|---|---|
| `POST /research/jobs` | Draft a plan and create a paused job |
| `GET /research/jobs/{id}` | Current state, plan, counts, and report metadata |
| `PATCH /research/jobs/{id}/plan` | Edit plan before or during the run |
| `POST /research/jobs/{id}/start` | Approve/start the plan |
| `POST /research/jobs/{id}/steer` | Add a persisted direction change |
| `POST /research/jobs/{id}/pause` | Pause after the current atomic operation |
| `POST /research/jobs/{id}/cancel` | Cancel without deleting accumulated work |
| `GET /research/jobs/{id}/events` | SSE progress stream with replay cursor |
| `GET /research/jobs/{id}/report` | Structured report and citations |
| `GET /research/jobs/{id}/export.md` | Downloadable Markdown report |

Use one global foreground-model gate so Research does not contend with normal chat or the daily brief. When a normal Wisp turn begins, Research should finish its current model call and pause model work; network fetches may continue.

### Search providers

Use a provider interface from the first commit:

```python
class SearchProvider(Protocol):
    async def search(self, query: str, *, limit: int,
                     domains: list[str], recency_days: int | None) -> list[SearchHit]: ...
```

Recommended order:

1. **MVP:** DuckDuckGo HTML, since Wisp already documents it as the no-key fallback. Parse the result page deterministically; do not ask Ornith to parse search HTML.
2. **Preferred optional provider:** a user-configured search API with stable structured results. Store its key in macOS Keychain, not YAML.
3. **Privacy-oriented option:** user-configured SearXNG endpoint.

Search privacy must be explicit: model inference remains local, but the search provider and fetched sites receive the query, IP address, and normal HTTP metadata.

### Fetching and normalization

The current `web_fetch` strips tags with regular expressions and clips at 3,000 characters. Keep it for quick chat lookups; Research needs a different fetch path that preserves document structure and provenance.

For the MVP:

- accept HTML, plain text, RSS/Atom, and text-based PDF;
- retain headings, paragraphs, tables-as-text, canonical URL, title, author, publication date, and retrieval time where available;
- extract PDFs with the existing `pypdf` dependency;
- reject or clearly label scanned/image-only PDFs because Wisp currently has no vision/OCR path;
- cache normalized content by canonical URL plus content hash;
- save the exact normalized offsets used by each evidence record.

Add a readability library only if a measured corpus shows the deterministic in-house extractor is insufficient. Any new runtime dependency must be included in `requirements-runtime.txt` and the packaged import audit.

### Ranking and source quality

Ranking should combine deterministic signals before asking the model:

- exact/near URL dedupe and canonicalization;
- query-term and BM25 relevance;
- embedding relevance using Wisp's existing Qwen3 embedding model;
- publication date fit;
- primary-source preference;
- domain diversity;
- penalties for thin pages, scraped mirrors, affiliate pages, and duplicate syndication.

Do not encode “trusted domain = true” as a universal fact. Store a source class such as primary, official, academic, reputable secondary, community, or unknown, plus the reason for the classification. The model can propose a class, but the report should expose it as metadata rather than treat it as truth.

## 6. Evidence and citation contract

Each extraction call returns atomic records, not a narrative summary:

```json
{
  "source_id": "src_12",
  "subquestion_id": "q_3",
  "claim": "The product entered general availability in May 2026.",
  "supporting_quote": "...entered general availability on May 14, 2026...",
  "start_offset": 1824,
  "end_offset": 1882,
  "published_at": "2026-05-14",
  "stance": "supports",
  "confidence": 0.91
}
```

Host-side validation rejects a record when:

- the quote is not an exact normalized substring of the stored source;
- the offsets do not resolve to that quote;
- the source or subquestion ID is unknown;
- required dates or numbers in the claim are absent from its cited evidence;
- the record repeats an existing evidence hash.

Report drafting uses evidence IDs such as `[E42]`. Python resolves them to stable numbered citations after drafting, so Ornith never invents URLs or citation numbers.

Verification occurs in two passes:

1. **Deterministic pass:** citation exists, quote exists, offsets match, URL is from the source table, and numeric/date tokens are supported.
2. **Small-model critic pass:** given one claim and its cited passages, label it supported, partially supported, contradicted, or unclear. This pass may remove or soften a claim, but it may never add a new factual claim.

Every factual paragraph needs a citation. Unsupported sentences are removed or rewritten as clearly labeled inference. The source drawer must display the evidence passage so the user can audit without re-running the model.

## 7. Persistence

Use `~/.moe/research.db`, separate from sessions and commitments.

Core tables:

- `research_jobs` — prompt, state, mode, budgets, timestamps, error, report version;
- `research_plans` — versioned approved plan and subsequent steering changes;
- `research_subquestions` — status, priority, coverage score;
- `research_queries` — query, provider, reason, timestamp, result count;
- `research_sources` — URL, canonical URL, metadata, content hash, fetch status, quality class;
- `research_chunks` — source-relative text and offsets;
- `research_evidence` — atomic claims, quotes, offsets, stance, validation state;
- `research_events` — append-only progress log and SSE replay cursor;
- `research_sections` — outline nodes and versioned drafts;
- `research_reports` — final Markdown plus structured citation map.

Normalized page text can live in `~/.moe/research_cache/` as content-addressed compressed files. Apply Wisp's existing local permissions convention (`0700` directory, `0600` files).

Default retention: keep report metadata and evidence; remove cached page bodies after 30 days unless the user pins the research job. Provide a visible **Delete research data** action.

## 8. Budgets and stopping rules

Research must have deterministic ceilings. Suggested starting defaults:

| Mode | Queries | Fetched pages | Accepted sources | Model calls | Expected local runtime |
|---|---:|---:|---:|---:|---|
| Quick | 4 | 8 | 3–5 | 8–15 | 2–6 minutes |
| Standard | 10 | 24 | 8–15 | 20–45 | 8–25 minutes |
| Deep | 20 | 50 | 15–30 | 45–90 | 20–60 minutes |

These are initial measurement targets, not promises. The UI should display work counts rather than an inaccurate countdown.

Stop when any of these is true:

- every required subquestion has at least two independent supporting sources, or one authoritative primary source where independence is inapplicable;
- two consecutive search rounds add no new validated evidence;
- the approved query/page/model-call budget is exhausted;
- the user pauses or cancels;
- the remaining gaps require inaccessible, paywalled, authenticated, or image-only material.

The final report should state which stopping condition fired.

## 9. Swift app changes

Add:

```text
app/Sources/WispApp/ResearchModel.swift
app/Sources/WispApp/ResearchView.swift
app/Sources/WispApp/ResearchWindow.swift
```

Extend `WispClient.swift` with job creation, plan editing, event streaming with a cursor, steering, pause/cancel, report loading, and Markdown export.

The main overlay gets a Research toggle and compact active-job chip. A separate resizable window handles the plan, activity log, source list, and report. Research remains accessible after the notch panel closes.

Important UI states:

- drafting plan;
- awaiting approval;
- running with current stage;
- paused by user;
- paused for foreground chat;
- partially complete due to budget/network/access limits;
- complete;
- cancelled, with accumulated evidence retained;
- failed, with a resumable checkpoint where possible.

## 10. Implementation phases

### Phase 0 — qualify the exact model (1–2 days)

Before building around Ornith, add it to a candidate roster and run a dedicated harness against the exact oQ6e abliterated checkpoint.

Measure:

- valid JSON against six nested schemas, 20 repetitions each;
- tool-call parsing with the configured oMLX parser;
- instruction retention across a 24K prompt;
- prompt-injection resistance only as a diagnostic—the host remains the safety boundary;
- quote extraction accuracy and exact-offset success;
- thinking-on/off behavior by task type;
- peak memory and tokens/second at 16K, 24K, and 32K.

**Gate:** at least 98% schema-valid responses after one repair retry, at least 95% exact-quote extraction on the fixture set, and no oMLX memory abort at the chosen working window. If it misses, keep the model but move more planning/ranking into deterministic code and reduce call size.

### Phase 1 — vertical-slice backend (3–5 days)

Implement SQLite jobs, plan approval, DuckDuckGo search parsing, safe fetching, normalization, progress events, and a basic one-pass report over 3–5 sources.

**Gate:** a job survives backend restart, can be paused/cancelled, and produces a Markdown report whose citations all open to real stored sources.

### Phase 2 — evidence ledger and verified citations (4–6 days)

Add chunking, atomic evidence extraction, exact-quote validation, claim IDs, section drafting from evidence only, citation resolution, and source drawer data.

Reuse the principles in `service/search/synth.py`, but generalize verification from one document to many sources.

**Gate:** 100% citation-ID validity, 100% quoted text found in the cited normalized source, and zero invented URLs in adversarial tests.

### Phase 3 — iterative research loop (3–5 days)

Add coverage scoring, contradiction tracking, query refinement, source-quality metadata, domain diversity, stopping rules, and steering.

**Gate:** on a 20-task fixture suite, Standard mode covers every required subquestion on at least 17 tasks and visibly reports gaps on the remainder rather than filling them from model memory.

### Phase 4 — native research UI and export (4–6 days)

Build plan review, progress/activity view, source list, report view, source passage inspection, compact notch status, and Markdown export.

**Gate:** closing/reopening the window loses no progress; a user can change source domains mid-run; every citation can be inspected in two clicks.

### Phase 5 — hardening and release (5–7 days)

Add prompt-injection fixtures, SSRF tests, malformed HTML/PDF tests, network failure recovery, content cache retention, provider fallback, load/energy measurements, and packaging verification.

Run `scripts/package_app.sh` and verify the installed backend copy, because source edits under `service/` do not affect the running app until it is repackaged.

**Gate:** all automated tests pass from both the repository and installed app; no private local string from the injection fixture appears in any outbound search query; cancellation stops within one atomic operation; a normal Wisp chat preempts further Research model work.

Estimated solo implementation: **3–5 weeks** for a dependable MVP. A rough one-week prototype is possible, but it should not be called a research tool until citations and prompt-injection boundaries are enforced.

## 11. Evaluation suite

Create `scripts/eval_research.py` and versioned fixtures under `test_fixtures/research/`.

Test categories:

- current product/company comparison;
- technical documentation with version constraints;
- recent news timeline;
- academic mini-literature review;
- recommendation with explicit criteria;
- conflicting sources;
- thin or unavailable evidence;
- direct URL and PDF research;
- malicious page telling Wisp to ignore instructions or call local tools;
- pages linking to localhost/private IPs;
- duplicated and syndicated articles;
- changed publication dates and stale search snippets;
- backend restart, pause, steer, cancel, and resume.

Track:

- subquestion coverage;
- source precision and primary-source share;
- citation precision and citation coverage;
- exact-quote validity;
- unsupported factual sentence rate;
- contradiction detection recall;
- structured-output repair rate;
- pages/model calls per successful report;
- wall time, energy, and peak memory;
- user-visible failure quality.

Release bars for Standard mode:

- 100% valid citation targets;
- 100% quoted passages trace to stored source text;
- at least 90% of externally verifiable factual sentences cited;
- under 5% unsupported factual sentences in blind human review;
- at least 80% primary or reputable secondary sources when such sources exist;
- no execution of an instruction originating in fetched content;
- no silent completion when a required subquestion lacks evidence.

## 12. Recommended MVP scope

Ship these first:

- explicit Research mode;
- editable plan;
- open web plus direct URLs;
- HTML, RSS, text PDF, and attached text documents;
- Quick and Standard depths;
- live activity and source list;
- pause, cancel, resume, and steer;
- evidence-backed report with clickable citations;
- Markdown export;
- local persistent job history.

Defer:

- authenticated sites and browser sessions;
- remote MCP/connectors;
- scanned PDF OCR and image understanding;
- Word/PDF export;
- research across private Mail/Messages by default;
- multiple simultaneous research jobs;
- parallel model workers;
- claims of “hundreds of sources.”

## 13. How to use it after the MVP ships

1. Open Wisp and turn on **Research**.
2. Give it a decision-oriented request with scope, timeframe, and output, for example:

   > Compare the best local-first research-agent architectures for a 24 GB Apple Silicon Mac in 2026. Prioritize primary documentation and measured benchmarks. Recommend an architecture for Wisp, include tradeoffs, and cite every technical claim.

3. Review the generated subquestions and allowed sources. Remove anything irrelevant and set Quick or Standard.
4. Start the job and watch the evidence/source count. Use **Steer** if it follows a weak branch.
5. Open important citations from the report and inspect the stored supporting passages.
6. Export Markdown or ask normal Wisp chat to summarize the completed report for a different audience.

For the Ornith 9B model, specific prompts will outperform vague ones. Include the decision you are making, date range, geography, required source types, comparison criteria, and desired output. The workflow should still turn vague prompts into a reasonable editable plan, but it should not pretend a missing scope never mattered.

## 14. Final recommendation

Start with Phase 0 and Phase 1, but architect the evidence ledger and resumable job state from day one. The make-or-break feature is not the number of searches; it is that every important sentence can be traced to a real passage and that an untrusted webpage cannot steer an abliterated local model into Wisp's action tools.

If those two properties hold, Ornith 1.5 9B oQ6e is large enough to provide a useful Claude/ChatGPT-like research experience through decomposition and staged synthesis. If they do not, a larger model will only make the failures sound more convincing.
