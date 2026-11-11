"""Owner decision 2026-11-10 (0.5.0 rc3): an "Always allow" for move_path saved
before rc3 has an empty, tool-wide scope. It no longer covers any move, so Wisp
shows the card again and the answer saves a rule scoped to the destination
folder. The old rule stays on disk and in the Settings list, where it can be
removed. Other tools' tool-wide rules are unchanged. Rules live in a temporary
file; nothing is moved, launched or contacted."""
from __future__ import annotations

import json

import pytest

import service.main  # noqa: F401 — registers every tool
from service.safety import grants, policy
from service.tools.registry import REGISTRY

MOVE = {"source": "/tmp/wisp-test/in/a.pdf", "destination": "/tmp/wisp-test/out/a.pdf"}


@pytest.fixture(autouse=True)
def isolated_grants(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(policy, "_FULL_ACCESS", False, raising=False)


def _old_move_rule():
    grants._CACHE["move_path"] = {"allow": [{"scope": "", "granted_at": 1.0}], "deny": []}


def _tier(args):
    return policy.decide(REGISTRY["move_path"].category, args, tool="move_path").tier


def test_an_old_tool_wide_move_rule_asks_again():
    _old_move_rule()
    assert grants.check("move_path", MOVE) is None
    assert _tier(MOVE) is policy.Tier.CONFIRM


def test_a_fresh_folder_rule_allows_moves_into_that_folder_only():
    _old_move_rule()
    saved = grants.grant("move_path", MOVE)
    assert saved["ok"] is True and saved["scope"] == "/tmp/wisp-test/out"
    assert _tier({"source": "/tmp/wisp-test/in/b.pdf",
                  "destination": "/tmp/wisp-test/out/b.pdf"}) is policy.Tier.ALLOW
    assert _tier({"source": "/tmp/wisp-test/in/b.pdf",
                  "destination": "/tmp/wisp-test/elsewhere/b.pdf"}) is policy.Tier.CONFIRM


def test_other_tools_tool_wide_rules_still_cover_every_call():
    _old_move_rule()
    grants._CACHE["write_file"] = {"allow": [{"scope": ""}], "deny": []}
    grants._CACHE["web_fetch"] = {"allow": [{"scope": ""}], "deny": []}
    assert grants.check("write_file", {"path": "/tmp/wisp-test/any/a.txt"}) == "allow"
    assert grants.check("web_fetch", {"url": "https://example.invalid/x"}) == "allow"


def test_a_tool_wide_move_block_still_blocks():
    grants._CACHE["move_path"] = {"allow": [], "deny": [{"scope": ""}]}
    assert grants.check("move_path", MOVE) == "deny"


def test_the_old_rule_is_still_listed_kept_on_disk_and_removable():
    _old_move_rule()
    grants.grant("move_path", MOVE)   # a save rewrites the file with both rules
    listed = grants.all_grants()["move_path"]["allow"]
    assert {r["scope"] for r in listed} == {"", "/tmp/wisp-test/out"}
    on_disk = json.loads(grants.GRANTS_PATH.read_text())["move_path"]["allow"]
    assert {r["scope"] for r in on_disk} == {"", "/tmp/wisp-test/out"}
    assert grants.revoke("move_path", "") is True
    assert [r["scope"] for r in grants.all_grants()["move_path"]["allow"]] == ["/tmp/wisp-test/out"]


def test_a_widened_move_allow_with_a_destination_saves_its_folder():
    # grant(scoped=False) used to store "", which no longer covers a move: it
    # would say "saved" and still ask every time. With a destination it saves
    # that folder instead.
    saved = grants.grant("move_path", MOVE, scoped=False)
    assert saved["ok"] is True and saved["scope"] == "/tmp/wisp-test/out"
    assert grants.check("move_path", MOVE) == "allow"
    # A block widened the same way stays tool-wide.
    assert grants.grant("move_path", MOVE, decision="deny", scoped=False)["scope"] == ""
