"""Source checks for the 0.5.0 rc3 Settings repairs: the role menus offer chat
models only and say when a pick wasn't saved (the logic itself is checked in
tests/QuickSettingsChecks.swift), and Google's Disconnect & erase asks first.
Reads source only; no app, service or network."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app/Sources/WispApp"


def _read(name: str) -> str:
    return (APP / name).read_text()


def _body(text: str, signature: str) -> str:
    """The brace-balanced body that follows `signature`."""
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[open_at:i + 1]
    raise AssertionError(f"unbalanced body after {signature!r}")


def test_role_menus_offer_chat_models_only():
    settings = _read("SettingsView.swift")
    # Every role's menu is built by RoleModelChoices, which leaves out the
    # embedder and the reranker; no menu lists the raw roster any more.
    assert "ForEach(loader.modelChoices(for: role), id: \\.self)" in settings
    assert "installed + roles.values" not in settings
    choices = _body(settings, "func modelChoices(for role: String) -> [String]")
    assert "RoleModelChoices.choices(installed: installed, roles: roles, keeping: selectedModel(for: role))" in choices
    rules = _body(_read("QuickSettingsText.swift"), "enum RoleModelChoices")
    assert 'static let nonChatRoles = ["embedding", "reranker"]' in rules
    assert 'name.contains("embed")' in rules and 'name.contains("rerank")' in rules


def test_role_fallback_is_the_model_the_service_ships():
    rules = _body(_read("QuickSettingsText.swift"), "enum RoleModelChoices")
    default = re.search(r'static let bundledDefault = "([^"]+)"', rules).group(1)
    roles = yaml.safe_load((ROOT / "service/config/models.yaml").read_text())["roles"]
    for role in ("fast", "coding", "reasoning", "general"):
        assert roles[role] == default, role
        assert f'"{role}": bundledDefault' in rules, role
    # The two roles the menus leave out are still what the service calls them.
    assert {"embedding", "reranker"} <= set(roles)
    assert "Mellum" not in _read("SettingsView.swift") and "Mellum" not in rules


def test_a_model_pick_the_service_did_not_save_goes_back_and_says_so():
    settings = _read("SettingsView.swift")
    save = _body(settings, "    func set(_ role: String, _ model: String)")
    assert "_ = try? await URLSession.shared.data(for: req)" not in save
    assert "(200..<300).contains(http.statusCode)" in save and 'object["ok"] as? Bool == true' in save
    assert "let previous = roles[role]" in save and "self.roles[role] = previous" in save
    assert "self.roleSaveProblem = RoleModelChoices.saveProblem" in save
    # A later pick or a refresh is never overwritten by an older failure.
    assert "if !saved, self.roles[role] == model {" in save
    # The setup test is still marked stale whatever happened.
    assert "post(name: .wispInferenceConfigurationChanged" in save
    assert "if let problem = loader.roleSaveProblem {" in settings


def test_google_disconnect_and_erase_asks_first():
    google = _read("GoogleSettingsPane.swift")
    pane = google[google.index("struct GoogleSettingsPane: View"):]
    # The row's button only opens the alert; one click erases nothing.
    row = _body(pane, "private func accountRow(_ account: GoogleStatus.Account) -> some View")
    assert "model.disconnect(" not in row
    button = _body(row, 'Button("Disconnect & erase…")')
    assert "accountToErase = account.id" in button and "confirmErase = true" in button
    # The erase runs from the alert's destructive button and nowhere else.
    assert pane.count("model.disconnect(") == 1
    start = pane.index(".alert(")
    alert = pane[start:pane.index(".onChange(", start)]
    assert "isPresented: $confirmErase" in alert and "\\(accountToErase)" in alert
    erase = _body(alert, 'Button("Erase", role: .destructive)')
    assert "model.disconnect(accountToErase)" in erase
    assert 'Button("Cancel", role: .cancel) {}' in alert
    for part in ("mail", "calendar events", "summaries", "to-dos", "revokes Wisp's Google access"):
        assert part in alert, part
    # What the erase does, and how its result is reported, did not change.
    model = google[:google.index("struct GoogleSettingsPane: View")]
    disconnect = _body(model, "    func disconnect(_ account: String)")
    assert '"assistant/gmail/disconnect", body: ["account": account]' in disconnect
    assert "GoogleErase.note(account: account, response: response)" in disconnect
