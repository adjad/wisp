"""The original Notch test corpus: parsing, hashing and the hash-only ledger.

Stdlib only. The corpus is the user's private document; it is read in place,
verified against its recorded digest, and never copied into the repository.
Each entry is one line, ``N. `[R|W|S]` text``, and a multi-turn entry joins
its turns with `` → `` (the corpus's own documented separator).

Prompt text leaves this module only in the private run directory
(``cases.json``, mode 0600). The ledger it initializes holds ids, risk tags
and hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ENTRY = re.compile(r"^(\d+)\. `\[([RWS])\]` (.+)$")
TURN_SEPARATOR = " → "
SEGMENTATION_APPROVAL = "corpus-format: entries join turns with ' → ' (Count line of the corpus)"


class CorpusError(ValueError):
    """A corpus that is not the reviewed one, or not parseable as it."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class Case:
    case_id: int
    risk_tag: str
    text: str
    segments: tuple[str, ...]

    @property
    def entry_sha256(self) -> str:
        return sha256_text(self.text)

    @property
    def segment_sha256s(self) -> tuple[str, ...]:
        return tuple(sha256_text(s) for s in self.segments)


def parse(path: str | os.PathLike, *, expected_sha256: str, expected_cases: int = 1000) -> list[Case]:
    """Every case, in corpus order. Refuses a different or malformed corpus."""
    if sha256_file(path) != expected_sha256:
        raise CorpusError("the corpus does not match its recorded digest")
    cases: list[Case] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = ENTRY.match(line)
        if not match:
            continue
        text = match.group(3).strip()
        segments = tuple(part.strip() for part in text.split(TURN_SEPARATOR))
        if any(not part for part in segments):
            raise CorpusError(f"case {match.group(1)} has an empty turn")
        cases.append(Case(int(match.group(1)), match.group(2), text, segments))
    ids = [c.case_id for c in cases]
    if ids != list(range(1, expected_cases + 1)):
        raise CorpusError(f"expected ids 1..{expected_cases} once each, in order; found {len(ids)}")
    return cases


def _private_write(path: Path, data) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)


def initialize(run_dir: str | os.PathLike, cases: list[Case], *, corpus_sha256: str) -> dict:
    """Write cases.json (private, with text) and ledger.json (hashes only), once.

    A run directory already initialized for this corpus is left as it is, so
    resuming never rewrites recorded attempts; one bound to another corpus
    is refused.
    """
    run = Path(run_dir)
    ledger_path, cases_path = run / "ledger.json", run / "cases.json"
    if ledger_path.exists() or cases_path.exists():
        existing = json.loads(ledger_path.read_text(encoding="utf-8"))
        if existing.get("corpus_sha256") != corpus_sha256:
            raise CorpusError("this run directory belongs to a different corpus")
        return {"initialized": False, "cases": len(existing.get("cases", []))}
    _private_write(cases_path, {"corpus_sha256": corpus_sha256, "cases": [
        {"case_id": c.case_id, "risk_tag": c.risk_tag, "text": c.text, "segments": list(c.segments)}
        for c in cases]})
    ledger_cases = []
    for c in cases:
        entry = {"case_id": c.case_id, "risk_tag": c.risk_tag, "prompt_sha256": c.entry_sha256,
                 "status": "NOT_RUN", "attempts": []}
        if len(c.segments) > 1:
            entry["segmentation"] = {"entry_sha256": c.entry_sha256, "segments": list(c.segment_sha256s),
                                     "approved_by": SEGMENTATION_APPROVAL, "approved_at": 0}
        ledger_cases.append(entry)
    _private_write(ledger_path, {"schema_version": 1, "corpus_sha256": corpus_sha256, "cases": ledger_cases})
    return {"initialized": True, "cases": len(cases)}


def load_cases(run_dir: str | os.PathLike, *, corpus_sha256: str) -> list[Case]:
    data = json.loads(Path(run_dir, "cases.json").read_text(encoding="utf-8"))
    if data.get("corpus_sha256") != corpus_sha256:
        raise CorpusError("cases.json belongs to a different corpus")
    return [Case(c["case_id"], c["risk_tag"], c["text"], tuple(c["segments"])) for c in data["cases"]]
