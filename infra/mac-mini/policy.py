"""Additive policy authoring and conservative full-policy review.

This does not publish policy. Unknown/overlapping selectors require review;
Tailnet grants are additive, so this fragment cannot narrow existing grants.
"""
import copy
import re
import ipaddress
import hashlib
import json


def strict_document(raw):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result: raise ValueError('duplicate_policy_key')
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def backup_evidence(before, after, plan):
    return {'schema_version':1, 'before':before, 'after':after,
            'before_sha256':hashlib.sha256(before.encode()).hexdigest(),
            'after_sha256':hashlib.sha256(after.encode()).hexdigest(),
            'plan_sha256':hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',',':')).encode()).hexdigest()}


def verify_backups(evidence, policy, plan):
    try:
        if (not isinstance(evidence,dict) or not isinstance(evidence.get('before'),str)
                or not isinstance(evidence.get('after'),str) or len(evidence['before'])+len(evidence['after']) > 4*1024*1024
                or evidence != backup_evidence(evidence['before'], evidence['after'], plan)):
            return False
        return (strict_document(evidence['after']) == policy
                and render(strict_document(evidence['before']), plan['tailnet_user'], plan['user']) == policy)
    except (ValueError, TypeError, KeyError, RecursionError):
        return False

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


def disjoint_context(inventory):
    if (not isinstance(inventory, dict) or set(inventory) != {'schema_version','complete','devices'}
            or type(inventory['schema_version']) is not int or inventory['schema_version'] != 1
            or inventory['complete'] is not True or not isinstance(inventory['devices'], list)
            or not 1 <= len(inventory['devices']) <= 10000):
        raise ValueError('complete_policy_inventory_required')
    addresses, protected, ids = set(), {PRIMARY}, set()
    for device in inventory['devices']:
        if (not isinstance(device, dict) or set(device) != {'id','ips','tags'}
                or not isinstance(device['id'], str) or not device['id'] or device['id'] in ids
                or not isinstance(device['ips'], list) or not device['ips']
                or not isinstance(device['tags'], list) or len(set(device['tags'])) != len(device['tags'])
                or any(not isinstance(t, str) or not re.fullmatch('tag:[a-z][a-z0-9-]*', t) for t in device['tags'])):
            raise ValueError('invalid_policy_inventory')
        ids.add(device['id'])
        for address in device['ips']:
            ip = ipaddress.ip_address(address)
            if str(ip) != address or address in addresses:
                raise ValueError('invalid_policy_inventory')
            addresses.add(address)
            if TAG in device['tags'] or PRIMARY in device['ips']:
                protected.add(address)
    if protected == {PRIMARY}:
        raise ValueError('inference_inventory_required')
    return addresses - protected


def security_projection(status):
    def peer(row):
        return {'id':row['ID'],'ips':sorted(row['TailscaleIPs']),'tags':sorted(row.get('Tags',[])),
                'host':row.get('DNSName','').rstrip('.'),'keys':sorted(row.get('sshHostKeys',[]))}
    return {'schema_version':1,'backend':status.get('BackendState'),
            'tailnet':status.get('CurrentTailnet'), 'self':peer(status['Self']),
            'peers':sorted((peer(p) for p in status.get('Peer',{}).values()),key=lambda p:p['id'])}


def inventory_binding(snapshot, plan, *, now):
    """Bind additive exceptions to a separately approved fresh full export.

    Local peer visibility cannot establish completeness. The approval pin must
    be supplied independently from the administrator's complete export review;
    this function never manufactures that approval from observed peers.
    """
    inventory=plan.get('policy_inventory')
    if inventory is None:
        return True  # review() still refuses every unsupported additive rule.
    try:
        disjoint_context(inventory)
        rows={r['id']:{'ips':sorted(r['ips']),'tags':sorted(r['tags'])} for r in inventory['devices']}
        target=rows[plan['node_id']]
        if plan['ip'] not in target['ips'] or TAG not in target['tags']:return False
        observed=snapshot['tailscale']
        peers=[observed['Self'],*observed.get('Peer',{}).values()]
        seen=set()
        for peer in peers:
            peer_id=peer['ID']
            if peer_id in seen or rows.get(peer_id)!={'ips':sorted(peer['TailscaleIPs']),'tags':sorted(peer.get('Tags',[]))}:return False
            seen.add(peer_id)
        if seen!=set(rows) or PRIMARY not in observed['Self']['TailscaleIPs']:return False
        target_peer=next(p for p in peers if p['ID']==plan['node_id'])
        if target_peer.get('DNSName','').rstrip('.')!=plan['host']:return False
        approval=snapshot['policy_inventory_approval']
        canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':')).encode()
        digest=lambda value:hashlib.sha256(canonical(value)).hexdigest()
        expected={'schema_version','source','complete_export_reviewed','target','inventory_sha256',
                  'observed_sha256','exported_policy_sha256','issued_at','expires_at'}
        return (set(approval)==expected and type(approval['schema_version']) is int and approval['schema_version']==1
                and approval['source']=='independent-admin-export' and approval['complete_export_reviewed'] is True
                and approval['target']=={k:plan[k] for k in ('host','node_id','ip')}
                and approval['inventory_sha256']==digest(inventory)
                and approval['observed_sha256']==digest(security_projection(observed))
                and approval['exported_policy_sha256']==snapshot['policy_backups']['before_sha256']
                and type(approval['issued_at']) is int and type(approval['expires_at']) is int
                and approval['issued_at']<=now<approval['expires_at']<=approval['issued_at']+900
                and plan.get('policy_inventory_approval_sha256')==digest(approval))
    except (KeyError,ValueError,TypeError,AttributeError,StopIteration):
        return False


def disjoint_rule(row, kind, safe):
    """A deliberately decidable subset: concrete inventory IP destinations only.

    Different tags are not disjoint (devices can carry multiple tags). No aliases,
    CIDRs, wildcard, app capabilities, via, or indirect selector is interpreted.
    """
    try:
        def destinations(values):
            return isinstance(values, list) and 0 < len(values) <= 32 and len(set(values)) == len(values) and all(v in safe for v in values)
        def sources(values):
            if not isinstance(values, list) or not 0 < len(values) <= 32 or len(set(values)) != len(values): return False
            for value in values:
                try: identity(value)
                except ValueError:
                    if str(ipaddress.ip_address(value)) != value: return False
            return True
        def ports(values):
            return isinstance(values, list) and 0 < len(values) <= 32 and len(set(values)) == len(values) and all(
                re.fullmatch(r'(tcp|udp):([1-9][0-9]{0,4})', v) and 1 <= int(v.split(':')[1]) <= 65535 for v in values)
        if kind == 'grants':
            return set(row) == {'src','dst','ip'} and sources(row['src']) and destinations(row['dst']) and ports(row['ip'])
        if kind == 'ssh':
            return (set(row) in ({'action','src','dst','users'}, {'action','src','dst','users','checkPeriod'})
                and row['action'] in ('accept','check') and sources(row['src']) and destinations(row['dst'])
                and isinstance(row['users'], list) and 0 < len(row['users']) <= 32
                and all(username(v) for v in row['users'])
                and (row.get('checkPeriod') == 'always' if row['action'] == 'check' else 'checkPeriod' not in row))
        if kind == 'acls':
            if set(row) != {'action','src','dst','proto'} or row['action'] != 'accept' or not sources(row['src']) or row['proto'] not in ('tcp','udp'): return False
            entries = row['dst']
            return (isinstance(entries, list) and 0 < len(entries) <= 32 and len(set(entries)) == len(entries)
                and all(value.rsplit(':',1)[0] in safe and ports([row['proto']+':'+value.rsplit(':',1)[1]]) for value in entries))
        if kind == 'nodeAttrs':
            return set(row) == {'target','attr'} and destinations(row['target']) and row['attr'] == ['funnel']
        if kind == 'tests':
            if set(row) - {'src','proto','accept','deny'} or not {'src','proto'} <= set(row) or not sources([row['src']]) or row['proto'] not in ('tcp','udp'): return False
            return bool(set(row) & {'accept','deny'}) and all(disjoint_rule({'action':'accept','src':[row['src']], 'proto':row['proto'],'dst':row[key]}, 'acls', safe) for key in ('accept','deny') if key in row)
        if kind == 'sshTests':
            return (set(row) <= {'src','dst','accept','check','deny'} and {'src','dst'} <= set(row)
                and sources([row['src']]) and destinations(row['dst']) and bool(set(row) & {'accept','check','deny'})
                and all(isinstance(row[key],list) and 0 < len(row[key]) <= 32 and all(v == 'root' or username(v) for v in row[key]) for key in ('accept','check','deny') if key in row))
    except (ValueError, TypeError, KeyError, IndexError):
        return False
    return False


def review(policy, owner, user, inventory=None):
    """Deliberately conservative: additional access rules are unresolved.

    A general selector solver without the full device/group inventory cannot
    prove disjointness. Block rather than claiming restrictive exclusivity.
    """
    if not valid_schema(policy):
        return ["malformed_policy"]
    exact = fragment(owner, user)
    issues = []
    try:
        safe = disjoint_context(inventory) if inventory is not None else set()
    except (ValueError, TypeError):
        return ['invalid_policy_inventory']
    for key in ("tests", "sshTests"):
        rows = policy.get(key, [])
        if any(rows.count(required) != 1 for required in exact[key]):
            issues.append("incomplete_policy_test_" + key)
        related = [row for row in rows if not disjoint_rule(row, key, safe)]
        if len(rows) > 256 or any(rows.count(row) != 1 for row in rows) or not additional_denials(related, exact[key], key, owner, user):
            issues.append("invalid_additional_policy_test_" + key)
    for key in ("grants", "ssh"):
        rows = policy.get(key, [])
        if (any(rows.count(rule) != 1 for rule in exact[key]) or len(rows) > 256
                or any(rows.count(row) != 1 or row not in exact[key] and not disjoint_rule(row, key, safe) for row in rows)):
            issues.append("broader_or_unresolved_" + key)
    if any(not disjoint_rule(row, 'acls', safe) for row in policy.get("acls", [])):
        issues.append("legacy_acl_requires_review")
    if any(not disjoint_rule(row, 'nodeAttrs', safe) for row in policy.get("nodeAttrs", [])):
        issues.append("node_attributes_require_review")
    if policy.get("tagOwners", {}).get(TAG) != [owner]:
        issues.append("tag_ownership_requires_review")
    try:
        for tag, owners in policy.get('tagOwners', {}).items():
            if not re.fullmatch('tag:[a-z][a-z0-9-]*', tag) or not owners or len(set(owners)) != len(owners) or any(not identity(v) for v in owners): raise ValueError
        for group, members in policy.get('groups', {}).items():
            if not re.fullmatch('group:[a-z][a-z0-9-]*', group) or not isinstance(members,list) or not members or any(not identity(v) for v in members): raise ValueError
        for host, address in policy.get('hosts', {}).items():
            if not re.fullmatch('[a-z][a-z0-9-]*',host) or address not in safe: raise ValueError
        if policy.get('ipsets') or policy.get('autoApprovers'): raise ValueError
        if policy.get('disableIPv4',False) is not False or policy.get('randomizeClientPort',False) is not False: raise ValueError
    except (ValueError, TypeError):
        issues.append('indirect_policy_access_requires_review')
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
