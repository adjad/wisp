"""H-1: a standing "always allow" must never lift an always-confirm action.

Grantability used to be a hand-kept list of tool names, which silently missed
update_event, clear_reminders, clear_past_reminders, forward_email and
http_request, and policy.decide() consulted grants before the always-confirm
categories. These tests are driven by the tool REGISTRY, so a future tool in an
always-confirm category is covered without anyone remembering to list it.
"""
from __future__ import annotations

import asyncio

import pytest

import service.main  # noqa: F401 — registers every tool
from service.safety import grants, policy
from service.tools.registry import REGISTRY

ALWAYS = sorted(name for name, tool in REGISTRY.items()
                if tool.category in policy.ALWAYS_CONFIRM_CATEGORIES)
CASES = [(name, {"cmd": "ls"} if REGISTRY[name].category == "shell" else {}) for name in ALWAYS]


@pytest.fixture(autouse=True)
def isolated_grants(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(policy, "_FULL_ACCESS", False, raising=False)


def test_the_registry_has_always_confirm_tools_to_check():
    # Guards the test itself: if the category sets were renamed, the loop below would pass vacuously.
    assert {"update_event", "clear_reminders", "clear_past_reminders", "forward_email",
            "http_request", "send_email", "cancel_event", "create_tool"} <= set(ALWAYS)


@pytest.mark.parametrize("tool,args", CASES)
def test_no_always_confirm_tool_is_grantable(tool, args):
    assert not grants.is_grantable(tool, args), f"{tool} must not offer an always-allow"
    result = grants.grant(tool, args, decision="allow")
    assert result.get("ok") is False, f"{tool}: an allow grant was stored"
    assert grants.check(tool, args) is None


@pytest.mark.parametrize("tool,args", CASES)
def test_a_stale_allow_grant_is_ignored(tool, args):
    # A grant written by an older build (or by editing grants.json) must not be honoured.
    grants._CACHE[tool] = {"allow": [{"scope": ""}], "deny": []}
    assert grants.check(tool, args) is None, f"{tool}: a stored allow grant was honoured"
    decision = policy.decide(REGISTRY[tool].category, args, tool=tool)
    assert decision.tier is not policy.Tier.ALLOW, f"{tool}: policy allowed it on a stale grant"


@pytest.mark.parametrize("tool,args", CASES)
def test_a_deny_grant_still_blocks(tool, args):
    # The user can always say "never"; only "always" is withheld.
    assert grants.grant(tool, args, decision="deny", scoped=False).get("ok") is not False
    assert policy.decide(REGISTRY[tool].category, args, tool=tool).tier is policy.Tier.DENY


def test_policy_ignores_a_grant_for_every_always_confirm_category():
    for category in policy.ALWAYS_CONFIRM_CATEGORIES:
        grants._CACHE["fake_tool"] = {"allow": [{"scope": ""}], "deny": []}
        # check() is name-based for an unregistered tool, so this isolates the policy-side guard.
        assert policy.decide(category, {}, tool="fake_tool").tier is not policy.Tier.ALLOW, category


def test_ordinary_tools_are_still_grantable():
    # The fix must not turn grants off wholesale.
    for tool, args in [("run_shell", {"cmd": "ls -la"}), ("write_file", {"path": "/tmp/x/a.txt"}),
                       ("web_fetch", {"url": "https://example.com/a"})]:
        assert tool in REGISTRY and grants.is_grantable(tool, args), tool
    assert grants.grant("web_fetch", {"url": "https://example.com/a"}).get("ok") is True
    assert grants.check("web_fetch", {"url": "https://example.com/b"}) == "allow"


def _resolve_card(tool, args, approved, scope):
    """Exercise the real emitted card/resolution path, never the tool itself."""
    from service.agent.approver import InteractiveApprover
    events = []
    action = {"id": "synthetic-card", "tool": tool, "args": args}

    async def run():
        async def emit(event):
            events.append(event)
            assert approver.resolve(action["id"], approved, scope)

        approver = InteractiveApprover(emit, timeout=1)
        result = await approver.confirm(action)
        assert approver._pending == {} and approver._actions == {}
        assert not approver.resolve(action["id"], approved, scope)
        return result

    assert asyncio.run(run()) is approved
    assert len(events) == 1
    assert events[0]["type"] == "confirm"
    assert events[0]["tool"] == tool and events[0]["args"] == args
    return events[0]


@pytest.mark.parametrize("tool,args", CASES)
@pytest.mark.parametrize("scope", ["once", "always", "never"])
def test_the_confirmation_card_does_not_offer_always_for_these_tools(tool, args, scope):
    approved = scope != "never"
    card = _resolve_card(tool, args, approved, scope)
    assert card["grantable"] is False
    # Even a stale client submitting 'always' cannot persist a protected allow.
    assert grants.check(tool, args) == ("deny" if scope == "never" else None)
    assert policy.decide(REGISTRY[tool].category, args, tool).tier is not policy.Tier.ALLOW
    if scope == "once":
        assert _resolve_card(tool, args, True, "once")["grantable"] is False
        assert grants.check(tool, args) is None


@pytest.mark.parametrize("scope", ["once", "always", "never"])
def test_ordinary_confirmation_cards_keep_scoped_grants(tmp_path, scope):
    tool, args = "write_file", {"path": str(tmp_path / "notes.txt"), "content": "synthetic"}
    card = _resolve_card(tool, args, scope != "never", scope)
    assert card["grantable"] is True
    assert card["scope_hint"] == str(tmp_path)
    expected = {"once": None, "always": "allow", "never": "deny"}[scope]
    assert grants.check(tool, args) == expected
    assert policy.decide(REGISTRY[tool].category, args, tool).tier is {
        "once": policy.Tier.CONFIRM, "always": policy.Tier.ALLOW, "never": policy.Tier.DENY,
    }[scope]
