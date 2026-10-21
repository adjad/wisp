"""scripts/resign_local_install.py: the local-install re-sign that keeps TCC grants.

Everything runs against a fake `codesign`/`security`/`ditto` and a tiny fake bundle in
a temp dir: no real signing, keychain, or /Applications. The contract under test:
the artifact is never signed in place; only the OUTER bundle is signed, by identity
hash, without --deep and without a network timestamp, preserving identifier,
entitlements, flags and runtime but not the ad-hoc requirement; the result is
verified (strict verify, the exact requirement for this bundle id and this identity's
leaf, the authority, every file outside the outer signature byte-identical, nested
interpreter cdhashes unchanged); a missing or ambiguous identity or a codesign refusal
leaves a verified ad-hoc copy and says plainly that grants reset; the first install
signed this way is reported as re-prompting once; and one private JSON line records
the artifact's cdhash next to the installed one.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import sys
from pathlib import Path

import pytest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import resign_local_install as rli  # noqa: E402

IDENTITY_HASH = "38248687768CD72E955305C8310CE0FDB9FDC84D"
LEAF = IDENTITY_HASH.lower()
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
GOOD_REQUIREMENT = f'identifier "{rli.BUNDLE_ID}" and certificate leaf = H"{LEAF}"'


def make_bundle(root: Path, name="Wisp.app", engine=False) -> Path:
    app = root / name
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/Wisp").write_bytes(b"\xcf\xfa\xed\xfe app")
    (app / "Contents/_CodeSignature").mkdir()
    (app / "Contents/_CodeSignature/CodeResources").write_text("adhoc seal")
    with (app / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": rli.BUNDLE_ID, "CFBundleVersion": "2203",
                       "WispSourceCommit": "f66d6ddc9f7adea9a52c47cfbbe60c3b0f8e9332"}, handle)
    py = app / "Contents/Resources/backend/.venv/bin/python3"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"\xcf\xfa\xed\xfe py")
    py.chmod(0o755)
    (app / "Contents/Resources/build-info.json").write_text("{}")
    if engine:
        bin_dir = app / "Contents/Resources/engine/py/bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python3.11").write_bytes(b"\xcf\xfa\xed\xfe engine")
        (bin_dir / "python3.11").chmod(0o755)
        (bin_dir / "python3").symlink_to("python3.11")
        (bin_dir / "pip3").write_text("#!/bin/sh\nexec python3 -m pip \"$@\"\n")
        (bin_dir / "pip3").chmod(0o755)
        (app / "Contents/Resources/engine/ENGINE.json").write_text("{}")
        (app / "Contents/Resources/engine/omlx").mkdir()
        (app / "Contents/Resources/engine/omlx/server.py").write_text("print()")
    return app


class FakeTools:
    """codesign/security/ditto as the script sees them. `signed` flips for the outer
    bundle when it is signed and is reset when the output is copied afresh; the sign
    step rewrites the fake main executable and seal, as real codesign does."""

    def __init__(self, listing=LISTING, sign_rc=0, break_nested=False, requirement=None,
                 artifact_verify_rc=0, output_verify_rc=0, authority="Wisp Dev", touch_on_sign=None,
                 security_rc=0, installed_requirement=None):
        self.calls: list[list[str]] = []
        self.installed_requirement = installed_requirement
        self.listing, self.sign_rc, self.break_nested = listing, sign_rc, break_nested
        self.requirement, self.authority, self.touch_on_sign = requirement, authority, touch_on_sign
        self.artifact_verify_rc, self.output_verify_rc, self.security_rc = artifact_verify_rc, output_verify_rc, security_rc
        self.signed: set[str] = set()
        self.artifact: str | None = None

    def __call__(self, argv, timeout=600):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tool = argv[0]
        if tool == rli.SECURITY:
            return self.security_rc, "" if self.security_rc else self.listing
        if tool == rli.DITTO:
            self.artifact = self.artifact or argv[1]
            shutil.copytree(argv[1], argv[2], symlinks=True, dirs_exist_ok=True)
            self.signed.discard(argv[2])
            return 0, ""
        assert tool == rli.CODESIGN
        path = argv[-1]
        if "--verify" in argv:
            rc = self.artifact_verify_rc if path == self.artifact or self.artifact is None else self.output_verify_rc
            return rc, "" if not rc else "invalid signature"
        if "--sign" in argv:
            if self.sign_rc:
                # Half-signed: the seal is already rewritten when codesign gives up.
                Path(path, "Contents/_CodeSignature/CodeResources").write_text("half")
                return self.sign_rc, "errSecInternalComponent"
            self.signed.add(path)
            Path(path, "Contents/MacOS/Wisp").write_bytes(b"\xcf\xfa\xed\xfe app+sig")
            Path(path, "Contents/_CodeSignature/CodeResources").write_text("wisp dev seal")
            if self.touch_on_sign:
                Path(path, self.touch_on_sign).write_bytes(b"changed by a bug")
            return 0, ""
        is_outer = path.endswith(".app") or path.endswith(".app.new")
        signed = path in self.signed
        if Path(path).is_file() and not Path(path).read_bytes().startswith(b"\xcf\xfa\xed\xfe"):
            return 1, f"{path}: code object is not signed at all"   # a plain script, as real codesign answers
        if "-r-" in argv:
            if path.endswith("Installed.app"):
                return 0, "designated => " + (self.installed_requirement or f'cdhash H"{"9" * 40}"') + "\n"
            if is_outer and signed:
                return 0, "designated => " + (self.requirement or GOOD_REQUIREMENT) + "\n"
            cd = ADHOC_CDHASH if is_outer else PY_CDHASH
            return 0, f'designated => cdhash H"{cd}"\n'
        if is_outer:
            if signed:
                return 0, (f"Identifier={rli.BUNDLE_ID}\nFormat=app bundle with Mach-O thin (arm64)\n"
                           f"CodeDirectory v=20400 size=1 flags=0x0(none) hashes=1+3 location=embedded\n"
                           f"CDHash={SIGNED_CDHASH}\nSignature size=1\nAuthority={self.authority}\nTeamIdentifier=not set\n")
            return 0, (f"Identifier={rli.BUNDLE_ID}\nCodeDirectory v=20400 size=1 flags=0x2(adhoc) hashes=1+3 location=embedded\n"
                       f"CDHash={ADHOC_CDHASH}\nSignature=adhoc\nTeamIdentifier=not set\n")
        cd = "ffff" * 10 if (self.break_nested and self.signed) else PY_CDHASH
        return 0, (f"Identifier=python3-5555\nCodeDirectory v=20400 size=1 flags=0x2(adhoc) hashes=1+2 location=embedded\n"
                   f"CDHash={cd}\nSignature=adhoc\nTeamIdentifier=not set\n")

    def sign_calls(self):
        return [c for c in self.calls if c[0] == rli.CODESIGN and "--sign" in c]


def test_identity_listing_keeps_distinct_hashes_per_name():
    assert rli.parse_identities(LISTING) == {"Wisp Dev": [IDENTITY_HASH]}
    assert rli.parse_identities("  0 valid identities found\n") == {}
    two = LISTING + f'  2) {"A" * 40} "Wisp Dev"\n  3) {"B" * 40} "Say \\"hi\\" Dev"\n'
    parsed = rli.parse_identities(two)
    assert parsed["Wisp Dev"] == [IDENTITY_HASH, "A" * 40], "two distinct certs with one name are both kept"
    assert 'Say \\"hi\\" Dev' in parsed


def test_requirement_parsing_accepts_codesigns_implicit_marker():
    explicit = f"Executable=/x/Wisp\ndesignated => {GOOD_REQUIREMENT}\n"
    implicit = f'Executable=/x/Wisp\n# designated => cdhash H"{ADHOC_CDHASH}"\n'
    assert rli.parse_requirement(explicit) == GOOD_REQUIREMENT
    assert rli.parse_requirement(implicit) == f'cdhash H"{ADHOC_CDHASH}"', "an ad-hoc artifact's requirement is implicit"
    assert rli.parse_requirement("Executable=/x\n") is None


def test_requirement_must_be_exactly_this_bundle_and_this_leaf():
    assert rli.requirement_matches(GOOD_REQUIREMENT, IDENTITY_HASH)
    assert rli.requirement_matches(" " + GOOD_REQUIREMENT + " ", IDENTITY_HASH.lower())
    bad = [f'cdhash H"{ADHOC_CDHASH}"', None, "",
           f'identifier "{rli.BUNDLE_ID}"',
           f'identifier "com.other" and certificate leaf = H"{LEAF}"',
           f'identifier "com.other" or {GOOD_REQUIREMENT}',
           f'{GOOD_REQUIREMENT} or identifier "com.other"',
           f'identifier "{rli.BUNDLE_ID}" and anchor apple generic',
           f'identifier "{rli.BUNDLE_ID}" and certificate root = H"{LEAF}"',
           f'identifier "{rli.BUNDLE_ID}" and certificate leaf = H"{"0" * 40}"',
           f'identifier "{rli.BUNDLE_ID}" and certificate leaf = H"{"0" * 64}"']
    for requirement in bad:
        assert not rli.requirement_matches(requirement, IDENTITY_HASH), requirement


def test_sign_command_touches_only_the_outer_bundle_and_keeps_the_pipelines_metadata():
    cmd = rli.sign_command(Path("/Applications/Wisp.app.new"), IDENTITY_HASH)
    assert cmd[0] == rli.CODESIGN and cmd[-1] == "/Applications/Wisp.app.new"
    assert "--deep" not in cmd, "nested engine/backend signatures must stay as the pipeline made them"
    assert "--timestamp=none" in cmd, "a self-signed identity must not reach for Apple's timestamp server"
    assert cmd[cmd.index("--sign") + 1] == IDENTITY_HASH, "signed by hash, never by name"
    meta = [c for c in cmd if c.startswith("--preserve-metadata=")][0]
    kept = set(meta.split("=", 1)[1].split(","))
    assert {"identifier", "entitlements", "flags", "runtime"} <= kept
    assert "requirements" not in kept, "the ad-hoc cdhash requirement must be replaced, not preserved"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_tree_digest_refuses_an_unreadable_directory(tmp_path):
    app = make_bundle(tmp_path)
    locked = app / "Contents/Resources/locked"
    locked.mkdir()
    (locked / "x").write_text("x")
    locked.chmod(0o000)
    try:
        with pytest.raises(rli.ResignError, match="Cannot read"):
            rli.tree_digest(app)
    finally:
        locked.chmod(0o755)


def test_tree_digest_skips_only_what_the_outer_signature_rewrites(tmp_path):
    app = make_bundle(tmp_path, engine=True)
    digest = rli.tree_digest(app)
    assert "Contents/MacOS/Wisp" not in digest and not any(k.startswith("Contents/_CodeSignature") for k in digest)
    assert digest["Contents/Resources/backend/.venv/bin/python3"][0] == "f"
    assert digest["Contents/Resources/engine/py/bin/python3"] == ("l", "python3.11")
    assert digest["Contents/Resources/engine/py/bin/pip3"][0] == "f", "helper scripts are covered by the tree"
    assert digest["Contents/Resources/engine/omlx"] == ("d",)
    (app / "Contents/Resources/engine/omlx/server.py").write_text("print(1)")
    assert rli.tree_digest(app) != digest


def test_nested_targets_cover_the_backend_and_the_real_engine_layout(tmp_path):
    app = make_bundle(tmp_path, engine=True)
    rels = sorted(p.relative_to(app).as_posix() for p in rli.nested_targets(app))
    assert rels == ["Contents/Resources/backend/.venv/bin/python3", "Contents/Resources/engine/py/bin/python3",
                    "Contents/Resources/engine/py/bin/python3.11"], "pip3 (a shell script) is not a signing target"
    # An engine-bearing build re-signs end to end: the helper script never reaches codesign -dvvv.
    record = rli.resign(app, tmp_path / "E.app.new", log=None, runner=FakeTools(), now=0)
    assert record.resigned and set(record.nested_cdhash) == set(rels)
    (app / "Contents/Resources/engine/py/bin/python3").unlink()
    (app / "Contents/Resources/engine/py/bin/python3").symlink_to("/usr/bin/python3")
    with pytest.raises(rli.ResignError, match="outside the bundle"):
        rli.nested_targets(app)


def test_happy_path_resigns_a_copy_and_records_both_cdhashes(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    log = tmp_path / "log.jsonl"
    tools = FakeTools()
    record = rli.resign(app, out, log=log, runner=tools, now=0)
    assert record.resigned and record.requirement_stable
    assert record.grants_carry_over is None, "no earlier install logged: the first one re-prompts once"
    assert record.artifact_cdhash == ADHOC_CDHASH and record.installed_cdhash == SIGNED_CDHASH
    assert record.artifact_requirement == f'cdhash H"{ADHOC_CDHASH}"'
    assert record.installed_requirement == GOOD_REQUIREMENT
    assert record.installed_signature == "Wisp Dev" and record.identity_hash == IDENTITY_HASH
    assert record.build_number == "2203" and record.commit.startswith("f66d6ddc")
    assert record.nested_cdhash == {"Contents/Resources/backend/.venv/bin/python3": {"cdhash": PY_CDHASH, "unchanged": True}}
    assert record.files_compared == len(rli.tree_digest(app)) > 0
    # One sign call, on the output only; the artifact was copied, never signed.
    assert [c[-1] for c in tools.sign_calls()] == [str(out)]
    assert (app / "Contents/MacOS/Wisp").read_bytes() == b"\xcf\xfa\xed\xfe app", "artifact untouched"
    assert (out / "Contents/MacOS/Wisp").read_bytes().endswith(b"+sig")
    # The artifact was verified strictly before anything; the copy and the result after.
    verifies = [c for c in tools.calls if "--verify" in c]
    assert verifies[0][-1] == str(app) and "--strict" in verifies[0] and "--deep" in verifies[0]
    assert tools.calls.index(verifies[0]) < tools.calls.index(tools.sign_calls()[0])
    assert [c[-1] for c in verifies].count(str(out)) == 2, "the copy is verified before signing, the result after"
    # One private JSON line, with the note that this is not a release artifact.
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["artifact_cdhash"] == ADHOC_CDHASH and entry["installed_cdhash"] == SIGNED_CDHASH
    assert entry["resigned"] is True and "not a release artifact" in entry["note"]
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    text = rli.summary(record)
    assert "re-prompts once" in text and SIGNED_CDHASH in text and ADHOC_CDHASH in text
    assert "carry over" not in text.split("re-prompts")[0]


def test_carry_over_is_read_from_the_installed_app_when_there_is_one(tmp_path):
    installed = make_bundle(tmp_path, "Installed.app")
    # The installed app is still ad-hoc: the first re-signed install re-prompts once.
    first = rli.resign(make_bundle(tmp_path, "A.app"), tmp_path / "A.app.new", log=None, installed=installed,
                       runner=FakeTools(), now=0)
    assert first.previous_requirement.startswith("cdhash") and first.grants_carry_over is False
    assert "ask once more" in rli.summary(first)
    # The installed app already carries this identity's requirement: grants carry over.
    second = rli.resign(make_bundle(tmp_path, "B.app"), tmp_path / "B.app.new", log=None, installed=installed,
                        runner=FakeTools(installed_requirement=GOOD_REQUIREMENT), now=0)
    assert second.grants_carry_over is True and "grants carry over" in rli.summary(second)
    # A different leaf on the installed app (another certificate) means a re-prompt.
    third = rli.resign(make_bundle(tmp_path, "C.app"), tmp_path / "C.app.new", log=None, installed=installed,
                       runner=FakeTools(installed_requirement=GOOD_REQUIREMENT.replace(LEAF, "1" * 40)), now=0)
    assert third.grants_carry_over is False
    # No installed app named: the log's last line stands in, and nothing known means "re-prompts once".
    missing = tmp_path / "Nowhere.app"
    fourth = rli.resign(make_bundle(tmp_path, "D.app"), tmp_path / "D.app.new", log=None, installed=missing,
                        runner=FakeTools(), now=0)
    assert fourth.grants_carry_over is None and "re-prompts once" in rli.summary(fourth)


def test_the_log_is_the_fallback_source_for_carry_over(tmp_path):
    log = tmp_path / "log.jsonl"
    first = rli.resign(make_bundle(tmp_path, "A.app"), tmp_path / "A.app.new", log=log, runner=FakeTools(), now=0)
    second = rli.resign(make_bundle(tmp_path, "B.app"), tmp_path / "B.app.new", log=log, runner=FakeTools(), now=0)
    assert first.grants_carry_over is None
    assert second.previous_requirement == GOOD_REQUIREMENT and second.grants_carry_over is True
    # A fallback install in between changes the requirement back to a cdhash.
    rli.resign(make_bundle(tmp_path, "D.app"), tmp_path / "D.app.new", log=log,
               runner=FakeTools(listing="  0 valid identities found\n"), now=0)
    fifth = rli.resign(make_bundle(tmp_path, "E.app"), tmp_path / "E.app.new", log=log, runner=FakeTools(), now=0)
    assert fifth.grants_carry_over is False and "ask once more" in rli.summary(fifth)
    log.write_text("not json\n")
    assert rli.previous_requirement(log) is None


def test_missing_identity_falls_back_to_a_verified_adhoc_copy_and_says_grants_reset(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    tools = FakeTools(listing="  0 valid identities found\n")
    record = rli.resign(app, out, log=tmp_path / "log.jsonl", runner=tools, now=0)
    assert not record.resigned and not record.requirement_stable
    assert "WILL RESET" in record.reason and "Wisp Dev" in record.reason
    assert tools.sign_calls() == []
    assert out.is_dir(), "the ad-hoc copy is still staged for the install"
    assert [c[-1] for c in tools.calls if "--verify" in c] == [str(app), str(out)], "the copy itself was verified"
    assert record.installed_cdhash == ADHOC_CDHASH, "what is installed is the artifact as built"
    assert "WILL RESET" in rli.summary(record)


def test_ambiguous_or_unreadable_identities_fall_back_with_the_reason(tmp_path):
    two = LISTING + f'  2) {"A" * 40} "Wisp Dev"\n'
    record = rli.resign(make_bundle(tmp_path, "A.app"), tmp_path / "A.app.new", log=None, runner=FakeTools(listing=two), now=0)
    assert not record.resigned and "2 valid identities" in record.reason and "refusing to guess" in record.reason
    record = rli.resign(make_bundle(tmp_path, "B.app"), tmp_path / "B.app.new", log=None, runner=FakeTools(security_rc=1), now=0)
    assert not record.resigned and "security find-identity" in record.reason and "locked" in record.reason


def test_codesign_refusal_leaves_a_fresh_verified_adhoc_copy(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    tools = FakeTools(sign_rc=1)
    record = rli.resign(app, out, log=tmp_path / "log.jsonl", runner=tools, now=0)
    assert not record.resigned
    assert "WILL RESET" in record.reason and "prompt is the user's" in record.reason
    assert (out / "Contents/_CodeSignature/CodeResources").read_text() == "adhoc seal", "no half-signed seal survives"
    dittos = [c for c in tools.calls if c[0] == rli.DITTO]
    assert len(dittos) == 2, "the half-signed copy is replaced by a fresh copy of the artifact"
    assert [c[-1] for c in tools.calls if "--verify" in c][-1] == str(out)


def test_a_changed_nested_file_fails_and_removes_the_output(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    tools = FakeTools(touch_on_sign="Contents/Resources/backend/.venv/bin/python3")
    with pytest.raises(rli.ResignError, match="files other than the outer signature changed.*python3"):
        rli.resign(app, out, log=tmp_path / "log.jsonl", runner=tools, now=0)
    assert not out.exists()
    assert not (tmp_path / "log.jsonl").exists(), "nothing was installed, nothing is logged"


def test_a_changed_nested_cdhash_fails_even_when_bytes_look_the_same(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    with pytest.raises(rli.ResignError, match="nested signature changed"):
        rli.resign(app, out, log=None, runner=FakeTools(break_nested=True), now=0)
    assert not out.exists()


@pytest.mark.parametrize("requirement", [f'cdhash H"{SIGNED_CDHASH}"',
                                         f'identifier "{rli.BUNDLE_ID}" and anchor apple generic',
                                         f'identifier "{rli.BUNDLE_ID}" and certificate leaf = H"{"0" * 40}"'])
def test_a_requirement_that_is_not_this_identity_is_refused(tmp_path, requirement):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    with pytest.raises(rli.ResignError, match="not exactly this bundle id"):
        rli.resign(app, out, log=None, runner=FakeTools(requirement=requirement), now=0)
    assert not out.exists()


def test_a_lookalike_authority_is_refused(tmp_path):
    app = make_bundle(tmp_path)
    with pytest.raises(rli.ResignError, match="authority is"):
        rli.resign(app, tmp_path / "Wisp.app.new", log=None, runner=FakeTools(authority="Wisp Dev Old"), now=0)


def test_an_unverifiable_artifact_is_never_copied_or_signed(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    tools = FakeTools(artifact_verify_rc=1)
    with pytest.raises(rli.ResignError, match="--verify failed"):
        rli.resign(app, out, log=None, runner=tools, now=0)
    assert not out.exists() and tools.sign_calls() == [] and not any(c[0] == rli.DITTO for c in tools.calls)


def test_an_unverifiable_copy_fails_before_signing(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    tools = FakeTools(output_verify_rc=1)
    with pytest.raises(rli.ResignError, match="--verify failed"):
        rli.resign(app, out, log=None, runner=tools, now=0)
    assert not out.exists() and tools.sign_calls() == []


def test_require_stages_and_logs_nothing_when_not_resigned(tmp_path):
    app = make_bundle(tmp_path)
    log = tmp_path / "log.jsonl"
    for tools in (FakeTools(listing="  0 valid identities found\n"), FakeTools(sign_rc=1)):
        out = tmp_path / f"W{id(tools)}.app.new"
        with pytest.raises(rli.NotResigned, match="Nothing staged") as info:
            rli.resign(app, out, log=log, require=True, runner=tools, now=0)
        assert "Installing" not in str(info.value) and "WILL RESET" in str(info.value)
        assert not out.exists()
    assert not log.exists(), "a rejected --require run leaves no line claiming an install"


def test_an_output_that_appears_mid_run_is_never_removed(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    real_exists = Path.exists

    def exists_then_stranger(self):
        # The pre-flight check passes; a stranger's bundle lands before the mkdir claims the path.
        if self == out and not real_exists(self):
            out.mkdir()
            (out / "theirs").write_text("x")
            return False
        return real_exists(self)
    with patch.object(Path, "exists", exists_then_stranger):
        with pytest.raises(rli.ResignError, match="appeared while this ran"):
            rli.resign(app, out, log=None, runner=FakeTools(), now=0)
    assert (out / "theirs").exists(), "a bundle this run did not create is never removed"
    shutil.rmtree(out)

    # A copier that fails after the claim leaves only this run's own directory to clean up.
    def failing_copier(src, dst):
        (Path(dst) / "partial").write_text("x")
        raise rli.ResignError("ditto failed: disk full")
    with pytest.raises(rli.ResignError, match="disk full"):
        rli.resign(app, out, log=None, runner=FakeTools(), copier=failing_copier, now=0)
    assert not out.exists(), "this run's own partial copy is removed"


def test_inputs_are_checked(tmp_path):
    app = make_bundle(tmp_path)
    out = tmp_path / "Wisp.app.new"
    out.mkdir()
    with pytest.raises(rli.ResignError, match="already exists"):
        rli.resign(app, out, log=None, runner=FakeTools(), now=0)
    with pytest.raises(rli.ResignError, match="Not an app bundle"):
        rli.resign(tmp_path / "nope.app", tmp_path / "x.app.new", log=None, runner=FakeTools(), now=0)
    with pytest.raises(rli.ResignError, match="staging bundle path"):
        rli.resign(app, tmp_path / "somewhere", log=None, runner=FakeTools(), now=0)


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    app = make_bundle(tmp_path)
    log = tmp_path / "l.jsonl"
    installed = str(tmp_path / "Nowhere.app")
    monkeypatch.setattr(rli, "run", FakeTools(listing="  0 valid identities found\n"))
    assert rli.main([str(app), "--output", str(tmp_path / "a.app.new"), "--log", str(log), "--installed", installed]) == 0
    assert "WILL RESET" in capsys.readouterr().out and (tmp_path / "a.app.new").is_dir()
    monkeypatch.setattr(rli, "run", FakeTools(sign_rc=1))
    assert rli.main([str(app), "--output", str(tmp_path / "b.app.new"), "--log", str(log), "--installed", installed, "--require"]) == 2
    assert not (tmp_path / "b.app.new").exists(), "--require stages nothing when not re-signed"
    assert "Nothing staged" in capsys.readouterr().err
    monkeypatch.setattr(rli, "run", FakeTools(artifact_verify_rc=1))
    assert rli.main([str(app), "--output", str(tmp_path / "c.app.new"), "--log", str(log), "--installed", installed]) == 1
    monkeypatch.setattr(rli, "run", FakeTools())
    assert rli.main([str(app), "--output", str(tmp_path / "d.app.new"), "--log", str(log), "--installed", installed, "--require"]) == 0
    assert "ask once more" in capsys.readouterr().out, "the log's last line (an ad-hoc fallback) stands in for the installed app"
    assert rli.main([str(app), "--output", str(tmp_path / "e.app.new"), "--log", str(tmp_path / "fresh.jsonl"), "--installed", installed]) == 0
    assert "re-prompts once" in capsys.readouterr().out
