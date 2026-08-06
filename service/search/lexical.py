"""T0 literal + T1 lexical retrieval. No model, no network, pure stdlib.

These are the tiers that keep working when oMLX is down, the embedder failed to
load, or the machine is under memory pressure — so they carry no dependency on
anything in service.inference. T0 in particular is the ⌘F floor from the design
doc: if it can find it, it finds it, in about a millisecond.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from service.search.chunker import Chunk

_WORD_RE = re.compile(r"[a-z0-9]+")

# Deliberately tiny and hand-picked. A big synonym table starts firing on the
# wrong sense of a word and quietly poisons ranking; the embedder (T2) is the
# real answer to vocabulary mismatch, and it's better at it than any list.
_SYNONYMS = {
    "trait": {"characteristic", "quality", "attribute", "personality", "temperament"},
    "color": {"colour", "shade", "hue"},
    "cost": {"price", "expense", "fee", "charge"},
    "buy": {"purchase", "acquire"},
    "doc": {"document", "file"},
    "pic": {"picture", "image", "photo"},
    "car": {"vehicle", "automobile"},
    "dog": {"puppy", "canine", "pup"},
    "cat": {"kitten", "feline"},
}
# Reverse map so a query word hits the canonical term's expansions too.
_SYN_INDEX: dict[str, set[str]] = {}
for _k, _vs in _SYNONYMS.items():
    for _w in {_k} | _vs:
        _SYN_INDEX.setdefault(_w, set()).update({_k} | _vs)

_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "is", "was", "were", "are", "be", "been",
    "to", "in", "on", "at", "for", "with", "as", "by", "it", "its", "this", "that",
    "these", "those", "what", "which", "who", "whom", "how", "when", "where", "why",
    "did", "do", "does", "he", "she", "they", "them", "his", "her", "their", "i",
}


def fold(text: str) -> str:
    """Lowercase + strip diacritics, LENGTH-PRESERVING so char offsets survive.

    NFD would expand 'é' into two code points and shift every offset after it,
    so combining marks are replaced in place rather than dropped.
    """
    out = []
    for ch in text:
        d = unicodedata.normalize("NFD", ch)
        base = d[0] if d else ch
        out.append(base.lower())
    return "".join(out)


def stem(word: str) -> str:
    """Cheap suffix stripping. Not linguistically correct, just consistent —
    all that matters is that query and document stem to the same thing."""
    for suf, keep in (("ingly", 0), ("edly", 0), ("ing", 3), ("ies", 1), ("ied", 1),
                      ("es", 2), ("ed", 2), ("ly", 2), ("s", 1)):
        if len(word) > len(suf) + 2 and word.endswith(suf):
            base = word[: -len(suf)]
            if suf == "ies" or suf == "ied":
                return base + "y"
            if keep == 3 and len(base) > 2 and base[-1] == base[-2]:
                base = base[:-1]  # running -> run
            return base
    return word


def tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(fold(text))


def _expand(terms: list[str]) -> list[str]:
    """Query terms + synonyms, all stemmed, stopwords dropped."""
    out: list[str] = []
    for t in terms:
        if t in _STOPWORDS:
            continue
        out.append(stem(t))
        for syn in _SYN_INDEX.get(t, ()):
            out.append(stem(syn))
    return out or [stem(t) for t in terms]


def _within_edit1(a: str, b: str) -> bool:
    """True if `a` and `b` are within one edit. Bounded and cheap — full
    Levenshtein over every term × every vocab word is not worth it here."""
    if abs(len(a) - len(b)) > 1:
        return False
    if a == b:
        return True
    if len(a) == len(b):
        diffs = sum(1 for x, y in zip(a, b) if x != y)
        return diffs == 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] != long[j]:
            if skipped:
                return False
            skipped = True
            j += 1
            continue
        i += 1
        j += 1
    return True


@dataclass
class Hit:
    chunk_idx: int
    score: float
    # Char span in the ORIGINAL text that actually matched, for highlighting.
    start: int
    end: int
    kind: str  # 'literal' | 'lexical'


def literal_hits(text: str, query: str, *, limit: int = 200) -> list[Hit]:
    """T0 — exactly what ⌘F does. Case- and diacritic-insensitive substring."""
    q = fold(query).strip()
    if not q:
        return []
    hay = fold(text)
    out: list[Hit] = []
    pos = hay.find(q)
    while pos != -1 and len(out) < limit:
        out.append(Hit(-1, 1.0, pos, pos + len(q), "literal"))
        pos = hay.find(q, pos + max(1, len(q)))
    return out


class BM25:
    """Standard BM25 over chunks, built once per document and cached with it."""

    K1 = 1.2
    B = 0.75

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.docs: list[Counter] = []
        self.lens: list[int] = []
        df: Counter = Counter()
        for c in chunks:
            toks = [stem(t) for t in tokenize(c.text)]
            tf = Counter(toks)
            self.docs.append(tf)
            self.lens.append(len(toks))
            df.update(tf.keys())
        self.n = max(1, len(chunks))
        self.avglen = (sum(self.lens) / self.n) if self.n else 1.0
        self.idf = {
            t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }
        self.vocab = set(df)

    def search(self, query: str, *, limit: int = 30) -> list[Hit]:
        terms = _expand(tokenize(query))
        if not terms:
            return []
        # Typo recovery: a query term absent from the document's vocabulary is
        # allowed to stand in for a within-one-edit term that IS present.
        resolved: list[str] = []
        for t in terms:
            if t in self.vocab:
                resolved.append(t)
                continue
            near = [v for v in self.vocab if _within_edit1(t, v)]
            resolved.extend(near[:2] or [t])

        scored: list[tuple[float, int]] = []
        for i, tf in enumerate(self.docs):
            score = 0.0
            dl = self.lens[i] or 1
            for t in resolved:
                f = tf.get(t)
                if not f:
                    continue
                idf = self.idf.get(t, 0.0)
                denom = f + self.K1 * (1 - self.B + self.B * dl / self.avglen)
                score += idf * (f * (self.K1 + 1)) / denom
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)

        out: list[Hit] = []
        for score, i in scored[:limit]:
            c = self.chunks[i]
            s, e = self._best_span(c, resolved)
            out.append(Hit(i, score, s, e, "lexical"))
        return out

    def _best_span(self, c: Chunk, terms: list[str]) -> tuple[int, int]:
        return focus_span(c, terms)


def query_terms(query: str) -> list[str]:
    """The stemmed, expanded terms a span should be scored against."""
    return _expand(tokenize(query))


def focus_span(c: Chunk, terms: list[str]) -> tuple[int, int]:
    """The sentence inside `c` that best covers `terms`, as absolute offsets.

    Chunks run ~1000 chars, so pointing a citation or a semantic result at the
    chunk's start shows the reader whatever heading happened to lead it rather
    than the line that actually matched. This picks the supporting sentence.
    """
    body = c.text
    sents = _spans_with_bounds(body)
    if not sents:
        return c.start, min(c.end, c.start + 200)

    folded = fold(body)
    best: tuple[int, tuple[int, int]] | None = None
    for s, e in sents:
        hits = sum(1 for m in _WORD_RE.finditer(folded[s:e]) if stem(m.group()) in terms)
        if best is None or hits > best[0]:
            best = (hits, (s, e))
    if best is None or best[0] == 0:
        first, last = sents[0]
        return c.start + first, c.start + min(last, first + 240)
    s, e = best[1]
    return c.start + s, c.start + min(e, s + 400)


def _spans_with_bounds(text: str) -> list[tuple[int, int]]:
    """Sentence spans within `text`, falling back to line spans when the text
    has no sentence punctuation (OCR output, tables, code)."""
    out: list[tuple[int, int]] = []
    pos = 0
    for m in re.finditer(r"[.!?]\s+|\n{2,}", text):
        if m.end() > pos:
            out.append((pos, m.end()))
            pos = m.end()
    if pos < len(text):
        out.append((pos, len(text)))
    if len(out) <= 1 and "\n" in text:
        out, pos = [], 0
        for m in re.finditer(r"\n", text):
            if m.end() > pos:
                out.append((pos, m.end()))
                pos = m.end()
        if pos < len(text):
            out.append((pos, len(text)))
    return [(s, e) for s, e in out if e > s]
