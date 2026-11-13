"""Settings > Privacy & Access lists the standing "Always allow" rules and removes
one at a time (0.5.0 rc3). The list reads GET /permissions and each Remove calls
DELETE /permissions/{tool}?scope=...; these tests pin the reply shape the app
parses and the exact request form the app builds, against the real routes with
the rules kept in a temporary file. The app's own parsing and URL building are
checked in tests/QuickSettingsChecks.swift. Nothing is launched or contacted."""
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import service.main as main
from service.safety import grants

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app/Sources/WispApp"
FOLDER = "/Users/sam/C++ notes"          # a made-up folder with a plus and a space


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    return TestClient(main.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)


def _remove(client, tool: str, scope: str):
    """The request StandingRule.removeURL builds: everything but letters, digits
    and -._~ percent-encoded, and the scope always sent."""
    return client.delete(f"/permissions/{quote(tool, safe='')}?scope={quote(scope, safe='')}")


def _rules(client) -> set:
    reply = client.get("/permissions")
    assert reply.status_code == 200
    return {(kind, tool, row.get("scope", ""))
            for tool, entry in reply.json()["grants"].items()
            for kind in ("allow", "deny") for row in entry.get(kind, [])}


def _seed():
    assert grants.grant("move_path", {}, scoped=False)["ok"] is True
    assert grants.grant("write_file", {"path": f"{FOLDER}/a.txt"})["ok"] is True
    assert grants.grant("write_file", {}, scoped=False)["ok"] is True
    assert grants.grant("web_fetch", {"url": "https://example.invalid/page?q=1"})["ok"] is True
    assert grants.grant("open_app", {}, decision="deny", scoped=False)["ok"] is True


def test_the_list_shows_every_rule_with_its_scope(client):
    _seed()
    assert _rules(client) == {
        ("allow", "move_path", ""), ("allow", "write_file", FOLDER), ("allow", "write_file", ""),
        ("allow", "web_fetch", "example.invalid"), ("deny", "open_app", ""),
    }


def test_remove_drops_only_the_one_rule(client, monkeypatch):
    _seed()
    before = _rules(client)
    # A tool-wide rule goes; the same tool's folder rule stays.
    assert _remove(client, "write_file", "").json() == {"ok": True}
    assert _rules(client) == before - {("allow", "write_file", "")}
    assert grants.check("write_file", {"path": "/Users/sam/Other/b.txt"}) is None      # asks again
    assert grants.check("write_file", {"path": f"{FOLDER}/c.txt"}) == "allow"
    # A folder with a plus and a space is matched exactly.
    assert _remove(client, "write_file", FOLDER).json() == {"ok": True}
    assert _rules(client) == before - {("allow", "write_file", ""), ("allow", "write_file", FOLDER)}
    assert grants.check("write_file", {"path": f"{FOLDER}/c.txt"}) is None
    # A block rule can be removed the same way, by the Wisp app only (lifting a block lets the
    # action through again); the others are untouched.
    refused = _remove(client, "open_app", "")
    assert refused.status_code == 403 and "Only the Wisp app" in refused.json()["detail"]
    assert ("deny", "open_app", "") in _rules(client)
    monkeypatch.setattr(main, "_require_wisp_app", lambda request, reason: None)     # the app's own request
    assert _remove(client, "open_app", "").json() == {"ok": True}
    assert _rules(client) == {("allow", "move_path", ""), ("allow", "web_fetch", "example.invalid")}
    # A rule that is already gone is not an error the row has to explain.
    assert _remove(client, "open_app", "").json() == {"ok": False}


def test_only_the_wisp_app_can_add_an_always_allow_or_lift_a_block(client, monkeypatch):
    _seed()
    before = _rules(client)
    reason = "Only the Wisp app can let Wisp act without asking. Change this in Wisp's Settings."
    # A local program that is not the app: no new "Always allow", by default or by name.
    for body in ({"tool": "run_shell", "scoped": False}, {"tool": "run_shell", "decision": "allow", "scoped": False},
                 {"tool": "write_file", "args": {"path": "/Users/sam/x/a.txt"}, "decision": "allow"}):
        refused = client.post("/permissions", json=body)
        assert refused.status_code == 403 and refused.json()["detail"] == reason
    # ...and no lifting a block, with the scope or without it.
    assert client.delete("/permissions/open_app").status_code == 403
    assert _remove(client, "open_app", "").status_code == 403
    assert _rules(client) == before and grants.check("run_shell", {"cmd": "ls"}) is None
    # Tightening stays open to the ordinary request: a new block, and removing an allow.
    assert client.post("/permissions", json={"tool": "quit_app", "decision": "deny", "scoped": False}).json()["ok"] is True
    assert _remove(client, "web_fetch", "example.invalid").json() == {"ok": True}
    assert client.get("/permissions").status_code == 200                    # reading stays open
    # The app's own request does all of it.
    monkeypatch.setattr(main, "_require_wisp_app", lambda request, why: None)
    assert client.post("/permissions", json={"tool": "write_file", "args": {"path": f"{FOLDER}/z.txt"}}).json()["ok"] is True
    assert _remove(client, "open_app", "").json() == {"ok": True}
    assert ("deny", "open_app", "") not in _rules(client)


def test_the_scope_is_what_keeps_a_remove_to_one_rule(client):
    _seed()
    # Without ?scope= the route drops every rule of the tool, which is why the app
    # always sends it, empty too.
    assert client.delete("/permissions/write_file").json() == {"ok": True}
    assert not any(tool == "write_file" for _kind, tool, _scope in _rules(client))
    text = (APP / "QuickSettingsText.swift").read_text()
    rule = text[text.index("struct StandingRule: Identifiable, Equatable"):]
    build = rule[rule.index("func removeURL(base: URL) -> URL?"):]
    assert 'parts.percentEncodedPath = root + "/permissions/" + name' in build
    assert 'parts.percentEncodedQuery = "scope=" + value' in build
    assert 'CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")' in build


def test_a_web_page_cannot_remove_a_rule(client):
    _seed()
    before = _rules(client)
    reply = client.delete("/permissions/open_app?scope=", headers={"Origin": "https://example.invalid"})
    assert reply.status_code in (400, 403)
    assert _rules(client) == before


def test_settings_lists_the_rules_and_only_removes(client):
    settings = (APP / "SettingsView.swift").read_text()
    # The list sits in Privacy & Access, right under Full access.
    pane = settings[settings.index('if selectedPane == "privacy" {'):]
    assert pane.index("fullAccessLabel") < pane.index("standingRulesSection") < pane.index('Text("Browser history")')
    section = settings[settings.index("private var standingRulesSection: some View"):
                       settings.index("private var behaviorLearningLabel: some View")]
    assert "ForEach(rules) { rule in" in section and "Text(rule.summary)" in section
    assert 'Button("Remove") { loader.removeStandingRule(rule) }' in section
    assert "Text(StandingRule.noRules)" in section and "Text(StandingRule.unread)" in section
    assert ".task { await loader.refreshStandingRules() }" in section
    loader = settings[settings.index("    func refreshStandingRules() async"):
                      settings.index("    #if WISP_NOTCH_QA || WISP_CHAT_QA")]
    assert 'req.httpMethod = "DELETE"' in loader and "rule.removeURL(base: WispClient.baseURL)" in loader
    # The row goes only when a fresh read no longer lists the rule.
    assert "if now.contains(rule) { self.standingRulesStatus = StandingRule.removeFailed }" in loader
    assert 'appendingPathComponent("permissions")' in loader and "StandingRule.parse(data)" in loader
    # Settings never creates a rule: Always allow is still only the card's button.
    assert '"POST"' not in loader
    # An unread list is never shown as "no rules".
    text = (APP / "QuickSettingsText.swift").read_text()
    parse = text[text.index("static func parse(_ data: Data) -> [StandingRule]?"):]
    assert 'let grants = object["grants"] as? [String: Any] else { return nil }' in parse
