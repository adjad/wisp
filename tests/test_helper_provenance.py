"""Immutable helper inputs and exact-head CI; all external actions are synthetic."""
import hashlib
import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/mac-mini"))
import node_prep as prep

SHA = "a" * 40
INPUTS = {prep.HELPER_INPUTS[0]: b"// reviewed credentials\n",
          prep.HELPER_INPUTS[1]: b"// reviewed helper\n",
          prep.HELPER_INPUTS[2]: b'{"ci_swift":"6.1.2"}'}


@pytest.fixture
def adapter(monkeypatch):
    state = {"calls": [], "dirty": b"", "head": SHA, "checks": 0}
    def run(argv, **kwargs):
        state["calls"].append(argv)
        if argv[:3] == ["/usr/bin/git", "-C", str(ROOT)]:
            if argv[3:] == ["rev-parse", "HEAD"]:
                state["checks"] += 1
                if state.get("on_check"):
                    state["on_check"](state)
                return state["head"].encode()
            if argv[3:] == ["status", "--porcelain", "--untracked-files=all"]:
                return state["dirty"]
            if argv[3] == "show":
                source, name = argv[4].split(":", 1)
                assert source == SHA
                return INPUTS[name]
        if argv == ["/usr/bin/swiftc", "--version"]:
            return state.get("compiler", b"Apple Swift version 6.1.2")
        if argv[0] == "/usr/bin/swiftc" and "-parse-as-library" in argv:
            paths = list(map(Path, argv[argv.index("-module-cache-path") + 2:-2]))
            assert len(paths) == 2
            for name, path in zip(prep.HELPER_INPUTS, paths):
                assert not path.is_relative_to(ROOT)
                assert path.read_bytes() == INPUTS[name]
            state["binary"] = Path(argv[-1])
            state["binary"].write_bytes(b"signed synthetic binary")
            if state.get("compiled"):
                state["compiled"](paths, state)
            return b""
        if argv[0] == "/usr/bin/codesign":
            return b""
        if argv[-1] == "protocol-version":
            if state.get("during_verify"):
                state["during_verify"](Path(argv[0]))
            return b"wisp-mini-helper-v2\n"
        pytest.fail("unexpected external action: " + str(argv))
    monkeypatch.setattr(prep, "run", run)
    return state


@pytest.mark.parametrize("failure", ["missing", "short", "head", "tracked", "untracked"])
def test_init_pin_precedes_all_initialization_mutations(tmp_path, monkeypatch, adapter, failure):
    monkeypatch.setattr(prep.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(prep, "private_moe", lambda *a, **k: pytest.fail("premature migration"))
    monkeypatch.setattr(prep, "read_json", lambda *a: pytest.fail("premature settings read"))
    if failure == "head": adapter["head"] = "b" * 40
    if failure in {"tracked", "untracked"}: adapter["dirty"] = b" M tracked" if failure == "tracked" else b"?? injected"
    pin = None if failure == "missing" else "abc" if failure == "short" else SHA
    with pytest.raises(prep.Refused):
        prep.keychain("init", expected_source=pin)
    assert not list(tmp_path.iterdir())


def test_applied_init_cli_forwards_reviewed_sha(monkeypatch):
    seen = []
    monkeypatch.setattr(prep, "keychain", lambda command, **kwargs: seen.append((command, kwargs)) or b'{"credentials":"ready"}')
    assert prep.main(["init-primary", "--live", "--apply", "--source-sha", SHA]) == 0
    assert seen == [("init", {"expected_source": SHA})]


def test_helper_receipt_uses_immutable_compile_snapshot(tmp_path, adapter):
    directory = tmp_path / "provisioning"
    prep.prepare_helper(directory, expected_source=SHA)
    record = json.loads((directory / "helper.json").read_text())
    assert record["schema_version"] == 3 and record["source_commit"] == SHA
    assert record["sources"] == {name: hashlib.sha256(data).hexdigest() for name, data in INPUTS.items()}
    assert prep.verify_helper(directory, expected_source=SHA) == directory / "wisp-keychain-helper"
    assert not list(tmp_path.glob(".helper-inputs-*"))
    generation = tmp_path / ".credential-generation"
    assert re.fullmatch(r"[0-9a-f]{64}", generation.read_text())
    assert generation.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["compile_input", "checkout", "staged_binary", "published_binary", "verify_binary",
                                     "publication_head", "acceptance_head"])
def test_mutation_never_reaches_acceptance(tmp_path, adapter, failure):
    directory = tmp_path / "provisioning"
    if failure == "compile_input":
        adapter["compiled"] = lambda paths, state: paths[0].write_bytes(b"changed after compiler")
    elif failure == "checkout":
        adapter["compiled"] = lambda paths, state: state.update(dirty=b" M source")
    elif failure == "verify_binary":
        adapter["during_verify"] = lambda binary: binary.write_bytes(b"changed during verification")
    elif failure in {"publication_head", "acceptance_head"}:
        def change_head(state):
            if state["checks"] == (4 if failure == "publication_head" else 5):
                state["head"] = "b" * 40
        adapter["on_check"] = change_head
    else:
        def mutate(state):
            # wrapper + transaction + post-compile + prepublication + preacceptance
            if state["checks"] == (4 if failure == "staged_binary" else 5):
                target = state["binary"] if failure == "staged_binary" else directory / "wisp-keychain-helper"
                target.write_bytes(b"changed candidate")
        adapter["on_check"] = mutate
    with pytest.raises(prep.Refused):
        prep.prepare_helper(directory, expected_source=SHA, accept=lambda binary: pytest.fail("unverified acceptance"))
    assert not directory.exists()
    assert not list(tmp_path.glob(".helper-inputs-*"))


def test_compiler_version_is_exact_not_prefix(tmp_path, adapter):
    adapter["compiler"] = b"Apple Swift version 6.1.20"
    with pytest.raises(prep.Refused, match="unqualified_helper_compiler"):
        prep.prepare_helper(tmp_path / "provisioning", expected_source=SHA)
    assert "binary" not in adapter


def test_public_helper_transaction_holds_exclusive_lock(tmp_path, monkeypatch, adapter):
    from contextlib import contextmanager
    held = []
    @contextmanager
    def lock(parent):
        assert parent == tmp_path
        held.append(True)
        try:
            yield
        finally:
            held.pop()
    monkeypatch.setattr(prep, "provisioning_lock", lock)
    original_rotate = prep.rotate_credential_generation
    def rotate(parent):
        assert held
        original_rotate(parent)
    monkeypatch.setattr(prep, "rotate_credential_generation", rotate)
    prep.prepare_helper(tmp_path / "provisioning", expected_source=SHA,
                        accept=lambda binary: held or pytest.fail("unlocked acceptance"))
    assert not held


@pytest.mark.parametrize("mutation", ["legacy", "source", "input", "binary"])
def test_receipt_tampering_blocks_reuse(tmp_path, adapter, mutation):
    directory = tmp_path / "provisioning"
    prep.prepare_helper(directory, expected_source=SHA)
    path = directory / "helper.json"
    record = json.loads(path.read_text())
    if mutation == "legacy": record["schema_version"] = 2
    if mutation == "source": record["source_commit"] = "b" * 40
    if mutation == "input": record["sources"][prep.HELPER_INPUTS[0]] = "b" * 64
    if mutation == "binary": record["sha256"] = "b" * 64
    path.write_text(json.dumps(record))
    with pytest.raises(prep.Refused):
        prep.verify_helper(directory, expected_source=SHA)


def test_failed_acceptance_keeps_rotated_generation(tmp_path, adapter):
    path = tmp_path / ".credential-generation"
    path.write_text("f" * 64)
    before = path.read_bytes()
    def refuse(binary):
        assert path.read_bytes() != before
        assert (tmp_path / ".helper-transaction.json").is_file()
        raise prep.Refused("synthetic failed acceptance")
    with pytest.raises(prep.Refused, match="helper_recovery_required"):
        prep.prepare_helper(tmp_path / "provisioning", expected_source=SHA, accept=refuse)
    assert path.read_bytes() != before


def test_regression_workflow_asserts_exact_head():
    workflow = (ROOT / ".github/workflows/regression-gate.yml").read_text()
    pin = "${{ github.event.pull_request.head.sha || github.sha }}"
    assert "ref: " + pin in workflow
    assert "CANDIDATE_SHA: " + pin in workflow
    assert 'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"' in workflow


def test_git_reads_ignore_ambient_redirection_and_replace_refs(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("GIT_DIR", "/synthetic/other-repository")
    monkeypatch.setenv("GIT_WORK_TREE", "/synthetic/other-tree")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "99")
    captured = {}
    def execute(argv, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=b"synthetic", stderr=b"")
    monkeypatch.setattr(prep.subprocess, "run", execute)
    assert prep.run(["/usr/bin/git", "-C", str(ROOT), "show", SHA + ":" + prep.HELPER_INPUTS[0]]) == b"synthetic"
    assert "GIT_DIR" not in captured and "GIT_WORK_TREE" not in captured
    assert captured["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert captured["GIT_CONFIG_COUNT"] == "1"
    assert captured["GIT_CONFIG_KEY_0"] == "core.fsmonitor" and captured["GIT_CONFIG_VALUE_0"] == "false"
