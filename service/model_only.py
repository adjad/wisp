"""Test switch that sends every /agent turn to the model (see model_only_enabled)."""
from __future__ import annotations

import os


def model_only_enabled() -> bool:
    """TEST SWITCH: every /agent turn goes to the model, nothing answers first.

    On, it forces model-led discovery for every turn and skips the
    deterministic layers that otherwise answer before the model: the batch
    correction guard, typed tasks, delivery workflows, router reads and
    router-direct calls, and the verbatim short-circuit of presynthesized tool
    results. In the agent loop it also states thinking ON for every step of
    every model and never pins temperature, so each model runs on its own oMLX
    profile. Safety is unchanged: the policy engine, confirmation cards,
    routing guard contracts and the audit log still apply to every call.

    WISP_MODEL_ONLY (true/false) wins; otherwise the `model_only` config key
    (~/.moe/config.yaml overrides the packaged default). Invalid values fail
    closed to OFF rather than raising, so a typo never breaks a turn.
    """
    value = os.environ.get("WISP_MODEL_ONLY")
    if value is None:
        from service.config import models_config
        try:
            value = models_config().get("model_only", False)
        except Exception:  # noqa: BLE001 - an unreadable config keeps the default path
            return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
