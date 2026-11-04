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
            "send_email", "cancel_event", "create_tool"} <= set(ALWAYS)


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


@pytest.fixture
def publication_state(monkeypatch):
    """Only synthetic memory epochs; the autouse fixture owns grants.json."""
    from service.publication_authority import Authority
    for module, label in ((policy, "test-action-policy"), (grants, "test-action-grants")):
        authority = Authority(label)
        authority.publish()
        monkeypatch.setattr(module, "_publication", authority)
        monkeypatch.setattr(module, "_proof_known", True)


@pytest.mark.parametrize("module", [policy, grants])
def test_publication_witness_is_memory_only_and_never_rearms(module, publication_state, monkeypatch):
    first = module.publication_witness()
    assert first is not None and first.current()
    def forbidden(*args, **kwargs):
        raise AssertionError("witness must not load, construct or publish")
    monkeypatch.setattr(module, "Authority", forbidden)
    monkeypatch.setattr(module, "_load_snapshot", forbidden)
    monkeypatch.setattr(module._publication, "publish", forbidden)
    again = module.publication_witness()
    assert again is not None and again.current() and first.current()
    module._publication.withdraw()
    assert not first.current()
    assert module.publication_witness() is None
    assert module.publication_witness() is None


@pytest.mark.parametrize("setter,getter", [
    (policy.set_read_only, policy.read_only), (policy.set_full_access, policy.full_access),
])
def test_publication_mode_aba_never_revives_old_witness(setter, getter, publication_state):
    original = getter()
    before = policy.publication_witness()
    setter(not original)
    middle = policy.publication_witness()
    assert before is not None and not before.current()
    assert middle is not None and middle.current()
    setter(original)
    after = policy.publication_witness()
    assert getter() == original
    assert after is not None and after.current()
    assert not before.current() and not middle.current()


def test_publication_grant_revoke_aba_preserves_decisions(publication_state):
    before = grants.publication_witness()
    args = {"url": "https://example.invalid/a"}
    assert grants.grant("web_fetch", args)["ok"] is True
    granted = grants.publication_witness()
    assert grants.check("web_fetch", args) == "allow"
    assert before is not None and not before.current()
    assert granted is not None and granted.current()
    assert grants.revoke("web_fetch") is True
    assert grants.check("web_fetch", args) is None
    assert grants.publication_witness().current()
    assert not before.current() and not granted.current()
    assert grants.revoke("web_fetch") is False


@pytest.mark.parametrize("operation", ["read_only", "full_access", "grant", "revoke", "save"])
def test_publication_same_thread_refuses_before_any_safety_effect(operation, publication_state):
    from service.publication_authority import PublicationBusy, Witness, publication
    args = {"url": "https://example.invalid/a"}
    assert grants.grant("web_fetch", args)["ok"]
    current = Witness.combine(policy.publication_witness(), grants.publication_witness())
    cache, modes = grants.all_grants(), (policy.read_only(), policy.full_access())
    persisted = grants.GRANTS_PATH.read_bytes()
    operations = {
        "read_only": lambda: policy.set_read_only(not modes[0]),
        "full_access": lambda: policy.set_full_access(not modes[1]),
        "grant": lambda: grants.grant("web_fetch", args, decision="deny"),
        "revoke": lambda: grants.revoke("web_fetch"),
        "save": grants._save,
    }
    with publication(current):
        with pytest.raises(PublicationBusy):
            operations[operation]()
        assert current.current()
        assert grants.all_grants() == cache
        assert (policy.read_only(), policy.full_access()) == modes
        assert grants.GRANTS_PATH.read_bytes() == persisted


def test_publication_mutation_waits_for_existing_commit_lease(publication_state):
    import threading
    from service.publication_authority import publication
    before = policy.publication_witness()
    attempted, finished = threading.Event(), threading.Event()
    errors = []
    original = policy.read_only()
    def mutate():
        attempted.set()
        try:
            policy.set_read_only(not original)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()
    with publication(before):
        worker = threading.Thread(target=mutate)
        worker.start()
        assert attempted.wait(1)
        assert not finished.wait(0.02)
        assert policy.read_only() == original and before.current()
    worker.join(1)
    assert not worker.is_alive() and not errors and finished.is_set()
    assert policy.read_only() != original and not before.current()
    policy.set_read_only(original)


@pytest.mark.parametrize("short_write", [False, True])
def test_publication_failed_save_withholds_proof_without_changing_grant_result(
        short_write, publication_state, monkeypatch):
    before = grants.publication_witness()
    original = grants.private_write_bytes
    def fail(path, data):
        # grants.json is written through service.paths.private_write_bytes (0600,
        # atomic), which raises on a short write instead of returning a count.
        if path == grants.GRANTS_PATH:
            if short_write:
                raise OSError("synthetic short write")
            raise PermissionError("synthetic save failure")
        return original(path, data)
    monkeypatch.setattr(grants, "private_write_bytes", fail)
    args = {"url": "https://example.invalid/a"}
    assert grants.grant("web_fetch", args)["ok"] is True
    assert grants.check("web_fetch", args) == "allow"  # Historical cache semantics.
    assert before is not None and not before.current()
    assert grants.publication_witness() is None
    assert grants.publication_witness() is None


def test_publication_unexpected_mutator_failure_cannot_rearm(publication_state):
    before = policy.publication_witness()
    class Broken:
        def __bool__(self):
            raise ValueError("synthetic conversion failure")
    original = policy.read_only()
    with pytest.raises(ValueError, match="synthetic"):
        policy.set_read_only(Broken())
    assert policy.read_only() == original
    assert before is not None and not before.current()
    assert policy.publication_witness() is None
    policy.set_read_only(original)
    assert policy.publication_witness().current() and not before.current()


@pytest.mark.parametrize("module", [policy, grants])
def test_publication_unknown_state_cannot_be_rearmed_by_getter(module, publication_state, monkeypatch):
    monkeypatch.setattr(module, "_proof_known", False)
    module._publication.withdraw()
    assert module.publication_witness() is None
    assert module.publication_witness() is None


@pytest.mark.parametrize("raw", ["[]", "null", "not-json", '{"tool":{"allow":"bad"}}',
    '{"tool":{"allow":[{"scope":"","granted_at":null}]}}',
    '{"tool":{"allow":[],"allow":[{"scope":""}]}}'])
def test_publication_malformed_cache_keeps_legacy_read_but_withholds_proof(raw, publication_state):
    import json
    grants.GRANTS_PATH.write_text(raw)
    cache, known = grants._load_snapshot()
    assert known is False
    try:
        legacy = json.loads(raw)
        expected = legacy if isinstance(legacy, dict) else {}
    except ValueError:
        expected = {}
    assert cache == expected and grants._load() == expected


def test_publication_absent_cache_known_empty_unreadable_unknown(publication_state, monkeypatch):
    from pathlib import Path
    assert grants._load_snapshot() == ({}, True)
    grants.GRANTS_PATH.write_text("{}")
    read = Path.read_text
    def denied(path, *args, **kwargs):
        if path == grants.GRANTS_PATH:
            raise PermissionError("synthetic unreadable cache")
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", denied)
    assert grants._load_snapshot() == ({}, False)
    assert grants._load() == {}


@pytest.mark.parametrize("raw,known", [
    ("", True), ("read_only: false", True), ("[]", False),
    ("read_only: maybe", False), ("unknown_setting: true", False),
    ("shell_deny: ['[']", False),
])
def test_publication_policy_snapshot_knowledge_does_not_change_legacy_value(
        raw, known, publication_state, monkeypatch, tmp_path):
    import yaml
    path = tmp_path / "policy.yaml"
    path.write_text(raw)
    monkeypatch.setattr(policy, "POLICY_YAML", path)
    assert policy._load_snapshot() == (yaml.safe_load(raw) or {}, known)
    assert policy._load_yaml() == (yaml.safe_load(raw) or {})


@pytest.mark.parametrize("raw,known", [
    (None, True), ("{}", True),
    ('{"tool":{"allow":[{"scope":""}],"deny":[]}}', True),
    ("[]", False), ("not-json", False),
    ('{"tool":{"allow":[],"allow":[{"scope":""}]}}', False),
    ("UNREADABLE", False),
])
def test_publication_cold_grant_cache_issues_proof_only_for_known_state(
        raw, known, monkeypatch, tmp_path):
    import importlib.util
    import sys
    from pathlib import Path
    import service.paths
    path = tmp_path / "grants.json"
    if raw is not None:
        path.write_text(raw)
    monkeypatch.setattr(service.paths, "MOE_DIR", tmp_path)
    original = Path.read_text
    def read(target, *args, **kwargs):
        if target == path and raw == "UNREADABLE":
            raise PermissionError("synthetic cold cache failure")
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    name = "_wisp_cold_grant_epoch_fixture"
    spec = importlib.util.spec_from_file_location(name, grants.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    witness = module.publication_witness()
    assert (witness is not None) is known
    if known:
        assert witness.current()
    else:
        assert module.publication_witness() is None


@pytest.mark.parametrize("raw,known", [
    ("", True), ("read_only: false", True), ("[]", False),
    ("unknown_setting: true", False), ("read_only: maybe", False),
])
def test_publication_cold_policy_config_issues_proof_only_for_known_state(
        raw, known, monkeypatch):
    import importlib.util
    import io
    import sys
    from pathlib import Path
    original = Path.open
    def read(path, *args, **kwargs):
        if path == policy.POLICY_YAML:
            return io.StringIO(raw)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", read)
    name = "_wisp_cold_policy_epoch_fixture"
    spec = importlib.util.spec_from_file_location(name, policy.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    witness = module.publication_witness()
    assert (witness is not None) is known
    if known:
        assert witness.current()
    else:
        assert module.publication_witness() is None


# --- categories that used to fall to "unclassified action" ---------------------

def _mode(monkeypatch, mode):
    monkeypatch.setattr(policy, "_FULL_ACCESS", mode == "full")
    monkeypatch.setattr(policy, "_READ_ONLY", mode == "view")


def _decide(name, args):
    return policy.decide(REGISTRY[name].category, args, tool=name)


CONTACT_WRITES = [
    {"action": "create", "name": "Pat Example"},
    {"action": "add_phone", "name": "Mom", "value": "+1 555-555-0188"},
    {"action": "add_email", "name": "Mom", "value": "mom@example.com"},
    {"action": "not_an_action", "name": "Mom"},
]


@pytest.mark.parametrize("args", CONTACT_WRITES, ids=lambda a: a["action"])
def test_contact_writes_ask_even_with_full_access_and_never_offer_always(monkeypatch, args):
    # A changed number silently redirects every later send to that person.
    assert REGISTRY["manage_contacts"].category in policy.ALWAYS_CONFIRM_CATEGORIES
    for mode in ("default", "full"):
        _mode(monkeypatch, mode)
        decision = _decide("manage_contacts", args)
        assert decision.tier is policy.Tier.CONFIRM, (mode, decision)
        assert "always confirmed" in decision.reason and "unclassified" not in decision.reason
    _mode(monkeypatch, "view")
    assert _decide("manage_contacts", args).tier is policy.Tier.DENY
    assert not grants.is_grantable("manage_contacts", args)
    assert "manage_contacts" in grants.never_grantable()
    assert grants.grant("manage_contacts", args, decision="allow").get("ok") is False


def test_contact_cards_name_the_exact_change(monkeypatch):
    _mode(monkeypatch, "full")
    assert _decide("manage_contacts", CONTACT_WRITES[1]).reason.startswith(
        "adds the phone number +1 555-555-0188 to the contact matching “Mom”")
    assert _decide("manage_contacts", CONTACT_WRITES[2]).reason.startswith(
        "adds the email address mom@example.com to the contact matching “Mom”")
    assert _decide("manage_contacts", CONTACT_WRITES[0]).reason.startswith(
        "creates a new contact named “Pat Example”")


LOCAL_WRITES = [
    ("create_note", {"title": "Groceries", "body": "milk"}, "creates the note “Groceries” in Notes"),
    ("append_note", {"title": "Groceries", "text": "eggs"}, "adds to the note “Groceries” in Notes"),
    ("set_timer", {"duration": "10 minutes", "label": "pasta"}, "sets a timer for 10 minutes"),
    ("set_timer", {"kind": "alarm", "time": "7:30am"}, "sets an alarm for 7:30am"),
    ("set_timer", {"kind": "sleep", "duration": "30 minutes"}, "pauses playback in 30 minutes"),
    ("manage_timers", {"action": "cancel", "label": "pasta"}, "cancels the timer pasta"),
    ("manage_timers", {"action": "cancel_all"}, "cancels every timer and alarm"),
    ("manage_timers", {"action": "stopwatch_start"}, "starts the stopwatch"),
    ("manage_timers", {"action": "stopwatch_lap"}, "records a lap on the stopwatch"),
    ("manage_timers", {"action": "stopwatch_stop"}, "stops the stopwatch"),
    ("manage_timers", {"action": "stopwatch_"}, "changes your timers"),         # no "s the stopwatch"
    ("manage_timers", {"action": ["list"]}, "changes your timers"),     # malformed: write rules
]


@pytest.mark.parametrize("tool,args,reason", LOCAL_WRITES, ids=[f"{t}:{r}" for t, _, r in LOCAL_WRITES])
def test_note_and_timer_writes_follow_the_access_mode(monkeypatch, tool, args, reason):
    _mode(monkeypatch, "default")
    decision = _decide(tool, args)
    assert (decision.tier, decision.reason) == (policy.Tier.CONFIRM, reason)
    _mode(monkeypatch, "full")
    assert _decide(tool, args).tier is policy.Tier.ALLOW
    _mode(monkeypatch, "view")
    decision = _decide(tool, args)
    assert decision.tier is policy.Tier.DENY and "view-only" in decision.reason
    assert grants.is_grantable(tool, args)      # ordinary writes keep "always allow"


@pytest.mark.parametrize("args", [{}, {"action": ""}, {"action": "list"}, {"action": " LIST "},
                                  {"action": "stopwatch_read"}])
@pytest.mark.parametrize("mode", ["default", "full", "view"])
def test_reading_timers_never_asks(monkeypatch, args, mode):
    # "how much time is left on the pasta timer" used to get a card, and a refusal in view-only.
    _mode(monkeypatch, mode)
    assert _decide("manage_timers", args).tier is policy.Tier.ALLOW


MEMORY_CARDS = [
    ("remember", {"fact": "Mom's  birthday\nis May 3"}, "saving a memory: Mom's birthday is May 3"),
    ("remember", {"fact": "Mom's birthday is May 4", "supersedes_id": 12},
     "saving a memory: Mom's birthday is May 4 (replacing memory 12)"),
    ("forget", {"query": "old address"}, "forgetting: old address"),
    ("save_knowledge", {"title": "Lease terms", "text": "x"}, "saving to your knowledge: Lease terms"),
    ("edit_knowledge", {"record_id": "a" * 32, "expected_revision": "1", "title": "Lease", "text": "x"},
     "replacing saved knowledge item " + "a" * 32 + " with “Lease”"),
    ("delete_knowledge", {"record_id": "a" * 32, "expected_revision": "1"},
     "deleting saved knowledge item " + "a" * 32),
    ("move_path", {"source": "/Users/x/Desktop/a.txt", "destination": "/Users/x/Documents/"},
     "moving /Users/x/Desktop/a.txt to /Users/x/Documents/"),
    ("move_path", {"source": "/Users/x/a\n(nothing changes)", "destination": "/tmp/b"},
     "moving /Users/x/a (nothing changes) to /tmp/b"),           # one line: no spoofed second line
    ("write_file", {"path": "/Users/x/Desktop/a.txt", "content": "x"}, "writing /Users/x/Desktop/a.txt"),
]


@pytest.mark.parametrize("tool,args,reason", MEMORY_CARDS, ids=[f"{c[0]}-{i}" for i, c in enumerate(MEMORY_CARDS)])
def test_file_category_cards_name_what_changes(monkeypatch, tool, args, reason):
    # The memory tools take no `path`: their cards used to read "writing " / "deleting ".
    _mode(monkeypatch, "default")
    decision = _decide(tool, args)
    assert decision.tier is policy.Tier.CONFIRM and decision.reason == reason
    assert REGISTRY[tool].category in ("fs_write", "fs_delete")     # categories unchanged


def test_a_long_memory_is_cut_to_fit_the_card(monkeypatch):
    _mode(monkeypatch, "default")
    reason = _decide("remember", {"fact": "x" * 5000}).reason
    assert reason.startswith("saving a memory: xxx") and reason.endswith("…") and len(reason) < 260


@pytest.mark.parametrize("mode", ["default", "view"])
def test_no_registered_tool_is_unclassified(monkeypatch, mode):
    _mode(monkeypatch, mode)
    loose = {name: decision.reason for name, tool in REGISTRY.items()
             if (decision := policy.decide(tool.category, {}, tool=name)).reason.startswith("unclassified")}
    assert not loose, f"tools with no policy rule for their category: {loose}"


# --- tool names the policy refers to must exist ---------------------------------

# Imported from model_led inside a function, so not a table of these modules.
_EXTERNAL_TABLES = {"OPAQUE_OUTBOUND_TOOLS"}


def _referenced_tool_names(module):
    """Every literal tool name a module's code compares `tool` against, plus
    every module-level *_TOOLS table, as written in the source."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(module))
    names, unresolved = set(), set()

    def strings(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            return set().union(*(strings(e) for e in node.elts))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("frozenset", "set") and node.args:
            return strings(node.args[0])
        if isinstance(node, ast.Name) and node.id.lstrip("_")[:1].isupper():   # a table, not a local
            value = getattr(module, node.id, None)
            if isinstance(value, (set, frozenset, dict)):
                return {v for v in value if isinstance(v, str)}
            unresolved.add(node.id)
        return set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(isinstance(o, ast.Name) and o.id == "tool" for o in operands):
                for o in operands:
                    if not (isinstance(o, ast.Name) and o.id == "tool"):
                        names |= strings(o)
    for attr, value in vars(module).items():
        if attr.endswith("_TOOLS") and isinstance(value, (set, frozenset)):
            names |= {v for v in value if isinstance(v, str)}
    return names, unresolved


@pytest.mark.parametrize("module", [policy, grants], ids=["policy", "grants"])
def test_every_tool_name_policy_names_is_registered(module):
    # uninstall_app, archive_files, convert_file, encrypt_file and http_request
    # outlived their tools here after the 182 -> 100 consolidation.
    names, unresolved = _referenced_tool_names(module)
    assert unresolved <= _EXTERNAL_TABLES, unresolved
    # Guards the scan itself: a refactor that hid the names would pass vacuously.
    expected = {"policy": {"run_applescript", "organize_files", "toggle_setting", "move_path", "open_app"},
                "grants": {"manage_contacts", "send_email", "toggle_setting", "web_fetch", "run_shell"}}
    assert expected[module.__name__.rsplit(".", 1)[-1]] <= names
    # MCP and skill tools register at run time under these prefixes.
    stale = sorted(n for n in names if n not in REGISTRY and not n.startswith(("mcp_", "skill_")))
    assert not stale, f"{module.__name__} names tools that are not registered: {stale}"
