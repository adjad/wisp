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
# Share the complete label grammar between serialized assignments and typed
# audit dictionaries. Qualifiers (OPENAI_, client_, aws_) are identifiers, not
# arbitrary prose; metadata suffixes such as token_count do not match.
_CREDENTIAL_LABEL = (
    r"(?:(?:[a-z][a-z0-9]*[_-])+)?"
    r"(?:api[_-]?key|secret(?:[_-]?(?:access[_-]?)?key)?|"
    r"(?:access|auth|refresh)[_-]?token|token|passw(?:or)?d|passwd)")
_CREDENTIAL_FIELD = re.compile(_CREDENTIAL_LABEL, re.I)
_ASSIGNED = re.compile(
    rf"(?i)\b({_CREDENTIAL_LABEL}[\"']?\s*[:=]\s*[\"']?)([^\s\"'`,;]{{12,}})")
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
        # The unvisited subtree may contain a credential. Bound traversal by
        # dropping it, never by returning the original unsanitized value.
        return PLACEHOLDER
    if isinstance(value, str):
        return scrub(value)[0]
    if isinstance(value, dict):
        # A typed password/API-key field establishes sensitivity even when its
        # value is opaque or short. Drop credential containers as a whole too;
        # recurse normally through siblings so useful audit metadata survives.
        return {k: (PLACEHOLDER if isinstance(k, str)
                    and _CREDENTIAL_FIELD.fullmatch(k)
                    and v is not None and not isinstance(v, bool)
                    else scrub_obj(v, _depth + 1)) for k, v in value.items()}
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
