"""Credential recovery checks always use per-test synthetic state."""
import pytest


@pytest.fixture(autouse=True)
def isolated_credential_quarantine(tmp_path, monkeypatch):
    from service.config import quarantine, credentials
    gate = quarantine.RecoveryGate(home=lambda: tmp_path)
    gate.callbacks.append(credentials._VALUES.clear)
    monkeypatch.setattr(quarantine, "_gate", gate)
