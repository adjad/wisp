"""Explicit, source-pinned recovery of an interrupted helper publication.

No prior executable is trusted by its journal digest alone. Recovery can accept
only a helper independently verified against the caller's clean reviewed source.
The journal contains inventories, never credential values, and remains a barrier
until native ACL/credential acceptance and a fresh generation are durable.
"""
import json
import os
from pathlib import Path
import re
import stat

from primary_runtime import private_bytes


PHASES = {"publishing", "helper_restored_keychain_unverified",
          "restoration_unverified", "recovery_restoring", "recovery_verifying"}
FIELDS = {"schema_version", "operation", "source_commit", "slot", "prior",
          "candidate", "phase"}


def _snapshot_valid(value):
    if not isinstance(value, dict) or set(value) != {"inode", "mode", "uid", "files"}:
        return False
    if (type(value["inode"]) is not int or value["inode"] <= 0
            or type(value["uid"]) is not int or value["uid"] != os.getuid()
            or type(value["mode"]) is not int or value["mode"] not in (0o700, 0o755)
            or not isinstance(value["files"], dict)):
        return False
    for name, item in value["files"].items():
        if (not isinstance(name, str) or name in ("", ".", "..") or "/" in name
                or "\x00" in name or not isinstance(item, dict)
                or set(item) != {"mode", "uid", "sha256"}
                or type(item["uid"]) is not int or item["uid"] != os.getuid()
                or type(item["mode"]) is not int or item["mode"] < 0
                or item["mode"] & ~0o7777 or item["mode"] & 0o7022
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            return False
    return True


def _read(prep, journal):
    fd = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or not 0 < info.st_size <= 1024 * 1024):
            raise prep.Refused("unsafe_recovery_journal")
        raw = os.read(fd, info.st_size + 1)
        if len(raw) != info.st_size:
            raise prep.Refused("unsafe_recovery_journal")
        # Duplicate object fields can conceal conflicting provenance or phases.
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        record = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(record, dict) or set(record) != FIELDS
                or type(record["schema_version"]) is not int or record["schema_version"] != 2
                or record["operation"] != "helper"
                or not isinstance(record["source_commit"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", record["source_commit"])
                or not isinstance(record["slot"], str)
                or not re.fullmatch(r"\.helper-previous-[a-zA-Z0-9_-]+", record["slot"])
                or not isinstance(record["phase"], str) or record["phase"] not in PHASES
                or not _snapshot_valid(record["candidate"])
                or record["prior"] is not None and not _snapshot_valid(record["prior"])):
            raise prep.Refused("invalid_recovery_journal")
        return record
    except (ValueError, UnicodeError, TypeError):
        raise prep.Refused("invalid_recovery_journal") from None
    finally:
        os.close(fd)


def recover(prep, directory, expected_source, decision, authorize_keychain):
    """Recover only a known, unchanged helper transaction; return fixed output.

    restore-prior is deliberately unavailable when the previous helper does not
    match the reviewed source pin. Merely retaining an old receipt is not an
    authorization to execute it or an attestation of its native reader ACLs.
    """
    if authorize_keychain is not True or decision not in ("accept-current", "restore-prior"):
        raise prep.Refused("explicit_recovery_authorization_required")
    directory = Path(directory)
    parent = directory.parent
    prep.clean_source(expected_source)
    prep.private_moe(parent)
    with prep.provisioning_lock(parent):
        journal = parent / ".helper-transaction.json"
        record = _read(prep, journal)
        if record["source_commit"] != expected_source:
            raise prep.Refused("recovery_source_mismatch")
        slot = parent / record["slot"]
        current = prep.helper_snapshot(directory) if os.path.lexists(directory) else None
        saved = prep.helper_snapshot(slot) if os.path.lexists(slot) else None
        candidate, prior = record["candidate"], record["prior"]
        # The only possible inventories are before/after the atomic publication.
        published = current == candidate and saved == prior
        restored = current == prior and saved == candidate
        if not (published or restored):
            raise prep.Refused("recovery_inventory_mismatch")
        if record["phase"] == "helper_restored_keychain_unverified" and not restored:
            raise prep.Refused("recovery_phase_mismatch")
        target = current if decision == "accept-current" else prior
        if target is None:
            raise prep.Refused("reviewed_helper_required")
        target_directory = directory if target == current else slot
        prep.clean_source(expected_source)
        prep.verify_helper(target_directory, expected_source=expected_source)
        if prep.helper_snapshot(target_directory) != target or _read(prep, journal) != record:
            raise prep.Refused("recovery_inventory_changed")
        if target_directory != directory:
            record["phase"] = "recovery_restoring"
            prep.recovery_marker(journal, record)
            prep.swap_helper_directory(slot, directory)
            prep.sync_directory(parent)
        record["phase"] = "recovery_verifying"
        prep.recovery_marker(journal, record)
        # Read the current path again after restoration: ACL identities include
        # the final helper path, and no old executable is run speculatively.
        prep.clean_source(expected_source)
        if prep.helper_snapshot(directory) != target:
            raise prep.Refused("recovery_inventory_changed")
        binary = prep.verify_helper(directory, expected_source=expected_source)
        if prep.helper_snapshot(directory) != target:
            raise prep.Refused("recovery_inventory_changed")
        status = json.loads(prep.run([str(binary), "status"]))
        if status != {"credentials": "ready"}:
            raise prep.Refused("recovery_acceptance_failed")
        settings = parent.parent / ".omlx" / "settings.json"
        def read_settings():
            folder = settings.parent.lstat()
            if (not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.getuid()
                    or stat.S_IMODE(folder.st_mode) != 0o700):
                raise prep.Refused("unsafe_local_settings")
            raw = private_bytes(settings, maximum=1048576)
            after = settings.parent.lstat()
            if (after.st_ino, after.st_dev, after.st_mode, after.st_uid) != (
                    folder.st_ino, folder.st_dev, folder.st_mode, folder.st_uid):
                raise prep.Refused("local_settings_changed")
            return raw
        settings_raw = read_settings()
        config = json.loads(settings_raw)
        auth = config.get("auth") if isinstance(config, dict) else None
        token = auth.get("api_key") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
            raise prep.Refused("local_settings_agreement_required")
        agreement = json.loads(prep.run([str(binary), "local-check"],
                              data=json.dumps({"expected": token}).encode()))
        if agreement != {"local": "verified"}:
            raise prep.Refused("local_settings_agreement_required")
        prep.clean_source(expected_source)
        if (prep.helper_snapshot(directory) != target or _read(prep, journal) != record
                or read_settings() != settings_raw):
            raise prep.Refused("recovery_inventory_changed")
        prep.rotate_credential_generation(parent)
        prep.sync_directory(parent)
        if read_settings() != settings_raw:
            raise prep.Refused("local_settings_changed")
        try:
            journal.unlink()
            prep.sync_directory(parent)
        except BaseException:
            # If the final durability barrier failed, put the quarantine back.
            prep.recovery_marker(journal, record)
            raise
        return {"schema_version": 1, "status": "complete", "credentials": "ready",
                "generation": "renewed", "backend_refresh_required": True}
