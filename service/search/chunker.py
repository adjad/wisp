"""Split extracted document text into retrievable chunks.

Every chunk carries `(start, end)` char offsets into the ORIGINAL text. That is
not an optimization — it's what makes citation, highlight rectangles and
scroll-to possible, so the offsets must stay exact through every transform.
Nothing in here may rewrite text in a way that changes lengths.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ~250 tokens at the usual ~4 chars/token, with ~40% overlap so a sentence that
# straddles a boundary is still wholly present in one chunk.
TARGET_CHARS = 1000
OVERLAP_CHARS = 400
MIN_CHARS = 80

# Structural boundaries, strongest first. Splitting on these rather than a blind
# character stride keeps a heading with its body and a list item intact.
_PARA_RE = re.compile(r"\n\s*\n")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")


@dataclass(frozen=True)
class Chunk:
    idx: int
    text: str
    start: int
    end: int

    def to_dict(self) -> dict:
        return {"idx": self.idx, "text": self.text, "start": self.start, "end": self.end}


def _spans(text: str, pattern: re.Pattern) -> list[tuple[int, int]]:
    """Split `text` on `pattern`, returning (start, end) spans of the pieces.

    The separator is dropped from the piece but its length still counts toward
    the offsets, which is exactly why this returns spans rather than strings.
    """
    out: list[tuple[int, int]] = []
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            out.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        out.append((pos, len(text)))
    return out


def _atoms(text: str) -> list[tuple[int, int]]:
    """Smallest units we're willing to split between: paragraphs, then
    sentences within an over-long paragraph, then a hard character stride for
    pathological input (minified text, OCR with no punctuation)."""
    atoms: list[tuple[int, int]] = []
    for ps, pe in _spans(text, _PARA_RE) or [(0, len(text))]:
        if pe - ps <= TARGET_CHARS:
            atoms.append((ps, pe))
            continue
        para = text[ps:pe]
        sents = _spans(para, _SENT_RE) or [(0, len(para))]
        for ss, se in sents:
            if se - ss <= TARGET_CHARS:
                atoms.append((ps + ss, ps + se))
            else:  # no sentence structure to lean on — stride it
                for off in range(ss, se, TARGET_CHARS):
                    atoms.append((ps + off, ps + min(off + TARGET_CHARS, se)))
    return [(s, e) for s, e in atoms if e > s]


def chunk(text: str) -> list[Chunk]:
    """Pack atoms into overlapping ~TARGET_CHARS chunks with exact offsets."""
    if not text.strip():
        return []
    atoms = _atoms(text)
    if not atoms:
        return []

    chunks: list[Chunk] = []
    i = 0
    while i < len(atoms):
        start = atoms[i][0]
        end = atoms[i][1]
        j = i + 1
        while j < len(atoms) and atoms[j][1] - start <= TARGET_CHARS:
            end = atoms[j][1]
            j += 1
        body = text[start:end]
        if body.strip():
            chunks.append(Chunk(len(chunks), body, start, end))
        if j >= len(atoms):
            break
        # Step back far enough to overlap, but always make forward progress.
        back = j
        while back > i + 1 and end - atoms[back - 1][0] < OVERLAP_CHARS:
            back -= 1
        i = max(back, i + 1)

    # Fold a stubby trailing chunk into its predecessor rather than embedding a
    # fragment that will never rank meaningfully.
    if len(chunks) > 1 and len(chunks[-1].text) < MIN_CHARS:
        last = chunks.pop()
        prev = chunks[-1]
        chunks[-1] = Chunk(prev.idx, text[prev.start:last.end], prev.start, last.end)
    return chunks
