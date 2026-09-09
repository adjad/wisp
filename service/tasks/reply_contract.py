"""Exact, structured contract for the reply Mail is about to send.

Source-message metadata is deliberately not a substitute for the outgoing
envelope (Reply-To, aliases and reply-all can all change it).
"""
from __future__ import annotations

import hashlib
import json

FIELDS = ("message_id", "account", "account_id", "from", "to", "cc", "bcc",
          "subject", "content")
RECEIPT_PREFIX = "Reply sent. Receipt: "


def validate_envelope(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    if any(not isinstance(value.get(k), str) for k in FIELDS if k not in {"to", "cc", "bcc"}):
        return None
    if any(not value[k].strip() for k in ("message_id", "account", "account_id", "from", "content")):
        return None
    if any(any(c in value[k] for c in "\r\n\x00\x01\x02")
           for k in ("message_id", "account", "account_id", "from", "subject")):
        return None
    for field in ("to", "cc", "bcc"):
        if not isinstance(value.get(field), list) or any(
                not isinstance(v, str) or "@" not in v or "\n" in v or "\r" in v
                for v in value[field]):
            return None
    if not value["to"] or "@" not in value["from"]:
        return None
    return {key: list(value[key]) if key in {"to", "cc", "bcc"} else value[key]
            for key in FIELDS}


def envelope_matches(actual: object, expected: object) -> bool:
    a, b = validate_envelope(actual), validate_envelope(expected)
    # Exact equality includes the entire To/CC/BCC sets and rendered body.
    # A harmless ordering/format change can ask for fresh approval; it must
    # never silently loosen the contract on an effect.
    return a is not None and b is not None and a == b


def make_receipt(envelope: dict) -> str:
    receipt = {k: v for k, v in envelope.items() if k != "content"}
    receipt["content_digest"] = hashlib.sha256(envelope["content"].encode()).hexdigest()
    return RECEIPT_PREFIX + json.dumps(receipt, sort_keys=True, ensure_ascii=False)


def verify_receipt(raw: str, expected: dict) -> bool:
    if not validate_envelope(expected) or not raw.startswith(RECEIPT_PREFIX):
        return False
    try:
        return json.loads(raw[len(RECEIPT_PREFIX):]) == json.loads(
            make_receipt(expected)[len(RECEIPT_PREFIX):])
    except (ValueError, TypeError):
        return False


def preview(envelope: dict) -> str:
    lines = [f"Account: {envelope['account']}", f"From: {envelope['from']}",
             f"To: {', '.join(envelope['to'])}"]
    for key in ("cc", "bcc"):
        if envelope[key]:
            lines.append(f"{key.upper()}: {', '.join(envelope[key])}")
    lines += [f"Subject: {envelope['subject']}", "", envelope["content"]]
    return "\n".join(lines)
