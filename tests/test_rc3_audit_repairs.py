"""Audit repairs on the rc3 fix batch.

A move into an existing folder named without a trailing "/" is remembered as
that folder, not its parent: the same rule move_path itself uses. The grants
store is a temp file; nothing is moved.
"""
from __future__ import annotations

import pytest

import service.main  # noqa: F401 - registers every tool
from service.safety import grants, policy
from service.tools.registry import REGISTRY


@pytest.fixture(autouse=True)
def isolated_grants(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(policy, "_FULL_ACCESS", False, raising=False)


def _tier(args: dict) -> policy.Tier:
    return policy.decide(REGISTRY["move_path"].category, args, tool="move_path").tier


def test_a_move_into_an_existing_folder_is_scoped_to_that_folder(tmp_path):
    docs = tmp_path / "Documents"
    (tmp_path / "Desktop").mkdir()
    docs.mkdir()
    into_docs = {"source": str(tmp_path / "a.pdf"), "destination": str(docs)}
    assert grants.scope_for("move_path", into_docs) == str(docs)
    assert grants.grant("move_path", into_docs)["scope"] == str(docs)
    # Another move into the same folder runs; one into a sibling folder asks.
    assert _tier({"source": str(tmp_path / "b.pdf"), "destination": str(docs)}) is policy.Tier.ALLOW
    assert _tier({"source": str(tmp_path / "b.pdf"),
                  "destination": str(tmp_path / "Desktop")}) is policy.Tier.CONFIRM


def test_a_move_to_a_new_name_is_still_scoped_to_its_folder(tmp_path):
    dest = tmp_path / "Documents" / "renamed.pdf"
    assert grants.scope_for("move_path", {"source": "a", "destination": str(dest)}) == str(dest.parent)
    assert grants.scope_for("move_path", {"source": "a", "destination": f"{tmp_path}/Documents/"}) == \
        str(tmp_path / "Documents")
