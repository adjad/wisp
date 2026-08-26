"""Deterministic safety layer for MOE.

This is intentionally NOT an LLM. Every state-changing action the model proposes
passes through `policy.decide()`, which returns allow / confirm / deny based on
fixed rules. The model proposes; this code disposes.
"""
from service.safety.policy import (
    Decision, Tier, decide, read_only, set_read_only, full_access, set_full_access,
)
from service.safety.audit import audit
from service.safety import grants

__all__ = ["Decision", "Tier", "decide", "audit", "grants",
           "read_only", "set_read_only", "full_access", "set_full_access"]
