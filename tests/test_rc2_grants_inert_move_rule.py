"""Settings tells the truth about an inert pre-rc3 move rule.

Owner decision 2026-11-10: a tool-wide "Always allow" on move_path saved before
rc3 no longer covers any move (grants._allow_covers), but it stays on disk and
in Settings until removed. Settings still labelled it "Always allow · every
action". GET /permissions now gives such a rule a `summary` the Settings row
shows instead. Rules live in a temporary file; nothing is moved or contacted.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import service.main as main
from service.safety import grants, policy

APP = Path(__file__).resolve().parents[1] / "app/Sources/WispApp"
MOVE = {"source": "/tmp/wisp-test/in/a.pdf", "destination": "/tmp/wisp-test/out/a.pdf"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(policy, "_FULL_ACCESS", False, raising=False)


def _seed():
    grants._CACHE["move_path"] = {"allow": [{"scope": "", "granted_at": 1.0}],
                                  "deny": [{"scope": ""}]}
    grants._CACHE["write_file"] = {"allow": [{"scope": ""}], "deny": []}
    grants.grant("move_path", MOVE)          # a fresh folder rule, saved to disk


def test_only_the_inert_move_rule_is_labelled_no_longer_used():
    _seed()
    listed = grants.all_grants()
    allow = {r["scope"]: r for r in listed["move_path"]["allow"]}
    assert allow[""]["summary"] == grants.INERT_MOVE_RULE == "No longer used · Wisp asks again for moves"
    assert "summary" not in allow["/tmp/wisp-test/out"]          # the folder rule still works
    assert "summary" not in listed["move_path"]["deny"][0]       # a block still blocks
    assert "summary" not in listed["write_file"]["allow"][0]     # other tools unchanged
    # And the label is true: the old rule covers nothing, the folder rule does.
    assert grants.check("move_path", {**MOVE, "destination": "/tmp/wisp-test/else/a.pdf"}) == "deny"
    grants._CACHE["move_path"]["deny"] = []
    assert grants.check("move_path", {**MOVE, "destination": "/tmp/wisp-test/else/a.pdf"}) is None
    assert grants.check("move_path", MOVE) == "allow"


def test_the_label_is_never_stored():
    _seed()
    grants.all_grants()
    assert "summary" not in json.dumps(grants._CACHE)
    assert "summary" not in grants.GRANTS_PATH.read_text()


def test_the_permissions_reply_carries_the_label_and_remove_still_works(monkeypatch):
    _seed()
    # This remove also drops a block rule of the same scope, which only the Wisp app may do.
    monkeypatch.setattr(main, "_require_wisp_app", lambda request, reason: None)
    client = TestClient(main.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    rows = client.get("/permissions").json()["grants"]["move_path"]["allow"]
    assert [r.get("summary") for r in rows if r["scope"] == ""] == [grants.INERT_MOVE_RULE]
    assert client.delete("/permissions/move_path?scope=").json() == {"ok": True}
    rows = client.get("/permissions").json()["grants"]["move_path"]["allow"]
    assert [r["scope"] for r in rows] == ["/tmp/wisp-test/out"] and "summary" not in rows[0]


def test_settings_shows_the_services_label():
    text = (APP / "QuickSettingsText.swift").read_text()
    rule = text[text.index("struct StandingRule: Identifiable, Equatable"):]
    assert 'var note: String = ""' in rule
    summary = rule[rule.index("var summary: String {"):]
    assert summary.index("if !note.isEmpty { return note }") < summary.index('"every action"')
    assert 'note: row["summary"] as? String ?? ""' in rule
