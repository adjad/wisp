"""manage_contacts create: what it writes and what its receipt admits.

The create card names only the new contact, so a number or address passed with
create is not written; the receipt has to say so instead of reading as saved.
Contacts is never touched here: `_osa` is replaced and its script inspected.
"""
from __future__ import annotations

import subprocess

import pytest

from service.router.model_led import BuiltinCompletion, builtin_completion_verified
from service.safety import policy
from service.tools import contacts_tools, registry

TOOL = registry.REGISTRY["manage_contacts"]


@pytest.fixture
def scripts(monkeypatch):
    seen = []

    def osa(script):
        seen.append(script)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(contacts_tools, "_osa", osa)
    return seen


def test_create_without_a_value_keeps_its_plain_receipt(scripts):
    result = contacts_tools.manage_contacts("create", "John Smith")
    assert result == "Created contact John Smith."
    assert type(result) is BuiltinCompletion and builtin_completion_verified(TOOL, result)
    assert 'first name:"John", last name:"Smith"' in scripts[0]


@pytest.mark.parametrize("value, kind, action", [
    ("555-0142", "phone number", "add_phone"),
    ("  john.fixture@example.com ", "email address", "add_email"),
])
def test_create_with_a_value_says_the_value_was_not_saved(scripts, value, kind, action):
    result = contacts_tools.manage_contacts("create", "John Smith", value)
    shown = value.strip()
    assert result.startswith(f"Created contact John Smith. The {kind} {shown} was NOT saved")
    assert f"action='{action}'" in result
    # Still the verified receipt for the card that was made.
    assert type(result) is BuiltinCompletion and builtin_completion_verified(TOOL, result)
    assert not registry.not_done_receipt(TOOL, result)


def test_create_writes_only_what_its_card_shows(scripts):
    """The card for create names the contact and no number, so the script adds none."""
    args = {"action": "create", "name": "John Smith", "value": "555-0142"}
    assert "555-0142" not in policy._contact_change(args)
    contacts_tools.manage_contacts(**args)
    assert len(scripts) == 1 and "555-0142" not in scripts[0]
    assert "make new phone" not in scripts[0] and "make new email" not in scripts[0]


def test_a_failed_create_claims_nothing(monkeypatch):
    monkeypatch.setattr(contacts_tools, "_osa", lambda script: subprocess.CompletedProcess(
        [], 1, stdout="", stderr="synthetic Contacts failure"))
    result = contacts_tools.manage_contacts("create", "John Smith", "555-0142")
    assert result == "(could not create contact: synthetic Contacts failure)"
    assert type(result) is str
