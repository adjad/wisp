"""Frozen PR #42 v1 presentation protocol; deliberately no backend imports."""
from __future__ import annotations

import hashlib

from mini.http import encode

KINDS = frozenset({"canvas.sync", "study.generate", "stocks.watch", "research.run", "effect.proposal"})
EFFECTS = frozenset({"email.send", "message.send", "calendar.write"})
FIELDS = frozenset({"schema_version", "node_id", "result_id", "job_id", "occurrence_id",
                    "kind", "title", "text", "proposal"})


def text(value, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid result text")
    return value


def identity(prefix, *parts):
    return prefix + hashlib.sha256(encode(parts)).hexdigest()


def validate(raw, node_id):
    if not isinstance(raw, dict) or set(raw) != FIELDS:
        raise ValueError("Invalid result envelope")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or raw["node_id"] != node_id:
        raise ValueError("Invalid result identity")
    for key in ("node_id", "result_id", "job_id", "occurrence_id", "title"):
        text(raw[key])
    text(raw["text"], 100_000)
    if not isinstance(raw["kind"], str) or raw["kind"] not in KINDS:
        raise ValueError("Invalid result kind")
    proposal = raw["proposal"]
    if raw["kind"] == "effect.proposal":
        if not isinstance(proposal, dict) or set(proposal) != {"effect_id", "kind", "arguments"}:
            raise ValueError("Invalid effect proposal")
        text(proposal["effect_id"])
        if not isinstance(proposal["kind"], str) or proposal["kind"] not in EFFECTS or not isinstance(proposal["arguments"], dict):
            raise ValueError("Invalid effect proposal")
    elif proposal is not None:
        raise ValueError("Unexpected effect proposal")
    body = encode(raw)
    if len(body) > 200_000:
        raise ValueError("Result exceeds capacity")
    return body.decode(), hashlib.sha256(body).hexdigest()
