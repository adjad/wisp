"""Additive policy authoring and conservative full-policy review.

This does not publish policy. Unknown/overlapping selectors require review;
Tailnet grants are additive, so this fragment cannot narrow existing grants.
"""
import copy
import re

PRIMARY = "100.94.211.115"
TAG = "tag:wisp-inference"


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+", value):
        raise ValueError("invalid_tailnet_identity")
    return value


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", value) or value in {"root", "nobody", "daemon"}:
        raise ValueError("invalid_nonroot_user")
    return value


def fragment(owner, user):
    identity(owner)
    username(user)
    return {
        "tagOwners": {TAG: [owner]},
        "grants": [{"src": [PRIMARY], "dst": [TAG], "ip": ["tcp:22", "tcp:443", "tcp:8443"]}],
        "ssh": [{"action": "check", "src": [owner], "dst": [TAG], "users": [user], "checkPeriod": "always"}],
        "tests": [{"src": PRIMARY, "proto": "tcp", "accept": [TAG + ":22", TAG + ":443", TAG + ":8443"],
                   "deny": [TAG + ":80", TAG + ":8000", TAG + ":8765", TAG + ":8766"]},
                  {"src": PRIMARY, "proto": "udp", "deny": [TAG + ":22", TAG + ":443", TAG + ":8443"]}],
        "sshTests": [{"src": owner, "dst": [TAG], "check": [user], "deny": ["root"]}],
    }


def valid_schema(policy):
    if not isinstance(policy, dict):
        return False
    arrays = ("grants", "ssh", "acls", "nodeAttrs", "tests", "sshTests")
    mappings = ("tagOwners", "groups", "hosts", "autoApprovers", "ipsets")
    return (all(key not in policy or isinstance(policy[key], list) and all(isinstance(v, dict) for v in policy[key]) for key in arrays)
            and all(key not in policy or isinstance(policy[key], dict) for key in mappings)
            and all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in policy.get("tagOwners", {}).values()))


def render(existing, owner, user):
    """Preserve every existing rule. Warnings are separate from the policy JSON."""
    if not valid_schema(existing):
        raise ValueError("malformed_policy")
    result = copy.deepcopy(existing)
    addition = fragment(owner, user)
    owners = result.setdefault("tagOwners", {}).setdefault(TAG, [])
    if owner not in owners:
        owners.append(owner)
    for key in ("grants", "ssh", "tests", "sshTests"):
        rules = result.setdefault(key, [])
        for rule in addition[key]:
            if rule not in rules:
                rules.append(rule)
    return result


def review(policy, owner, user):
    """Deliberately conservative: additional access rules are unresolved.

    A general selector solver without the full device/group inventory cannot
    prove disjointness. Block rather than claiming restrictive exclusivity.
    """
    if not valid_schema(policy):
        return ["malformed_policy"]
    exact = fragment(owner, user)
    issues = []
    for key in ("tests", "sshTests"):
        if policy.get(key) != exact[key]:
            issues.append("incomplete_policy_test_" + key)
    for key in ("grants", "ssh"):
        if policy.get(key) != exact[key]:
            issues.append("broader_or_unresolved_" + key)
    if policy.get("acls"):
        issues.append("legacy_acl_requires_review")
    if policy.get("nodeAttrs"):
        issues.append("node_attributes_require_review")
    if policy.get("tagOwners", {}).get(TAG) != [owner]:
        issues.append("tag_ownership_requires_review")
    if set(policy) - {"grants", "ssh", "acls", "nodeAttrs", "tagOwners", "tests", "sshTests", "groups", "hosts", "autoApprovers", "ipsets", "randomizeClientPort", "disableIPv4"}:
        issues.append("unknown_policy_syntax")
    return issues


def funnel_disabled(config):
    if not isinstance(config, dict):
        return False
    allowed = {"TCP", "Web", "AllowFunnel", "Foreground", "ETag"}
    if set(config) - allowed or any(k in config and not isinstance(config[k], dict) for k in ("TCP", "Web", "AllowFunnel", "Foreground")) or config.get("AllowFunnel"):
        return False
    foreground = config.get("Foreground", {})
    return isinstance(foreground, dict) and all(funnel_disabled(v) for v in foreground.values())
