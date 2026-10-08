"""Credential recovery checks always use per-test synthetic state."""
import pytest


# These suites exercise the supported legacy rollback path and its original
# router/task/provider contracts. New default-on/model-led suites are excluded.
LEGACY_ROUTER_SUITES = frozenset({
    "test_diagnostics.py", "test_approval_routing.py", "test_super_model_routing.py",
    "test_turn_engine_calls.py", "test_agent_disconnect.py",
    "test_read_context_continuations.py", "test_local_provider_settings.py",
    "test_lazy_inference_readiness.py", "test_outbound_content_provenance.py",
    "test_typed_task_engine.py", "test_typed_reminder_operations.py",
    "test_workflow_engine.py", "test_web_response_followup.py",
    "test_replay_failure_fixes.py",
    "test_fast_path_intent.py", "test_router_everyday_prompts.py",
})


@pytest.fixture(autouse=True)
def explicit_legacy_router_for_rollback_suites(request, monkeypatch):
    if request.path.name in LEGACY_ROUTER_SUITES:
        monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "0")


@pytest.fixture(autouse=True)
def isolated_credential_quarantine(tmp_path, monkeypatch):
    from service.config import quarantine, credentials
    gate = quarantine.RecoveryGate(home=lambda: tmp_path)
    gate.callbacks.append(credentials._VALUES.clear)
    monkeypatch.setattr(quarantine, "_gate", gate)
