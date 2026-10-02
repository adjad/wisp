"""Credential redaction for text Wisp persists, logs or sends to a model.

A key typed or pasted into chat is ordinary text to everything downstream: it
was written to the turn store and full-text index, the audit log, and would
have been sent to whichever model answered (a cloud model included). Nothing
about a key is useful to a model, so the rule is simple: recognise credential
shapes at the boundary and replace them before any of that happens.

Patterns are deliberately precise (provider prefixes with realistic minimum
lengths, PEM private-key blocks, and explicit `key=value` assignments) so a
git SHA, a UUID or a long URL is left alone. Redaction is lossy by design and
never raises.
"""
from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "[redacted credential]"

_PREFIXED = (
    r"sk-or-v1-[A-Za-z0-9]{20,}",               # OpenRouter
    r"sk-ant-[A-Za-z0-9_-]{20,}",               # Anthropic
    r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}",         # OpenAI-style
    r"gh[pousr]_[A-Za-z0-9]{30,}",              # GitHub tokens
    r"github_pat_[A-Za-z0-9_]{40,}",
    r"xox[abprs]-[A-Za-z0-9-]{20,}",            # Slack
    r"AKIA[0-9A-Z]{16}",                        # AWS access key id
    r"AIza[0-9A-Za-z_-]{35}",                   # Google API key
    r"hf_[A-Za-z0-9]{30,}",                     # Hugging Face
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JWT
)
_PEM = r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
_BEARER = r"(?i:\bBearer)\s+[A-Za-z0-9._~+/=-]{20,}"
_ASSIGNED = re.compile(
    r"(?i)\b((?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|"
    r"token|passw(?:or)?d|passwd)\s*[:=]\s*[\"']?)([^\s\"'`,;]{12,})")
_TOKEN = re.compile("|".join([_PEM, _BEARER, *_PREFIXED]), re.S)

# After removing credentials, a message this short that began as a slash
# command (or is little more than the key itself) is someone handing Wisp a
# key, not asking it a question.
_HANDOFF_WORDS = 6


def scrub(text: str) -> tuple[str, int]:
    """Return (text with credentials replaced, number replaced)."""
    if not isinstance(text, str) or len(text) < 12:
        return text, 0
    count = 0

    def token(_match: re.Match) -> str:
        nonlocal count
        count += 1
        return PLACEHOLDER

    def assigned(match: re.Match) -> str:
        nonlocal count
        value = match.group(2)
        if value == PLACEHOLDER or value.startswith("[redacted"):
            return match.group(0)
        count += 1
        return match.group(1) + PLACEHOLDER

    cleaned = _TOKEN.sub(token, text)
    cleaned = _ASSIGNED.sub(assigned, cleaned)
    return cleaned, count


def scrub_obj(value: Any, _depth: int = 0) -> Any:
    """Recursively redact strings inside JSON-like data (audit records)."""
    if _depth > 8:
        return value
    if isinstance(value, str):
        return scrub(value)[0]
    if isinstance(value, dict):
        return {k: scrub_obj(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_obj(v, _depth + 1) for v in value]
    return value


def is_key_handoff(cleaned_prompt: str) -> bool:
    """True when a prompt that contained a credential was just handing it over."""
    words = [w for w in cleaned_prompt.replace(PLACEHOLDER, " ").split() if w]
    return cleaned_prompt.lstrip().startswith("/") or len(words) <= _HANDOFF_WORDS


HANDOFF_NOTICE = (
    "That looks like an API key, so I didn't keep it or send it to any model. "
    "Keys never go in chat. To connect a provider, open Settings \u2192 Cloud "
    "inference and enter it there \u2014 it's stored in your macOS Keychain. "
    "If you already pasted this key somewhere else, rotate it."
)
