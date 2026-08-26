"""T3 — the grounded answer, plus the verification pass that keeps it honest.

Two rules, both load-bearing:

1. **Never swap models.** Synthesis runs on whatever is ALREADY resident. If
   the agent model is loaded it writes the answer; if the summarizer is loaded the summarizer writes it
   (reading three paragraphs and answering needs no tool calling, which is the
   only thing the summarizer was ever unreliable at). Only if nothing at all is resident
   do we load the summarizer — the smallest, fastest cold start. Search must never be
   the reason a 12.7GB model gets pulled in. Mirrors brief.py's convention.

2. **Grounded or silent.** Every claim is checked against the retrieved text
   after generation. Anything unverifiable is dropped, and if nothing survives
   the answer degrades to NOT_FOUND rather than guessing. Hallucinating a
   detail into the user's own document destroys the feature permanently.
"""
from __future__ import annotations

import asyncio
import re

from service.config import no_thinking_kwargs, role_to_model
from service.inference.omlx_client import OMLXClient
from service.search.chunker import Chunk
from service.search.embedder import embedding_model
from service.search.lexical import focus_span, fold, query_terms

NOT_FOUND = "NOT_FOUND"

_SYSTEM = (
    "You answer questions about a document the user is looking at.\n"
    "Use ONLY the numbered passages provided. Do not use outside knowledge.\n"
    "Cite the passage for every claim with [n], e.g. 'The dog was hazel [2].'\n"
    "Quote distinctive wording from the passages exactly as written.\n"
    "Be direct and brief — two or three sentences at most. If the question has "
    "several parts, answer each part.\n"
    f"If the passages do not contain the answer, reply with exactly {NOT_FOUND} "
    "and nothing else."
)

# Whole-document questions ("what is this book about", "who wrote this") get a
# different contract. The passages here are a STRUCTURAL SAMPLE of the document
# in reading order — front matter plus a spread — not passages selected for
# containing the answer, so the old prompt's "if the passages don't contain the
# answer, say NOT_FOUND" was exactly wrong: no single passage ever states what a
# novel is "about", and the model would correctly-but-uselessly refuse every
# time. Here the job is to INFER from the sample. The honesty guarantee is
# preserved where it actually matters — quoted wording must still be real (see
# `verify`) — and NOT_FOUND stays available for genuinely unreadable input.
_SYSTEM_GLOBAL = (
    "You answer questions about a document the user is looking at, using "
    "excerpts from it.\n"
    "The numbered passages are a sample from across the document in reading "
    "order: the opening, the passages most relevant to the question, and points "
    "throughout. They are NOT guaranteed to state the answer outright, so "
    "REASON from the evidence — infer, connect passages, and draw the "
    "conclusion the excerpts support.\n"
    "ANSWER THE QUESTION THAT WAS ASKED. If asked what something is, explain "
    "what the passages show it to be. If asked who a person is, say what the "
    "text reveals about them. Do not answer a different, easier question, and "
    "do not merely describe what the document is unless that IS the question.\n"
    "Lead with the answer. Never open with 'the passages mention' or 'this "
    "document appears to be' unless the user asked what the document is.\n"
    "Cite passages as [n] where a specific detail comes from one.\n"
    "Any wording you put in quotation marks must appear exactly in a passage. "
    "Never invent titles, authors, names, or dates that are not in the text.\n"
    "Be substantive but concise — three or four sentences.\n"
    "If the excerpts genuinely don't support an answer, say briefly what they "
    "DO show that's related, rather than refusing outright.\n"
    f"Only if the passages are unreadable or entirely unrelated, reply with "
    f"exactly {NOT_FOUND} and nothing else."
)

# Sentence splitter for the verification pass. Keeps the citation marker with
# the sentence it belongs to.
_SENT_RE = re.compile(r"(?<=[.!?])\s+")
# Matches a whole citation GROUP, so "[1, 2, 3]" is one marker carrying three
# numbers rather than three unparseable ones. Models emit the grouped form
# constantly, and the old single-number pattern silently matched none of it —
# those answers rendered with no clickable citations at all.
_CITE_RE = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")
_NUM_RE = re.compile(r"\d+")
_QUOTED_RE = re.compile(r'"([^"]{4,})"')

# Models reach for non-ASCII bracket and quote forms surprisingly often —
# the agent model emits fullwidth 【1】 for citations, and curly quotes throughout.
# Unnormalized, the citation is unparseable (so the chip vanishes and the raw
# marker leaks into the visible answer) and a genuine quotation fails the
# verbatim check against straight-quoted source text.
_PUNCT_FOLD = str.maketrans({
    "【": "[", "】": "]", "［": "[", "］": "]",
    "“": '"', "”": '"', "‘": "'", "’": "'",
})


def _normalize_punct(s: str) -> str:
    return s.translate(_PUNCT_FOLD)

# Name-shaped tokens, for the global-scope proper-noun check below.
_PROPER_RE = re.compile(r"\b([A-Z][a-z]{2,})\b")
# Capitalized words that routinely appear mid-sentence in a summary without
# being lifted from the document — checking these would drop good answers.
_GENERIC_CAPS = {
    "The", "This", "That", "These", "Those", "It", "Its", "They", "There",
    "Here", "What", "When", "Where", "Which", "Who", "Whose", "How", "Why",
    "And", "But", "For", "Not", "Set", "Part", "Chapter", "Published",
    "Literary", "Fiction", "Nonfiction", "Novel", "Book", "Document", "Page",
    "Article", "Author", "Story", "Overview", "Summary", "Section",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
}


def _invents_proper_noun(sentence: str, sources: str) -> bool:
    """Whether `sentence` introduces a name-shaped word absent from the source.

    The loosened global-scope rule (uncited summary sentences are allowed)
    otherwise leaves one high-severity hole: an invented author, title, or
    character name, unquoted, reads as authoritative and is exactly the kind of
    thing a user would believe and repeat. Quotation checking can't catch it
    because the model never quoted anything. Proper nouns are cheap to check
    deterministically and are where fabrication actually shows up.

    Deliberately narrow: skips the sentence's first word (always capitalized)
    and a stoplist of generic capitalized words, so ordinary summary prose
    isn't dropped for saying "Literary" or "Chapter".
    """
    words = sentence.split()
    for tok in _PROPER_RE.findall(" ".join(words[1:])):
        if tok in _GENERIC_CAPS:
            continue
        if fold(tok) not in sources:
            return True
    return False


async def pick_model(client: OMLXClient, *, prefer_capable: bool = False,
                     allow_load: bool = False) -> tuple[str, bool]:
    """Choose the synthesis model. Returns (model, needs_load).

    `allow_load` is the one place this feature will pay for a model swap, and
    it's a deliberate exception to the "never swap for search" rule the rest of
    the pipeline follows. That rule exists so FIND stays instant — it is
    correct for T0-T2 and for span answers, which must feel like ⌘F.

    It is wrong for "summarize this book". That request is not a keystroke; the
    user has explicitly asked for reasoning across a long document and is
    already waiting seconds. Answering it on a 4B model produces exactly the
    hedging non-answers ("the provided excerpts do not contain...") that make
    the feature feel broken. Here the stronger model is the whole point, so it
    is worth loading — and only here.
    """
    fast = role_to_model("fast")
    capable = role_to_model("agent")
    non_chat = {embedding_model()}
    try:
        loaded = await client.loaded_models()
    except Exception:  # noqa: BLE001 — a status hiccup must not fail the search
        return fast, False
    candidates = [m for m in loaded if m not in non_chat]
    if prefer_capable and capable in candidates:
        return capable, False
    if prefer_capable and allow_load and capable:
        return capable, True          # worth the swap — see docstring
    if fast in candidates:
        return fast, False
    return (candidates[0] if candidates else fast), False


async def _pick_model(client: OMLXClient, *, prefer_capable: bool = False) -> str:
    """Whatever chat model is resident, preferring the small fast one.

    "Resident" is not the same as "usable for chat": after Smart Search or
    semantic tool retrieval has run, the embedder may be in loaded_models(),
    and oMLX rejects it on /v1/chat/completions ("is not an LLM / chat model").
    Anything that isn't a chat model has to be filtered out here or synthesis
    picks it and 400s.

    `prefer_capable` is the long-document case: the summarizer is entirely capable of
    reading a handful of short passages, but a broad question over a
    novel-length document needs more passages considered at once than a small
    model's context window comfortably holds. If the `agent` role model
    (the agent model, ~131K context) is ALREADY resident, use it instead — never load
    it for this: that would be exactly the swap-for-search cost this whole
    design avoids. On a long document the odds are decent it's already
    resident anyway (it's the default agent/coding/reasoning model), so this
    mostly matters on the sessions where it actually helps.
    """
    fast = role_to_model("fast")
    capable = role_to_model("agent")
    non_chat = {embedding_model()}
    try:
        loaded = await client.loaded_models()
    except Exception:  # noqa: BLE001 — a status hiccup must not fail the search
        return fast
    candidates = [m for m in loaded if m not in non_chat]
    if prefer_capable and capable in candidates:
        return capable
    if fast in candidates:
        return fast
    return candidates[0] if candidates else fast


def _render_passages(chunks: list[Chunk], picks: list[int]) -> str:
    out = []
    for n, idx in enumerate(picks, start=1):
        body = " ".join(chunks[idx].text.split())
        out.append(f"[{n}] {body}")
    return "\n\n".join(out)


def verify(answer: str, chunks: list[Chunk], picks: list[int], *,
           scope: str = "span") -> tuple[str, list[int]]:
    """Drop every sentence we can't trace back to the retrieved text.

    Deterministic and microsecond-cheap. Two rules, and which apply depends on
    what was asked:

    - **Fabricated quotation** — any text in quotation marks must appear in the
      passages. Enforced in BOTH scopes; this is the guarantee that matters
      most, and the one that keeps a made-up title or name out of the answer.
    - **Missing citation** — in `span` scope a sentence with no [n] has no
      provenance and is dropped. In `global` scope it is kept: a summary
      sentence ("it follows a clerk in a surveillance state") is a legitimate
      inference across the whole sample rather than a claim lifted from one
      passage, and dropping it turned every document-level question into a
      false "not in this document".
    """
    if answer.strip() == NOT_FOUND:
        return NOT_FOUND, []

    # Both sides get the same punctuation fold: the model writes straight
    # quotes and apostrophes while a typeset PDF carries curly ones, so
    # comparing them raw drops correctly-quoted sentences as "fabricated".
    def _src(text: str) -> str:
        return fold(_normalize_punct(text))

    all_sources = _src(" ".join(chunks[i].text for i in picks))
    kept: list[str] = []
    used: list[int] = []
    for sent in _SENT_RE.split(answer.strip()):
        s = sent.strip()
        if not s:
            continue
        cites = [int(n) for grp in _CITE_RE.findall(s) for n in _NUM_RE.findall(grp)]
        valid = [n for n in cites if 1 <= n <= len(picks)]
        if not valid and scope != "global":
            continue  # uncited claim — no provenance, no place in the answer

        # Quotes are checked against the cited passages when there are any,
        # else against the whole retrieved set (global summary sentences may
        # quote a passage without numbering it).
        sources = (_src(" ".join(chunks[picks[n - 1]].text for n in valid))
                   if valid else all_sources)
        quotes = _QUOTED_RE.findall(s)
        if any(_src(q) not in sources for q in quotes):
            continue  # fabricated quotation
        # Global scope allows uncited sentences, so it needs the extra guard
        # against invented names; span scope already required a citation.
        if scope == "global" and _invents_proper_noun(s, all_sources):
            continue

        kept.append(s)
        used.extend(picks[n - 1] for n in valid)

    if not kept:
        return NOT_FOUND, []
    # Dropping a leading sentence can leave the next one opening on a
    # connective ("Furthermore, the text mentions...") that now refers to
    # nothing. Cheap to clean up, and the alternative reads like a bug.
    kept[0] = re.sub(
        r"^(?:furthermore|however|additionally|moreover|also|in addition|"
        r"but|and|then|second(?:ly)?|finally)\b[,:]?\s*",
        "", kept[0], flags=re.I)
    if kept[0]:
        kept[0] = kept[0][0].upper() + kept[0][1:]
    # Preserve first-seen order without duplicates.
    seen: list[int] = []
    for u in used:
        if u not in seen:
            seen.append(u)
    return " ".join(kept), seen


# A broad question over a long document ("what changes over the course of the
# book") needs more supporting material than a short document's 5 passages —
# both the passage budget and the answer length scale up together for it.
_LONG_DOC_MAX_PASSAGES = 10
# Global scope must not be truncated below what engine._global_picks composed
# (front matter + matched + spread) — clipping it back to 10 would throw away
# exactly the retrieval hits that allocation was added to protect.
_GLOBAL_MAX_PASSAGES = 15
_LONG_DOC_MAX_TOKENS = 700
_SHORT_DOC_MAX_TOKENS = 400
# Wall-clock ceiling on one synthesis call, model load excluded (that's awaited
# separately, with its own progress event). Generous enough for a long overview
# on a big model, short enough that a runaway generation can't hang the panel.
_ANSWER_TIMEOUT_S = 90.0


async def answer(client: OMLXClient, question: str, chunks: list[Chunk],
                 picks: list[int], *, max_passages: int = 5,
                 long_doc: bool = False, scope: str = "span",
                 upgrade_model: bool = False,
                 on_progress=None) -> dict:
    """Compose a verified answer. Returns {} when there's nothing to say.

    `long_doc` widens the passage budget and reply length, and lets a
    resident (never freshly-loaded) larger-context model take the answer —
    see `_pick_model`. `scope` selects the contract: span-level lookup vs.
    whole-document overview (see `_SYSTEM_GLOBAL`).
    """
    if scope == "global":
        max_passages = max(max_passages, _GLOBAL_MAX_PASSAGES)
    elif long_doc:
        max_passages = max(max_passages, _LONG_DOC_MAX_PASSAGES)
    picks = picks[:max_passages]
    if not picks:
        return {}

    model, needs_load = await pick_model(
        client, prefer_capable=long_doc or scope == "global",
        allow_load=upgrade_model)
    if needs_load:
        if on_progress:
            await on_progress(model)
        try:
            # Honors keep_warm, so the small resident model survives and the
            # next quick search doesn't pay a reload.
            await client.ensure_only(model)
        except Exception:  # noqa: BLE001 — fall back rather than fail the answer
            model, _ = await pick_model(client, prefer_capable=False)
    passages = _render_passages(chunks, picks)
    messages = [
        {"role": "system",
         "content": _SYSTEM_GLOBAL if scope == "global" else _SYSTEM},
        {"role": "user", "content": f"{passages}\n\nQuestion: {question}"},
    ]
    max_tokens = _LONG_DOC_MAX_TOKENS if long_doc else _SHORT_DOC_MAX_TOKENS
    # THINKING OFF, not reasoning_effort="low".
    #
    # The intent below was always right — reading a dozen passages and
    # reporting what they say needs no deliberation — but the mechanism was
    # dead. `reasoning_effort` came from gpt-oss's template and gpt-oss is
    # unrostered; Agents-A1 is qwen3 lineage and has no such variable, so this
    # line was silently doing nothing and the model thought at full length
    # into a 400-token ceiling. (config.effort_kwargs, which used to carry
    # reasoning_effort, was removed entirely once nothing rostered read it.)
    #
    # That is not a slow answer, it is NO answer. Measured 2026-08-09 on this
    # call's own prompt shape at _SHORT_DOC_MAX_TOKENS:
    #     reasoning_effort="low"  6.4s, 400 completion tokens,
    #                             finish_reason=length, content 0 chars
    #     enable_thinking=False   0.7s,  24 completion tokens,
    #                             finish_reason=stop, a correct cited answer
    # With content empty, `if not raw` below returns {"error": "empty
    # completion"} — so every Cmd+Shift+F was burning its whole ceiling to
    # produce an error string. Same failure and same fix as the profile MAP
    # pass (memory/profile._map_extract) and the rolling conversation summary
    # (memory/context.maybe_summarize).
    extra: dict = dict(no_thinking_kwargs(model))
    # One retry: oMLX answers 400 while it's mid-load, and search deliberately
    # races the embedder's first load against this call. That transient must
    # not surface as a failed answer when a moment's wait would have worked.
    raw = ""
    last_err = ""
    for attempt in range(2):
        try:
            # Hard ceiling regardless of what the model does. Synthesis is the
            # OPTIONAL tier — the retrieval results are already on screen, so
            # abandoning a runaway generation degrades to "no answer" instead
            # of an indefinitely spinning panel.
            resp = await asyncio.wait_for(
                client.chat(model, messages, max_tokens=max_tokens,
                            **extra),
                timeout=_ANSWER_TIMEOUT_S)
            raw = (resp["choices"][0]["message"].get("content") or "").strip()
            break
        except asyncio.TimeoutError:
            last_err = "answer timed out"
            break                      # retrying a timeout just doubles the wait
        except Exception as e:  # noqa: BLE001 — synthesis is the optional tier
            last_err = str(e)
            if attempt == 0:
                await asyncio.sleep(1.5)
    if not raw:
        return {"error": last_err or "empty completion"}

    raw = re.sub(r"(?is)<think>.*?</think>", "", raw).strip()
    raw = _normalize_punct(raw)
    text, used = verify(raw, chunks, picks, scope=scope)
    if text == NOT_FOUND:
        return {"not_found": True, "model": model, "scope": scope}

    # Renumber citations to the order they're actually used, and hand back the
    # char spans so the UI can jump straight to the source.
    renumber = {old: i + 1 for i, old in enumerate(used)}
    def _fix(m: re.Match) -> str:
        out: list[int] = []
        for raw in _NUM_RE.findall(m.group(1)):
            n = int(raw)
            if not (1 <= n <= len(picks)):
                continue
            fixed = renumber.get(picks[n - 1])
            if fixed and fixed not in out:
                out.append(fixed)
        # Drop the marker entirely rather than emitting a bare "[]" — an
        # unresolvable citation should leave no residue in the answer text.
        return "[" + ", ".join(str(n) for n in out) + "]" if out else ""
    text = _CITE_RE.sub(_fix, text)

    # Aim each citation at the sentence that supports the answer rather than at
    # the whole chunk — clicking [1] should land on the claim, not on whatever
    # heading led its chunk. Scored against the answer's own wording, which is
    # closer to the supporting sentence than the question is.
    terms = query_terms(question + " " + text)
    citations = []
    for i, c in enumerate(used):
        fs, fe = focus_span(chunks[c], terms)
        citations.append({
            "n": i + 1, "chunk_idx": c, "start": fs, "end": fe,
            "chunk_start": chunks[c].start, "chunk_end": chunks[c].end,
            "preview": " ".join(chunks[c].text[fs - chunks[c].start:
                                               fe - chunks[c].start].split())[:220],
        })

    return {"text": text, "model": model, "citations": citations, "scope": scope}
