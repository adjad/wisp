"""Pydantic schemas for model-produced JSON the host must validate before
trusting it. Shape/type validation only — grounding (does the quote actually
appear in the source) is a separate, host-side check that no schema can
express, and stays in citations.py/orchestrator.py.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

_STANCES = {"supports", "contradicts", "context"}
_LABELS = {"supported", "partially_supported", "contradicted", "unclear"}


class SubquestionDraft(BaseModel):
    """Validated one at a time, never as part of a whole-plan list — a single
    malformed item must not discard every other good subquestion alongside it."""
    question: str = Field(min_length=1, max_length=400)


class QueryCandidate(BaseModel):
    subquestion_id: str
    query: str = Field(min_length=1, max_length=500)


class EvidenceItem(BaseModel):
    subquestion_id: str
    claim: str = Field(min_length=1, max_length=800)
    quote: str = Field(min_length=20, max_length=1600)
    stance: str = "supports"
    confidence: float = 0.5

    @field_validator("stance", mode="before")
    @classmethod
    def _stance(cls, v: object) -> str:
        value = str(v or "supports").lower()
        return value if value in _STANCES else "supports"

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v: object) -> float:
        try:
            return max(0.0, min(float(v), 1.0))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.5


class CriticVerdict(BaseModel):
    index: int
    label: str

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        value = v.lower()
        if value not in _LABELS:
            raise ValueError("invalid critic label")
        return value
