"""Settings › Models after Disconnect: the cloud form reloads from the backend (retrying
while it restarts) and, on the Intel build, the first-run values come back, so Connect
works without Refresh models. A tester using the reconnect steps hit a greyed-out
Connect after Disconnect. Reads source only; no app, service or network."""
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[1] / "app/Sources/WispApp/SettingsView.swift"


def _body(text: str, signature: str) -> str:
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[open_at:i + 1]
    raise AssertionError(signature)


def test_disconnect_reloads_the_form_after_it_succeeds():
    body = _body(SETTINGS.read_text(), "func disconnectCloud()")
    done = body.index('cloudStatus = "Local models only"')
    reset = body.index("intelPresetApplied = false")
    reload = body.index("await refreshCloudAfterDisconnect()")
    failure = body.index("} catch {\n                cloudStatus = error.localizedDescription")
    assert done < reset < reload < failure      # only on the success path, before cloudSaving ends


def test_the_reload_retries_while_the_backend_restarts():
    text = SETTINGS.read_text()
    body = _body(text, "private func refreshCloudAfterDisconnect()")
    assert "for attempt in 0..<Self.disconnectRefreshAttempts" in body
    assert "await refreshCloud()" in body and "if !cloudStateUnknown { return }" in body
    assert "Task.sleep" in body
    assert "nonisolated static let disconnectRefreshAttempts = 15" in text


def test_the_intel_preset_refills_only_an_empty_disconnected_form():
    body = _body(SETTINGS.read_text(), "private func preselectIntelPreset()")
    assert "!intelPresetApplied, !cloudConnected" in body
    assert "cloudRoles.isEmpty, cloudAPIKey.isEmpty" in body
