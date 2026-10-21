"""scripts/resign_local_install.py: the local-install re-sign that keeps TCC grants.

Everything runs against a fake `codesign`/`security`/`ditto` and a tiny fake bundle in
a temp dir: no real signing, keychain, or /Applications. The contract under test:
the artifact is never signed in place; only the OUTER bundle is signed, by identity
hash, without --deep and without a network timestamp, preserving identifier,
entitlements, flags and runtime but not the ad-hoc requirement; the result is
verified (strict verify, certificate-bound designated requirement, Wisp Dev
authority, nested signatures byte-for-byte unchanged); a missing identity or a
codesign refusal leaves a clean ad-hoc copy and says plainly that grants reset; and
one JSON line records the artifact's cdhash next to the installed one.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import resign_local_install as rli  # noqa: E402

IDENTITY_HASH = "38248687768CD72E955305C8310CE0FDB9FDC84D"
LISTING = f'''Policy: Code Signing
  Matching identities
  1) {IDENTITY_HASH} "Wisp Dev"
     1 identities found

  Valid identities only
  1) {IDENTITY_HASH} "Wisp Dev"
     1 valid identities found
'''
ADHOC_CDHASH = "7c3224922ac1aa955d0dfc2a784a706793b27d63"
SIGNED_CDHASH = "aaaa0000bbbb1111cccc2222dddd3333eeee4444"
PY_CDHASH = "1234567890abcdef1234567890abcdef12345678"
LEAF = "H\"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\""


def make_bundle(root: Path, name="Wisp.app") -> Path:
    app = root / name
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/Wisp").write_bytes(b"\xcf\xfa\xed\xfe app")
    with (app / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": rli.BUNDLE_ID, "CFBundleVersion": "2203",
                       "WispSourceCommit": "f66d6ddc9f7adea9a52c47cfbbe60c3b0f8e9332"}, handle)
    py = app / "Contents/Resources/backend/.venv/bin/python3"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"\xcf\xfa\xed\xfe py")
    py.chmod(0o755)
    return app


class FakeTools:
    """codesign/security/ditto as the script sees them. `signed` flips when the
    outer bundle is signed; nested code never changes unless `break_nested`."""

    def __init__(self, listing=LISTING, sign_rc=0, break_nested=False, unstable_requirement=False,
                 verify_rc=0):
        self.calls: list[list[str]] = []
        self.listing = listing
        self.sign_rc = sign_rc
        self.break_nested = break_nested
        self.unstable_requirement = unstable_requirement
        self.verify_rc = verify_rc
        self.signed: set[str] = set()

    def __call__(self, argv, timeout=600):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tool = argv[0]
        if tool == rli.SECURITY:
            return 0, self.listing
        if tool == rli.DITTO:
            shutil.copytree(argv[1], argv[2], symlinks=True)
            return 0, ""
        assert tool == rli.CODESIGN
        path = argv[-1]
        if "--verify" in argv:
            return self.verify_rc, "" if not self.verify_rc else "invalid signature"
        if "--sign" in argv:
            if self.sign_rc:
                return self.sign_rc, "errSecInternalComponent"
            self.signed.add(path)
            return 0, ""
        is_outer = path.endswith(".app") or path.endswith(".app.new")
        signed = path in self.signed
        if "-r-" in argv:
            if is_outer and signed and not self.unstable_requirement:
                return 0, f'designated => identifier "{rli.BUNDLE_ID}" and certificate leaf = {LEAF}\n'
            cd = SIGNED_CDHASH if (is_outer and signed) else (ADHOC_CDHASH if is_outer else PY_CDHASH)
            return 0, f'designated => cdhash H"{cd}"\n'
        # -dvvv
        if is_outer:
            if signed:
                return 0, (f"Identifier={rli.BUNDLE_ID}\nFormat=app bundle with Mach-O thin (arm64)\n"
                           f"CodeDirectory v=20400 size=1 flags=0x0(none) hashes=1+3 location=embedded\n"
                           f"CDHash={SIGNED_CDHASH}\nSignature size=1\nAuthority=Wisp Dev\nTeamIdentifier=not set\n")
            return 0, (f"Identifier={rli.BUNDLE_ID}\nCodeDirectory v=20400 size=1 flags=0x2(adhoc) hashes=1+3 location=embedded\n"
                       f"CDHash={ADHOC_CDHASH}\nSignature=adhoc\nTeamIdentifier=not set\n")
        cd = "ffff" * 10 if (self.break_nested and self.signed) else PY_CDHASH
        return 0, (f"Identifier=python3-5555\nCodeDirectory v=20400 size=1 flags=0x2(adhoc) hashes=1+2 location=embedded\n"
                   f"CDHash={cd}\nSignature=adhoc\nTeamIdentifier=not set\n")

    def sign_calls(self):
        return [c for c in self.calls if c[0] == rli.CODESIGN and "--sign" in c]


def test_identity_listing_is_parsed_by_name_and_hash():
    assert rli.parse_identities(LISTING) == {"Wisp Dev": IDENTITY_HASH}
    assert rli.parse_identities("  0 valid identities found\n") == {}
    two = LISTING + f'  2) {"A" * 40} "Wisp Dev"\n'
    assert rli.parse_identities(two)["Wisp Dev"] == IDENTITY_HASH, "the first (keychain order) wins"


def test_requirement_stability_rule():
    assert not rli.requirement_is_stable(f'cdhash H"{ADHOC_CDHASH}"')
    assert rli.requirement_is_stable(f'identifier "{rli.BUNDLE_ID}" and certificate leaf = {LEAF}')
    assert not rli.requirement_is_stable(f'identifier "com.other" and certificate leaf = {LEAF}')
    assert not rli.requirement_is_stable(None)
    assert not rli.requirement_is_stable(f'identifier "{rli.BUNDLE_ID}"'), "a bare identifier is not certificate-bound"


def test_sign_command_touches_only_the_outer_bundle_and_keeps_the_pipelines_metadata():
    cmd = rli.sign_command(Path("/Applications/Wisp.app.new"), IDENTITY_HASH)
    assert cmd[0] == rli.CODESIGN and cmd[-1] == "/Applications/Wisp.app.new"
    assert "--deep" not in cmd, "nested engine/backend signatures must stay as the pipeline made them"
    assert "--timestamp=none" in cmd, "a self-signed identity must not reach for Apple's timestamp server"
    assert ["--sign", IDENTITY_HASH] == cmd[cmd.index("--sign"):cmd.index("--sign") + 2], "signed by hash, never by name"
    meta = [c for c in cmd if c.startswith("--preserve-metadata=")][0]
    kept = set(meta.split("=", 1)[1].split(","))
    assert {"identifier", "entitlements", "flags", "runtime"} <= kept
    assert "requirements" not in kept, "the ad-hoc cdhash requirement must be replaced, not preserved"


def test_happy_path_resigns_a_copy_and_records_both_cdhashes(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    log = tmp_path / "log.jsonl"
    tools = FakeTools()
    record = rli.resign(app, out, log=log, runner=tools, now=0)
    assert record.resigned and record.grants_survive
    assert record.artifact_cdhash == ADHOC_CDHASH and record.installed_cdhash == SIGNED_CDHASH
    assert record.artifact_requirement == f'cdhash H"{ADHOC_CDHASH}"'
    assert record.installed_requirement.startswith(f'identifier "{rli.BUNDLE_ID}" and certificate leaf')
    assert record.installed_signature == "Wisp Dev" and record.identity_hash == IDENTITY_HASH
    assert record.build_number == "2203" and record.commit.startswith("f66d6ddc")
    assert record.nested_unchanged == {"Contents/Resources/backend/.venv/bin/python3": True}
    # One sign call, on the output only; the artifact was copied, never signed.
    assert [c[-1] for c in tools.sign_calls()] == [str(out)]
    assert all(c[-1] != str(app) for c in tools.sign_calls())
    assert out.is_dir() and (out / "Contents/MacOS/Wisp").read_bytes() == (app / "Contents/MacOS/Wisp").read_bytes()
    # The artifact was verified strictly before anything, and the output after.
    verifies = [c for c in tools.calls if "--verify" in c]
    assert verifies[0][-1] == str(app) and "--strict" in verifies[0] and "--deep" in verifies[0]
    assert verifies[-1][-1] == str(out)
    assert tools.calls.index(verifies[0]) < tools.calls.index(tools.sign_calls()[0])
    # One JSON line, private, with the note that this is not a release artifact.
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["artifact_cdhash"] == ADHOC_CDHASH and entry["installed_cdhash"] == SIGNED_CDHASH
    assert entry["resigned"] is True and "not a release artifact" in entry["note"]
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    text = rli.summary(record)
    assert "grants survive" in text and SIGNED_CDHASH in text and ADHOC_CDHASH in text


def test_missing_identity_falls_back_to_the_adhoc_copy_and_says_grants_reset(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    tools = FakeTools(listing="  0 valid identities found\n")
    record = rli.resign(app, out, log=tmp_path / "log.jsonl", runner=tools, now=0)
    assert not record.resigned and not record.grants_survive
    assert "WILL RESET" in record.reason and "Wisp Dev" in record.reason
    assert tools.sign_calls() == []
    assert out.is_dir(), "the ad-hoc copy is still staged for the install"
    assert record.installed_cdhash == ADHOC_CDHASH, "what is installed is the artifact as built"
    assert "NOT re-signed" in rli.summary(record)


def test_codesign_refusal_leaves_a_clean_adhoc_copy(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    tools = FakeTools(sign_rc=1)
    record = rli.resign(app, out, log=tmp_path / "log.jsonl", runner=tools, now=0)
    assert not record.resigned
    assert "WILL RESET" in record.reason and "keychain prompt" in record.reason
    assert out.is_dir() and (out / "Contents/Info.plist").is_file()
    dittos = [c for c in tools.calls if c[0] == rli.DITTO]
    assert len(dittos) == 2, "the half-signed copy is replaced by a fresh copy of the artifact"


def test_a_changed_nested_signature_fails_and_removes_the_output(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    tools = FakeTools(break_nested=True)
    with pytest.raises(rli.ResignError, match="nested signature changed"):
        rli.resign(app, out, log=tmp_path / "log.jsonl", runner=tools, now=0)
    assert not out.exists()
    assert not (tmp_path / "log.jsonl").exists(), "nothing was installed, nothing is logged"


def test_a_cdhash_bound_requirement_after_signing_is_refused(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    with pytest.raises(rli.ResignError, match="not certificate-bound"):
        rli.resign(app, out, log=tmp_path / "log.jsonl", runner=FakeTools(unstable_requirement=True), now=0)
    assert not out.exists()


def test_an_unverifiable_artifact_is_never_copied_or_signed(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    tools = FakeTools(verify_rc=1)
    with pytest.raises(rli.ResignError, match="--verify failed"):
        rli.resign(app, out, log=tmp_path / "log.jsonl", runner=tools, now=0)
    assert not out.exists() and tools.sign_calls() == []


def test_inputs_are_checked(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    out.mkdir()
    with pytest.raises(rli.ResignError, match="already exists"):
        rli.resign(app, out, log=tmp_path / "log.jsonl", runner=FakeTools(), now=0)
    with pytest.raises(rli.ResignError, match="Not an app bundle"):
        rli.resign(tmp_path / "nope.app", tmp_path / "x.app.new", log=tmp_path / "log.jsonl", runner=FakeTools(), now=0)
    with pytest.raises(rli.ResignError, match="staging bundle path"):
        rli.resign(app, tmp_path / "somewhere", log=tmp_path / "log.jsonl", runner=FakeTools(), now=0)


def test_engine_children_are_compared_when_an_engine_is_shipped(tmp_path):
    app = make_bundle(tmp_path)
    engine = app / "Contents/Resources/engine"
    (engine / "oMLX.app").mkdir(parents=True)
    exe = engine / "omlx-server"
    exe.write_bytes(b"\xcf\xfa\xed\xfe e")
    exe.chmod(0o755)
    (engine / "notes.txt").write_text("not code")
    rels = sorted(str(p.relative_to(app)) for p in rli.nested_targets(app))
    assert rels == ["Contents/Resources/backend/.venv/bin/python3", "Contents/Resources/engine/oMLX.app",
                    "Contents/Resources/engine/omlx-server"]


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    app = make_bundle(tmp_path)
    monkeypatch.setattr(rli, "run", FakeTools(listing="  0 valid identities found\n"))
    assert rli.main([str(app), "--output", str(tmp_path / "a.app.new"), "--log", str(tmp_path / "l.jsonl")]) == 0
    assert "WILL RESET" in capsys.readouterr().out
    assert rli.main([str(app), "--output", str(tmp_path / "b.app.new"), "--log", str(tmp_path / "l.jsonl"), "--require"]) == 2
    monkeypatch.setattr(rli, "run", FakeTools(verify_rc=1))
    assert rli.main([str(app), "--output", str(tmp_path / "c.app.new"), "--log", str(tmp_path / "l.jsonl")]) == 1
