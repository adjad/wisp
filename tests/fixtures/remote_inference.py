"""A fictional remote-inference profile for tests (no real server or secret).

``EXAMPLE`` uses the 8k read-only budgets; ``FULL_LIMITS`` are the larger
budgets the full-tools tests use. ``example_profile`` validates through the
same path a user's ``inference.remote_only`` config takes.
"""
from __future__ import annotations

EXAMPLE = {'base_url': 'https://inference.example', 'model_id': 'example-model',
           'credential_ref': 'keychain:example', 'context_window': 8000, 'max_tokens': 4096,
           'request_bytes': 131072, 'messages': 32, 'aggregate_bytes': 24000, 'tools': 32,
           'response_bytes': 4194304}
FULL_LIMITS = {'context_window': 32000, 'max_tokens': 8192, 'request_bytes': 524288,
               'messages': 200, 'aggregate_bytes': 131072}


def example_profile(**overrides):
    """A validated RemoteOnlyProfile for EXAMPLE (plus overrides)."""
    from service.inference.remote_policy import profile_from_config
    return profile_from_config({**EXAMPLE, **overrides})
