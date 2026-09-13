"""Synthetic helper recovery adversaries; never use ambient macOS services."""
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/mac-mini"))
import credential_recovery as recovery
import node_prep as prep


SHA = "a" * 40


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    parent = tmp_path / ".moe"
    parent.mkdir(mode=0o700)
    settings_parent = tmp_path / ".omlx"
    settings_parent.mkdir(mode=0o700)
    settings = settings_parent / "settings.json"
    settings.write_text(json.dumps({"auth": {"api_key": "e" * 64}}))
    settings.chmod(0o600)
    directory = parent / "provisioning"
    slot = parent / ".helper-previous-test"
    for path, data in ((directory, "candidate"), (slot, "prior")):
        path.mkdir(mode=0o700)
        (path / "wisp-keychain-helper").write_text(data)
        (path / "wisp-keychain-helper").chmod(0o700)
        (path / "helper.json").write_text("{}")
        (path / "helper.json").chmod(0o600)
    record = {"schema_version": 2, "operation": "helper", "source_commit": SHA,
              "slot": slot.name, "prior": prep.helper_snapshot(slot),
              "candidate": prep.helper_snapshot(directory), "phase": "publishing"}
    journal = parent / ".helper-transaction.json"
    prep.recovery_marker(journal, record)
    events = []
    def clean(sha):
        assert sha == SHA
        events.append("source")
    def verify(path, *, expected_source):
        assert expected_source == SHA
        events.append(("verify", path.name))
        return path / "wisp-keychain-helper"
    def run(args, data=None):
        assert args[0] == str(directory / "wisp-keychain-helper")
        assert journal.exists()
        if args[1:] == ["local-check"]:
            assert json.loads(data) == {"expected": "e" * 64}
            assert "e" * 64 not in str(args)
            events.append("agreement")
            return b'{"local":"verified"}'
        assert args[1:] == ["status"] and data is None
        events.append("status")
        return b'{"credentials":"ready"}'
    def swap(left, right):
        temporary = parent / "swap"
        os.rename(left, temporary)
        os.rename(right, left)
        os.rename(temporary, right)
        events.append("swap")
    monkeypatch.setattr(prep, "clean_source", clean)
    monkeypatch.setattr(prep, "verify_helper", verify)
    monkeypatch.setattr(prep, "run", run)
    monkeypatch.setattr(prep, "swap_helper_directory", swap)
    return directory, slot, journal, record, events


@pytest.mark.parametrize("decision", ["accept-current", "restore-prior"])
def test_explicit_recovery_checks_source_inventory_native_acceptance_and_fresh_generation(transaction, decision):
    directory, slot, journal, record, events = transaction
    prep.rotate_credential_generation(directory.parent)
    previous = (directory.parent / ".credential-generation").read_text()
    result = recovery.recover(prep, directory, SHA, decision, True)
    assert result == {"schema_version": 1, "status": "complete", "credentials": "ready",
                      "generation": "renewed", "backend_refresh_required": True}
    assert not journal.exists()
    assert (directory.parent / ".credential-generation").read_text() != previous
    assert prep.helper_snapshot(directory) == record["candidate" if decision == "accept-current" else "prior"]
    assert ("swap" in events) == (decision == "restore-prior")
    assert events.index("status") > events.index(("verify", "provisioning"))
    assert events.index("agreement") > events.index("status")


@pytest.mark.parametrize("phase", sorted(recovery.PHASES))
def test_recovery_retry_under_known_phase(transaction, phase):
    directory, slot, journal, record, events = transaction
    if phase == "helper_restored_keychain_unverified":
        prep.swap_helper_directory(slot, directory)
    record["phase"] = phase
    prep.recovery_marker(journal, record)
    assert recovery.recover(prep, directory, SHA, "accept-current", True)["status"] == "complete"


@pytest.mark.parametrize("mutation", ["legacy", "unknown_phase", "source", "slot", "extra", "candidate", "prior"])
def test_malformed_or_unreviewed_journals_never_execute(transaction, monkeypatch, mutation):
    directory, slot, journal, record, events = transaction
    if mutation == "legacy": record["schema_version"] = 1
    elif mutation == "unknown_phase": record["phase"] = "complete"
    elif mutation == "source": record["source_commit"] = "b" * 40
    elif mutation == "slot": record["slot"] = "../outside"
    elif mutation == "extra": record["unexpected"] = "synthetic-secret-canary"
    elif mutation == "candidate": record["candidate"]["files"]["helper.json"]["sha256"] = "c" * 64
    elif mutation == "prior": record["prior"]["uid"] = -1
    prep.recovery_marker(journal, record)
    monkeypatch.setattr(prep, "verify_helper", lambda *a, **k: pytest.fail("untrusted helper executed"))
    with pytest.raises(prep.Refused):
        recovery.recover(prep, directory, SHA, "accept-current", True)
    assert journal.exists()
    assert "status" not in events


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "duplicate"])
def test_unsafe_journal_refused(transaction, kind):
    directory, slot, journal, record, events = transaction
    if kind == "symlink":
        target = journal.with_name("target")
        journal.rename(target)
        journal.symlink_to(target)
    elif kind == "hardlink": os.link(journal, journal.with_name("linked"))
    elif kind == "mode": journal.chmod(0o644)
    else:
        journal.write_text('{"schema_version":2,' + journal.read_text()[1:])
    with pytest.raises((prep.Refused, OSError)):
        recovery.recover(prep, directory, SHA, "accept-current", True)
    assert "status" not in events


@pytest.mark.parametrize("failure", ["native", "provenance", "generation", "mutated_during_verification"])
def test_acceptance_failure_keeps_quarantine(transaction, monkeypatch, failure):
    directory, slot, journal, record, events = transaction
    def refuse(*a, **k): raise prep.Refused("synthetic_failure")
    if failure == "native": monkeypatch.setattr(prep, "run", lambda *a: b'{"credentials":"missing"}')
    elif failure == "provenance": monkeypatch.setattr(prep, "verify_helper", refuse)
    elif failure == "generation": monkeypatch.setattr(prep, "rotate_credential_generation", refuse)
    else:
        def mutate(path, **kw):
            (path / "wisp-keychain-helper").write_text("substitution")
            return path / "wisp-keychain-helper"
        monkeypatch.setattr(prep, "verify_helper", mutate)
    with pytest.raises(prep.Refused):
        recovery.recover(prep, directory, SHA, "accept-current", True)
    assert journal.exists()


def test_recovery_requires_literal_authorization_before_reads(transaction, monkeypatch):
    directory, slot, journal, record, events = transaction
    for authorization in (False, None, 1, "yes"):
        with pytest.raises(prep.Refused, match="authorization"):
            recovery.recover(prep, directory, SHA, "accept-current", authorization)
    assert events == []


def test_restore_refuses_unreviewed_prior_before_swap(transaction, monkeypatch):
    directory, slot, journal, record, events = transaction
    def verify(path, **kwargs):
        if path == slot: raise prep.Refused("helper_provenance_mismatch")
        pytest.fail("unexpected helper execution")
    monkeypatch.setattr(prep, "verify_helper", verify)
    with pytest.raises(prep.Refused):
        recovery.recover(prep, directory, SHA, "restore-prior", True)
    assert prep.helper_snapshot(directory) == record["candidate"]
    assert journal.exists() and "swap" not in events


def test_failure_after_swap_can_retry_without_swapping_back(transaction, monkeypatch):
    directory, slot, journal, record, events = transaction
    original = prep.run
    monkeypatch.setattr(prep, "run", lambda *a: b'{"credentials":"missing"}')
    with pytest.raises(prep.Refused):
        recovery.recover(prep, directory, SHA, "restore-prior", True)
    assert prep.helper_snapshot(directory) == record["prior"]
    assert json.loads(journal.read_text())["phase"] == "recovery_verifying"
    monkeypatch.setattr(prep, "run", original)
    recovery.recover(prep, directory, SHA, "restore-prior", True)
    assert events.count("swap") == 1


def test_first_install_acceptance_has_no_prior_slot(transaction):
    directory, slot, journal, record, events = transaction
    for item in slot.iterdir(): item.unlink()
    slot.rmdir()
    record["prior"] = None
    prep.recovery_marker(journal, record)
    assert recovery.recover(prep, directory, SHA, "accept-current", True)["status"] == "complete"


def test_missing_prior_is_not_a_restoration(transaction):
    directory, slot, journal, record, events = transaction
    for item in slot.iterdir(): item.unlink()
    slot.rmdir()
    record["prior"] = None
    prep.recovery_marker(journal, record)
    with pytest.raises(prep.Refused, match="reviewed_helper_required"):
        recovery.recover(prep, directory, SHA, "restore-prior", True)
    assert journal.exists() and "status" not in events


def test_failed_final_durability_barrier_reinstates_quarantine(transaction, monkeypatch):
    directory, slot, journal, record, events = transaction
    original = prep.sync_directory
    failed = False
    def sync(path):
        nonlocal failed
        if not journal.exists() and not failed:
            failed = True
            raise OSError("synthetic storage failure")
        original(path)
    monkeypatch.setattr(prep, "sync_directory", sync)
    with pytest.raises(OSError):
        recovery.recover(prep, directory, SHA, "accept-current", True)
    assert failed and journal.exists()
    assert json.loads(journal.read_text())["phase"] == "recovery_verifying"


@pytest.mark.parametrize("kind", ["missing", "invalid_token", "invalid_document", "parent_mode",
                                 "file_mode", "file_symlink", "parent_symlink"])
def test_unsafe_or_invalid_settings_keep_helper_quarantine(transaction, kind):
    directory, slot, journal, record, events = transaction
    folder = directory.parent.parent / ".omlx"
    settings = folder / "settings.json"
    if kind == "missing": settings.unlink()
    elif kind == "invalid_token": settings.write_text('{"auth":{"api_key":"legacy"}}')
    elif kind == "invalid_document": settings.write_text('[]')
    elif kind == "parent_mode": folder.chmod(0o755)
    elif kind == "file_mode": settings.chmod(0o644)
    elif kind == "file_symlink":
        target = folder / "target"
        settings.rename(target)
        settings.symlink_to(target)
    elif kind == "parent_symlink":
        target = folder.with_name("settings-target")
        folder.rename(target)
        folder.symlink_to(target, target_is_directory=True)
    with pytest.raises((prep.Refused, OSError, ValueError)):
        recovery.recover(prep, directory, SHA, "accept-current", True)
    assert journal.exists() and "agreement" not in events


@pytest.mark.parametrize("kind", ["mismatch", "wrong_reply", "changed_after_check", "changed_after_generation"])
def test_settings_agreement_must_hold_until_journal_clear(transaction, monkeypatch, kind):
    directory, slot, journal, record, events = transaction
    settings = directory.parent.parent / ".omlx/settings.json"
    original_run = prep.run
    def run(args, data=None):
        result = original_run(args, data=data)
        if args[-1] == "local-check":
            if kind == "mismatch": raise prep.Refused("synthetic_key_mismatch")
            if kind == "wrong_reply": return b'{"local":"present"}'
            if kind == "changed_after_check": settings.write_text('{"auth":{"api_key":"' + "f" * 64 + '"}}')
        return result
    monkeypatch.setattr(prep, "run", run)
    if kind == "changed_after_generation":
        original_rotate = prep.rotate_credential_generation
        def rotate(parent):
            original_rotate(parent)
            settings.write_text('{"auth":{"api_key":"' + "f" * 64 + '"}}')
        monkeypatch.setattr(prep, "rotate_credential_generation", rotate)
    with pytest.raises(prep.Refused):
        recovery.recover(prep, directory, SHA, "accept-current", True)
    assert journal.exists() and "agreement" in events
