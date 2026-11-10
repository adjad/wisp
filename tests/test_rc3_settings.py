"""Source checks for the 0.5.0 rc3 Settings repairs: the role menus offer chat
models only and say when a pick wasn't saved (the logic itself is checked in
tests/QuickSettingsChecks.swift), Google's Disconnect & erase asks first, and
the Auto-unload row says which models it is about and when a change wasn't saved,
and the reset card's Keeps list names no retired device.
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


def test_auto_unload_row_says_what_it_governs_and_when_a_change_was_not_saved():
    settings = _read("SettingsView.swift")
    text = _read("QuickSettingsText.swift")
    # The caption names the other models and sends the chat model to Save memory.
    caption = _body(text, "static func idleUnload(minutes: Double) -> String")
    assert "Smart Search" in caption and "The chat model follows Save memory instead." in caption
    assert "Text(QuickSettingsText.idleUnload(minutes: loader.idleMinutes))" in settings
    assert "models stay resident until you unload them" not in settings
    assert "Unload a model after" not in settings
    # A change the service didn't confirm goes back to the last confirmed value and says so.
    save = _body(settings, "    func setIdleMinutes(_ minutes: Double)")
    assert "client.setIdleTimeout" not in save
    assert "SettingsLoader.idleTimeoutReply(setting: minutes)" in save
    assert "self.idleMinutes = self.confirmedIdleMinutes" in save
    assert "self.idleSaveProblem = QuickSettingsText.idleUnloadSaveProblem" in save
    assert "guard generation == self.idleSaveGeneration else { return }" in save
    reply = _body(settings, "private static func idleTimeoutReply(setting minutes: Double? = nil) async -> Double?")
    assert "(200..<300).contains(http.statusCode)" in reply and "else { return nil }" in reply
    assert "if let problem = loader.idleSaveProblem {" in settings
    # An unanswered read no longer shows a made-up 20 minutes.
    refresh = _body(settings, "    func refresh()")
    assert "client.idleTimeout()" not in refresh
    assert "if let idle = await SettingsLoader.idleTimeoutReply() {" in refresh


def test_learning_copy_stays_free_of_transport_for_its_standalone_check():
    # tests/test_behavior_activity_native.py compiles this stretch of SettingsLoader
    # by itself, ending at setIdleMinutes, and it must not reach the network.
    settings = _read("SettingsView.swift")
    start = settings.index("    var behaviorExplanation: String {")
    copy = settings[start:settings.index("    func setIdleMinutes", start)]
    assert "URLSession" not in copy and "idleTimeoutReply" not in copy
    properties = settings[settings.index("    @Published var behaviorEnabled"):
                          settings.index("    @Published var behaviorForgetStatus")]
    assert "idle" not in properties and "roleSaveProblem" not in properties


def test_reset_card_keeps_list_names_no_retired_device():
    reset = _read("ResetWisp.swift")
    keeps = reset[reset.index("    static let keeps = ["):reset.index("    static let internetAccountsNote")]
    assert "Mac mini" not in keeps and "Air node" not in keeps
    assert ('"Everything outside Wisp: your own oMLX app and its cache, and the iPhone app\'s cache",'
            in keeps)
    # What the reset deletes and the accounts note are as they were.
    assert '"The model and Wisp\'s engine, so nothing downloads again",' in keeps
    assert reset.count("static let deletes = [") == 1 and 'list("Keeps", WispReset.keeps)' in reset
