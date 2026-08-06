# Wisp Smart Search — Design

**Problem:** `⌘F` matches strings. It finds `hazel` if you type `hazel`. It cannot answer
*"What were the traits of Joe's dog and what color was he?"* — a question whose answer is
spread across a sentence, phrased differently than the query, and split into two sub-asks.

**Goal:** a find-replacement that answers questions about what's in front of you, in
`⌘F` time, without ever being *worse* than `⌘F`.

Designed 2026-07-27. Not built. Follows the conventions in `ASSISTANT_ARCHITECTURE.md`
(local-first, deterministic code where possible, LLM only for what only it can do,
degrade gracefully).

---

## 1. Design principles

1. **Never lose the floor.** Literal substring matching still happens, still instantly,
   still first. Smart search is strictly additive. If the AI layer is cold, broken, or
   unloaded, you still have `⌘F`.
2. **Answer, then evidence.** The output is a sentence that answers the question, with
   every claim anchored to a span in the source. An answer you can't verify is worse than
   no answer.
3. **Latency is the feature.** Find is muscle memory. Anything that makes you *wait* to
   see the first result has already lost. Results stream in tiers (§4).
4. **Never trigger a model swap.** Search runs on what's already resident or on a tiny
   pinned embedder. A 12.7GB swap (`ensure_only`) to answer "where's the part about the
   dog" is a non-starter on 24GB. Same rule `brief.py` already follows.
5. **Grounded or silent.** If the answer isn't in the text, say "not in this document."
   Hallucinating a dog's color into someone's PDF destroys the feature permanently.

---

## 2. The keybinding question (decide first)

**Do not take `⌘F`.** Carbon `RegisterEventHotKey` (already used in `GlobalHotKey.swift`)
*can* claim `⌘F` system-wide, which would break native find in every app on the machine —
including apps where native find is better (Xcode, terminal, regex-capable editors).

Recommendation:

| Binding | Behavior |
|---|---|
| `⌘F` | untouched, native |
| **`⌘⇧F`** | Wisp Smart Search (default) |
| `⌘F` takeover | opt-in, **per-app allowlist** in Settings → Search |

The takeover is worth building because for the apps where you'd want it (Preview, Notes,
Safari reading, Mail) native find is genuinely useless, and muscle memory is real. But it
must be opt-in and per-app, never global.

**Selection seeding:** if text is selected when you invoke, prefill the query as
*"find passages like this"* rather than an empty box — one keystroke gets you
"find-similar," which `⌘F` can't do at all.

---

## 3. Where the text comes from

This is the hard, unglamorous half of the feature. Wisp is a separate process; it has to
get the focused document's text out of another app.

**Path A — Accessibility (primary).** Walk the focused window's `AXUIElement` tree,
pulling `AXValue` from text areas and `AXStaticText` nodes, in document order, recording
`(node, charRange)` for every emitted span. Gives clean text *including content scrolled
off-screen*, plus — critically — the ability to map an answer back to a screen rectangle
via `kAXBoundsForRangeParameterizedAttribute`.

Costs and caveats, honestly:
- Requires the **Accessibility** TCC permission, which Wisp has so far avoided (the Carbon
  hotkey was chosen specifically to dodge it). This is a new permission prompt.
- Chromium apps need `AXManualAccessibility` set to true before their tree is populated.
- Some apps expose nothing useful (canvas-rendered docs, Figma, many Electron apps).

**Path B — OCR (fallback).** `ScreenCaptureKit` capture of the focused window →
`VNRecognizeTextRequest` (Vision framework, on-device, fast, free, no model download).
Works literally everywhere, needs Screen Recording permission, but only sees the visible
viewport and loses reading order in complex layouts.

**Path C — file-native (best when available).** If the focused window's `AXDocument`
attribute resolves to a real file path (PDF, .md, .txt, .docx), parse the file directly.
Full text, perfect fidelity, no permission beyond file read.

**Resolution order: C → A → B**, decided per invocation, with the chosen path shown as a
tiny label in the search bar ("reading: document" / "reading: window" / "reading: screen")
so a degraded result is never mysterious.

**Do not use the VLM here.** `Qwen3-VL-8B` costs a 5.8GB swap and seconds of latency to do
worse OCR than the Vision framework does in ~80ms. The VLM's place is describing *images*
inside the document, as an optional enrichment pass (§8), not reading text.

---

## 4. The tiered pipeline

The whole design hangs on this: **four tiers, rendered progressively, each one usable on
its own.** You see something at every stage; the box never sits empty.

```
keystroke ──▶ T0 literal          ~1ms    exact substring, case/diacritic folded
          ──▶ T1 lexical          ~10ms   BM25 + stems + fuzzy + synonyms
   200ms ────▶ T2 semantic        ~120ms  embedding search over chunks
   pause ────▶ T3 synthesis       ~1-2s   grounded answer with citations
```

**T0 — literal.** Exactly `⌘F`. Runs on every keystroke against the extracted text.
Zero dependencies, zero model. This is the safety floor from §1.

**T1 — lexical.** In-memory BM25 over the same chunks, with Porter stemming, a small
static synonym map, and bounded Levenshtein for typos. Catches `traits` → `trait`,
`colored` → `color`. Deterministic, no model, ~10ms for a 100KB document.

**T2 — semantic.** The actual fix for the user's example. Details in §5.

**T3 — synthesis.** Only fires when the query *looks like a question* (§6) and the user
has paused typing ~400ms. Feeds the top-k spans from T1+T2 to the resident model and asks
for an answer, with mandatory span citations. Details in §7.

**Ranking.** T1 and T2 results merge via Reciprocal Rank Fusion (`1/(60+rank)` summed
across lists) — no score normalization needed between BM25 and cosine, which is exactly
the calibration headache RRF exists to avoid. T0 hits pin to the top always.

---

## 5. The semantic layer

**Chunking.** Split the extracted text on structural boundaries (headings, blank lines,
list items), then pack into ~250-token chunks with ~40% overlap. Every chunk carries
`(char_start, char_end)` into the original text — this is what makes citation and
highlighting possible, so it's non-negotiable plumbing, not an optimization.

**Embedding model.** `Qwen3-Embedding-0.6B-4bit-DWQ` (320MB, 1024-dim), served by
**oMLX's own `/v1/embeddings` endpoint**.

> **Revised during build.** The original plan here was `bge-small` via an in-process
> `mlx-embeddings` dependency, on the assumption that oMLX only served chat models. It
> doesn't — oMLX exposes first-class `/v1/embeddings` *and* `/v1/rerank`, and rejects LLMs
> on that endpoint with *"is not an embedding model"*. So the embedder is just another
> oMLX model behind the `OMLXClient` that already exists: **no new Python dependencies, no
> numpy, no bundle bloat.** `/v1/rerank` also means the deferred reranker stage is a drop-in
> later rather than a new subsystem.

Qwen3-Embedding is asymmetric — queries take an instruction prefix, passages go in bare.
Measured on this document's own worked example, the prefix widened the gap between the
answer and the nearest distractor (0.72 vs 0.37, against 0.76 vs 0.43 unprefixed). Better
*separation* is what matters, not raw score.

**Critical placement constraint (still holds, different mechanism).** The embedder must
never be evicted by, or evict, the chat model. It joins `OMLXClient.set_keep_warm()`
alongside gemma — which also exempts it from `idle_unloader`. gemma 5.5GB + gpt-oss 12.7GB
+ embedder 0.32GB = 18.5GB, under the 21.4GB oMLX ceiling.

**The trap this creates:** once the embedder is kept warm it is *always* in
`loaded_models()`, so "synthesize on whatever is resident" (§7) will happily pick the
embedding model and get a 400. Hit during build; `synth._pick_model` filters non-chat
models explicitly. Any future keep-warm non-chat model (a reranker, TTS) needs the same
treatment.

**Index storage.** For a single document, a flat numpy matrix and one matmul — 500 chunks
× 384 dims is ~0.2ms, brute force is *correct here*, an ANN index would be pure overhead.
Cache keyed by `sha256(extracted_text)` in `~/.moe/search_cache.db` so re-invoking on the
same page is instant. LRU-evict at ~200 documents.

---

## 6. Query understanding

*"What were the traits of Joe's dog and what color dog was he"* fails not just because of
vocabulary but because it's **two questions**. One embedding of the whole string is a
blurry average of both asks and may retrieve neither well.

**Decomposition.** Split multi-part questions into sub-queries, retrieve each
independently, union the results. Heuristics first (split on `and what/and how/and when`,
on `?`, on `;`), escalating to the fast model only when heuristics find nothing and the
query is long. Cheap, and it's the single highest-leverage step for questions like this one.

**Intent classification** (deterministic, no model):

| Signal | Mode |
|---|---|
| starts with wh-word / auxiliary, or ends in `?` | **answer** (T3 fires) |
| quoted string, or `regex:` prefix | **literal only** (T0, exact) |
| short noun phrase | **navigate** (ranked spans, no synthesis) |

**Structured filters parsed out before retrieval**, never left to the model: `after:2024`,
`in:code`, `-excluded`, `from:sender`. These are deterministic predicates over chunk
metadata; an LLM interpreting "not" is a reliability regression, not a feature.

**Worked trace of the user's example:**

```
query: "What were the traits of Joe's dog and what color dog was he"
  → intent: answer (wh-word)
  → decompose: ["traits of Joe's dog", "color of Joe's dog"]
  → T1 BM25:   "dog" hits chunk 7 (+ 3 others)
  → T2 embed:  "traits of Joe's dog"  → chunk 7 @ 0.71
               "color of Joe's dog"   → chunk 7 @ 0.68
  → RRF: chunk 7 top
  → T3: "Joe's dog was friendly and outgoing, and hazel in color."
        with [friendly and outgoing][hazel colored] linked to chars 1204-1251
```

Note that T0 and T1 alone would have gotten you *near* the answer — chunk 7 contains
"dog". The semantic layer's real win is documents where the query shares **no** words with
the answer, and the synthesis layer's win is assembling a two-part answer you'd otherwise
read for yourself.

---

## 7. Synthesis, and not lying

T3 prompt shape — retrieved spans only, no world knowledge, mandatory citation:

```
Answer the question using ONLY the numbered passages below. Quote exactly.
Every sentence must cite a passage as [n]. If the passages do not contain the
answer, reply exactly: NOT_FOUND.
```

**Model choice: whatever chat model is already resident.** Same convention as `brief.py`
§3.5. If `gpt-oss-20b` is loaded, it writes the answer. If `gemma-4-e4b` is loaded, it
writes the answer — it's entirely capable of "read these three paragraphs and answer", and
tool-calling (the one thing gemma was unreliable at) is not involved. If nothing is
resident, load `gemma` (5.5GB, fastest cold start), never `gpt-oss`. Search must never be
the reason a 12.7GB model gets swapped in. Non-chat models are filtered out first — see the
trap noted in §5.

**Verification pass (deterministic, post-generation).** For every quoted string in the
answer, assert it appears verbatim in the cited chunk. Drop any sentence that fails, and
if all sentences fail, degrade to showing ranked spans with no answer. This is a string
containment check — it costs microseconds and it is the difference between a feature you
trust with your documents and one you don't.

**`NOT_FOUND` is a first-class result**, rendered as "Not in this document — search wider?"
with a one-key scope escalation (§9). Far better than a confident wrong answer.

---

## 8. UI

Fits the existing frosted-glass overlay language (`OverlayPanel.swift`, `Theme.swift`).

- **Search bar** — frosted capsule anchored under the notch, deliberately *not* where the
  host app draws its own find bar, so the two never visually collide during `⌘F` takeover.
- **Answer card** — top of results, appears when T3 completes, with citation chips. `↵`
  jumps to the primary citation; `⌘C` copies the answer with source quote.
- **Result list** — spans below, each showing ±1 line of context, literal hits marked
  distinctly from semantic ones (a small icon, not color alone). `↑↓` walks them, and each
  step scrolls the *host app* to that span via AX.
- **In-place highlight** — a borderless click-through `NSWindow` overlaying the host window,
  drawing highlight rects from `kAXBoundsForRangeParameterizedAttribute`. Where the app
  doesn't provide bounds (most browsers), skip the overlay silently and rely on the panel's
  own context preview. Never fake a highlight in the wrong place.
- **Tier indicator** — a hairline progress state showing which tiers have landed, so a
  slow synthesis reads as "still thinking" rather than "broken."
- **Follow-up** — the search box accepts a second query that refines the first
  ("only the ones before 2023"), reusing the retrieved set. This is where search quietly
  becomes conversation, and it's the natural bridge to the existing chat panel.

---

## 9. Scope escalation — the idea worth stealing

The single biggest upgrade over `⌘F` isn't semantics, it's that **the search box doesn't
have to stop at the window boundary.** One key (`⇥`) widens scope:

```
this document  →  this app's open windows/tabs  →  recent documents  →  everything Wisp knows
```

That last rung is nearly free, because it already exists: the commitments store
(`~/.moe/assistant.db`), the session transcripts (`~/.moe/sessions.db`), and the
`search_commitments` agent tool are all built. Smart Search becomes the front door to
Wisp's whole memory, reached by a keystroke you already press twenty times a day.

**Optional rung, opt-in, off by default: a rolling index of documents you've had open.**
Same extraction pipeline, embeddings written to the cache, text retained for N days. This
is the "I read something about hazel dogs last Tuesday" case, and it is by far the most
valuable version of the feature — and the most privacy-loaded. If built:
per-app exclusion list defaulting to password managers, banking, private browsing, and
Messages; a visible indicator whenever indexing is active; one-click "forget the last
hour"; and everything stays in `~/.moe` per §8 of the assistant architecture. Ship the
first three rungs before touching this one.

---

## 10. Other ideas worth considering

- **Answer types beyond prose.** If retrieved spans are tabular or numeric, render a small
  table or a computed value ("3 invoices, $4,210 total") instead of a paragraph. The
  question "how much did I spend" wants arithmetic, not a quote.
- **Find-similar.** With text selected, invoke → ranked passages semantically near the
  selection. Impossible with `⌘F`, trivial once chunks are embedded.
- **Image content.** Optional enrichment: for documents with figures, run the VLM *once at
  index time* (idle-queued, exactly like the mail extractor in §3.2 of the assistant
  architecture) to caption images, and embed the captions as chunks. Then "the chart where
  revenue dips" becomes findable. Never on the hot path.
- **Cross-language.** A multilingual embedder (`bge-m3`, ~2GB — too big to pin, but
  loadable on demand) lets an English query hit Spanish text. Nice, niche; skip for v1.
- **Query telemetry, locally.** Log which tier produced the clicked result. If T0/T1 wins
  90% of the time, the semantic layer is dead weight for how you actually work and should
  be tuned or trimmed. Local-only, inspectable, deletable.
- **Explain-why.** Hover a semantic result to see *why* it matched ("matched: traits ≈
  friendly, outgoing"). Retrieval that can't explain itself feels like a slot machine.
- **Degraded-mode honesty.** When only OCR is available, say "reading visible screen only"
  in the bar. Users forgive limits they can see and resent limits they discover.

---

## 11. Build phases

| Phase | Scope | Outcome |
|---|---|---|
| **S0** ✅ | AX + PDFKit + OCR extraction, chunking with char offsets, T0 literal, `⌘⇧F` binding, overlay panel, AX scroll-to | A find bar that works across apps. |
| **S1** ✅ | BM25 (T1) with stems + typo recovery, RRF merge, result list | Beats `⌘F` on stems, typos, ranking. No model. |
| **S2** ✅ | Keep-warm Qwen3 embedder, chunk index + cache, T2 semantic | The vocabulary-gap fix. |
| **S3** ✅ | Intent classification, query decomposition, T3 synthesis on the resident model, verification pass, citation chips | The motivating example works end to end. |
| **S4** | Scope escalation into commitments + sessions; in-place highlight rects | Search becomes Wisp's front door. |
| **S5** *(gated on a separate conversation)* | Rolling document history index | "I read it last Tuesday." |

**S0–S3 built 2026-07-27.** Verified end to end against a multi-section test document:
the two-part dog question decomposes, retrieves, and answers with an exact citation;
*"What is Joe's dog's name?"* returns `NOT_FOUND` in 333ms rather than inventing one;
"which resident is generous with plants" finds the right sentence with zero shared
vocabulary. T2 runs ~21ms warm. With oMLX stopped entirely, T0/T1 still answer in 1ms and
T2 reports `semantic_unavailable` — the degradation path in §1 confirmed under real
failure, not just designed for.

S0 and S1 ship real value with zero model dependency and zero memory cost — worth having
in your hands before committing to the AI layers, and they're the layers that keep working
when everything else is unloaded.

---

## 12. Open decisions

1. **Accessibility permission** — acceptable to request? Without it, S0 is OCR-only and
   noticeably worse. This is the one blocking question.
2. **`⌘F` takeover** — ship the per-app opt-in in S0, or defer and live on `⌘⇧F` first?
3. **History index (S5)** — build at all? It's the most valuable and most sensitive piece,
   and it deserves its own design conversation rather than a paragraph here.
4. **Embedder pinning** — 130MB permanently resident, outside oMLX, exempt from
   `idle_unloader`. Confirm that's an acceptable standing cost.
