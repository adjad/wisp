"""Additive policy authoring and conservative full-policy review.

This does not publish policy. Unknown/overlapping selectors require review;
Tailnet grants are additive, so this fragment cannot narrow existing grants.
"""
import copy
import re
import ipaddress

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
        rows = policy.get(key, [])
        if any(rows.count(required) != 1 for required in exact[key]):
            issues.append("incomplete_policy_test_" + key)
        if not additional_denials(rows, exact[key], key, owner, user):
            issues.append("invalid_additional_policy_test_" + key)
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


def additional_denials(rows, required, key, owner, user):
    """Only concrete inventory denial assertions; never additional access rules.

    Network extras cover non-primary IPv4 Tailnet peers against the fixed mini
    tag and known service ports. SSH extras cover concrete Tailnet identities
    and accounts; denying the designated owner's approved account contradicts
    the required check assertion. Duplicate assertions refuse even across rows.
    """
    if len(rows) > 256:
        return False
    seen_rows, seen_assertions = [], set()
    for row in rows:
        if row in seen_rows:
            return False
        seen_rows.append(row)
        if row in required:
            for value in row.get('deny', []):
                seen_assertions.add((row['src'], row.get('proto'), value))
    for row in rows:
        if row in required:
            continue
        if (set(row) != ({'src', 'proto', 'deny'} if key == 'tests' else {'src', 'dst', 'deny'})
                or not isinstance(row.get('deny'), list) or not 1 <= len(row['deny']) <= 32
                or not all(isinstance(value, str) for value in row['deny'])):
            return False
        source = row['src']
        if key == 'tests':
            try:
                peer = ipaddress.IPv4Address(source)
            except (ValueError, TypeError, ipaddress.AddressValueError):
                return False
            if (not isinstance(source, str) or str(peer) != source or source == PRIMARY
                    or peer not in ipaddress.IPv4Network('100.64.0.0/10') or row['proto'] not in ('tcp', 'udp')
                    or any(value not in {TAG + ':' + str(port) for port in (22, 80, 443, 8000, 8443, 8765, 8766)}
                           for value in row['deny'])):
                return False
        else:
            try:
                identity(source)
                for account in row['deny']:
                    if account != 'root':
                        username(account)
            except ValueError:
                return False
            if row['dst'] != [TAG] or source == owner and user in row['deny']:
                return False
        for value in row['deny']:
            assertion = (source, row.get('proto'), value)
            if assertion in seen_assertions:
                return False
            seen_assertions.add(assertion)
    return True


def funnel_disabled(config):
    if not isinstance(config, dict):
        return False
    allowed = {"TCP", "Web", "AllowFunnel", "Foreground", "ETag"}
    if set(config) - allowed or any(k in config and not isinstance(config[k], dict) for k in ("TCP", "Web", "AllowFunnel", "Foreground")) or config.get("AllowFunnel"):
        return False
    foreground = config.get("Foreground", {})
    return isinstance(foreground, dict) and all(funnel_disabled(v) for v in foreground.values())
