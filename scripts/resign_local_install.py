#!/usr/bin/env python3
"""Re-sign a verified Wisp.app for a LOCAL install with the "Wisp Dev" identity.

Why: every pipeline artifact is ad-hoc signed, so its designated requirement is
`cdhash H"…"` and macOS treats each build as a new app. The Calendar, Reminders
and Full Disk Access grants the user gave the previous build die on every
install. A stable self-signed code-signing identity ("Wisp Dev", in the login
keychain) gives every build the same designated requirement
(`identifier "com.wisp.assistant" and certificate leaf = H"<identity sha1>"`),
so grants given to one install signed this way carry over to the next. The
first install signed this way still differs from the ad-hoc one it replaces,
so macOS asks once more; the install log says which case applies.

What this does, and only this:
  * copies the verified artifact to --output (never signs the artifact in place),
  * re-signs the OUTER bundle only (no --deep): the nested backend interpreter
    and the bundled engine keep the signatures the pipeline gave them, which the
    attribution checks (service/inference/bundled_peer.py, attributed_transport.py)
    read from the running processes,
  * preserves identifier, entitlements, flags and runtime; lets codesign derive
    the new designated requirement from the certificate,
  * verifies the result: `codesign --verify --deep --strict`, the exact
    requirement (identifier and this identity's leaf hash), the authority, and
    that every file except the main executable and the outer _CodeSignature is
    byte-identical to the artifact (so no nested signature changed),
  * appends one JSON line to the local install log with the artifact's cdhash
    next to the installed one.

When the identity is missing, ambiguous, or codesign refuses, the output is the
verified, untouched ad-hoc copy and this says so plainly: the grants will reset
again. A codesign that hangs (an unanswered keychain prompt) is a hard failure
after the timeout, with nothing left at --output.

Trade-off, stated once: with a certificate-bound requirement, anything signed
with this key and identifier would match the grants. The key lives in the user's
login keychain; keep its access control prompting ("Allow", not "Always Allow")
so no same-user program can sign with it silently.

This is a local-install transformation. It is NOT part of the CI artifact
pipeline, release.py, or the provenance rule that a verified artifact is ad-hoc
or Developer ID signed; those are unchanged and the artifact itself is never
modified.

Usage:
  scripts/resign_local_install.py dist/preview/Wisp.app --output /Applications/Wisp.app.new --require
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_IDENTITY = "Wisp Dev"
BUNDLE_ID = "com.wisp.assistant"
DEFAULT_LOG = Path.home() / ".moe" / "local-installs.jsonl"
CODESIGN = "/usr/bin/codesign"
SECURITY = "/usr/bin/security"
DITTO = "/usr/bin/ditto"
# The only parts of the bundle an outer re-sign may change.
RESIGNED_PATHS = ("Contents/MacOS/Wisp", "Contents/_CodeSignature")
# Nested code whose own signatures are also reported by cdhash: the backend
# interpreter the pipeline signs, and the bundled engine's interpreter
# (build-support/engine.py ENGINE_SIGNING_ID), absent from preview builds.
NESTED = ("Contents/Resources/backend/.venv/bin/python3", "Contents/Resources/engine/py/bin")
REQUIREMENT = re.compile(r'^identifier "' + re.escape(BUNDLE_ID) + r'" and certificate leaf = H"([0-9a-f]{40})"$')


def clean_env() -> dict:
    return {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "LANG": "C"}


def run(argv, timeout=600):
    """Run one tool; stdout+stderr as text, plus the return code."""
    result = subprocess.run([str(a) for a in argv], env=clean_env(), capture_output=True,
                            text=True, timeout=timeout)
    return result.returncode, result.stdout + result.stderr


# ---------------------------------------------------------------- pure parsing

def parse_identities(listing: str) -> dict[str, list[str]]:
    """`security find-identity -v -p codesigning` → {common name: [SHA-1 hashes]},
    distinct hashes only, in keychain order. The listing prints identities twice
    (matching, then valid); each hash is kept once."""
    found: dict[str, list[str]] = {}
    for line in listing.splitlines():
        match = re.match(r'\s*\d+\)\s+([0-9A-F]{40})\s+"(.+)"\s*$', line)
        if match:
            hashes = found.setdefault(match.group(2), [])
            if match.group(1) not in hashes:
                hashes.append(match.group(1))
    return found


def parse_display(text: str) -> dict:
    """The fields of `codesign -dvvv` this tool records."""
    out = {"identifier": None, "cdhash": None, "signature": None, "authority": [], "team": None, "flags": None}
    for line in text.splitlines():
        if line.startswith("Identifier="):
            out["identifier"] = line.split("=", 1)[1]
        elif line.startswith("CDHash="):
            out["cdhash"] = line.split("=", 1)[1]
        elif line.startswith("Signature="):
            out["signature"] = line.split("=", 1)[1]
        elif line.startswith("Authority="):
            out["authority"].append(line.split("=", 1)[1])
        elif line.startswith("TeamIdentifier="):
            out["team"] = line.split("=", 1)[1]
        elif line.startswith("CodeDirectory"):
            match = re.search(r"flags=(0x[0-9a-f]+)\(([^)]*)\)", line)
            out["flags"] = (match.group(1) + "(" + match.group(2) + ")") if match else line
    return out


def parse_requirement(text: str) -> str | None:
    """The designated requirement line of `codesign -d -r-`, without its prefix. codesign
    prints `# designated => …` when the requirement is implicit (an ad-hoc signature's
    cdhash) and `designated => …` when it is explicit; both are the requirement."""
    for line in text.splitlines():
        stripped = line.lstrip("# ").strip()
        if stripped.startswith("designated =>"):
            return stripped[len("designated =>"):].strip()
    return None


def requirement_matches(requirement: str | None, identity_hash: str) -> bool:
    """The one requirement grants survive under: exactly this bundle identifier and
    exactly this identity's certificate leaf. Anything else (a cdhash, an anchor,
    an `or`, another identifier, another certificate) is refused."""
    if not requirement:
        return False
    match = REQUIREMENT.match(requirement.strip())
    return bool(match) and match.group(1) == identity_hash.lower()


def sign_command(bundle: Path, identity_hash: str) -> list[str]:
    """The one codesign invocation: outer bundle only, by hash (never by name, so
    two identities called the same can't be confused), no network timestamp for a
    self-signed cert, keep what the pipeline set except the ad-hoc requirement."""
    return [CODESIGN, "--force", "--sign", identity_hash, "--timestamp=none",
            "--preserve-metadata=identifier,entitlements,flags,runtime", str(bundle)]


class ResignError(Exception):
    pass


class NotResigned(ResignError):
    """--require: the bundle could not be re-signed; nothing was staged or logged."""


def _unreadable(error: OSError) -> None:
    raise ResignError(f"Cannot read {error.filename}: {error.strerror}")


def tree_digest(bundle: Path, skip=RESIGNED_PATHS) -> dict[str, tuple]:
    """Every entry under the bundle except `skip`: regular files by size and
    SHA-256, symlinks by target, directories by presence. Equal digests before and
    after the outer re-sign prove no nested file (so no nested signature) changed.
    An unreadable directory or file is an error, never a silently skipped subtree."""
    out: dict[str, tuple] = {}
    for root, dirs, files in os.walk(bundle, onerror=_unreadable):
        for name in list(dirs) + files:
            path = Path(root) / name
            rel = path.relative_to(bundle).as_posix()
            if any(rel == s or rel.startswith(s + "/") for s in skip):
                if name in dirs:
                    dirs.remove(name)
                continue
            if path.is_symlink():
                out[rel] = ("l", os.readlink(path))
            elif path.is_dir():
                out[rel] = ("d",)
            elif path.is_file():
                digest = hashlib.sha256()
                try:
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1 << 20), b""):
                            digest.update(chunk)
                except OSError as exc:
                    raise ResignError(f"Cannot read {path}: {exc.strerror}") from None
                out[rel] = ("f", path.stat().st_size, digest.hexdigest())
            else:
                raise ResignError(f"Unexpected special file in the bundle: {path}")
    return out


MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


@dataclass
class Record:
    """One line of the local install log."""
    at: str
    artifact: str
    output: str
    build_number: str | None
    commit: str | None
    artifact_cdhash: str | None
    artifact_signature: str | None
    artifact_requirement: str | None
    resigned: bool
    identity: str | None
    identity_hash: str | None
    installed_cdhash: str | None
    installed_signature: str | None
    installed_requirement: str | None
    requirement_stable: bool = False
    previous_requirement: str | None = None
    grants_carry_over: bool | None = None
    files_compared: int = 0
    nested_cdhash: dict = field(default_factory=dict)
    note: str = ("local-install transformation of a verified artifact; not a release artifact; "
                 "the artifact file was not modified")
    reason: str | None = None


# ---------------------------------------------------------------- the steps


def bundle_meta(bundle: Path) -> tuple[str | None, str | None]:
    info = bundle / "Contents/Info.plist"
    try:
        import plistlib
        with info.open("rb") as handle:
            plist = plistlib.load(handle)
        return str(plist.get("CFBundleVersion") or "") or None, plist.get("WispSourceCommit")
    except Exception:  # noqa: BLE001
        return None, None


def describe(path: Path, runner=None) -> dict:
    runner = runner or run
    code, text = runner([CODESIGN, "-dvvv", str(path)])
    if code:
        raise ResignError(f"codesign could not describe {path}: {text.strip()[-400:]}")
    out = parse_display(text)
    code, text = runner([CODESIGN, "-d", "-r-", str(path)])
    out["requirement"] = parse_requirement(text) if not code else None
    return out


def verify(path: Path, runner=None) -> None:
    runner = runner or run
    code, text = runner([CODESIGN, "--verify", "--deep", "--strict", "--verbose=2", str(path)])
    if code:
        raise ResignError(f"codesign --verify failed for {path}: {text.strip()[-600:]}")


def nested_targets(bundle: Path) -> list[Path]:
    """The nested interpreters whose own cdhash is reported: the backend's python3
    and, when an engine is shipped, the Mach-O interpreters in its py/bin (helper
    scripts there are plain files, covered by the tree comparison instead). A path
    that resolves outside the bundle is an error."""
    targets: list[Path] = []
    for rel in NESTED:
        path = bundle / rel
        if not path.exists():
            continue
        candidates = (sorted(p for p in path.iterdir() if p.is_file() and os.access(p, os.X_OK) and is_macho(p))
                      if path.is_dir() else [path])
        for candidate in candidates:
            try:
                candidate.resolve().relative_to(bundle.resolve())
            except ValueError:
                raise ResignError(f"{candidate} resolves outside the bundle")
            targets.append(candidate)
    return targets


def find_identity(name: str, runner=None) -> tuple[str | None, str | None]:
    """(hash, None) for exactly one valid identity with this name, else (None, why)."""
    runner = runner or run
    code, text = runner([SECURITY, "find-identity", "-v", "-p", "codesigning"])
    if code:
        return None, f"`security find-identity` failed ({text.strip()[-200:] or 'no output'}); is the login keychain locked?"
    hashes = parse_identities(text).get(name, [])
    if not hashes:
        return None, f'no valid "{name}" code-signing identity in the keychain'
    if len(hashes) > 1:
        return None, f'{len(hashes)} valid identities are named "{name}" ({", ".join(hashes)}); refusing to guess'
    return hashes[0], None


def previous_requirement(log: Path | None, installed: Path | None = None, runner=None) -> str | None:
    """What the grants are currently bound to: the installed app's own designated
    requirement when `installed` names one (read-only), else the requirement of the
    last logged install, else None."""
    if installed is not None and Path(installed).is_dir():
        runner = runner or run
        code, text = runner([CODESIGN, "-d", "-r-", str(installed)])
        return parse_requirement(text) if not code else None
    if not log or not Path(log).is_file():
        return None
    last = None
    try:
        for line in Path(log).read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = line
        return json.loads(last).get("installed_requirement") if last else None
    except (OSError, ValueError, UnicodeError, AttributeError):
        return None


def resign(artifact: Path, output: Path, *, identity: str = DEFAULT_IDENTITY, log: Path | None = DEFAULT_LOG,
           installed: Path | None = None, require: bool = False, runner=None, copier=None, now=None) -> Record:
    """See the module docstring. `installed`: the app whose grants should carry over
    (its requirement is read, never changed). `require`: raise NotResigned instead
    of staging and logging the ad-hoc fallback."""
    runner = runner or run   # resolved at call time, so tests can swap the module's `run`
    artifact = Path(artifact).resolve()
    output = Path(output).absolute()
    if not (artifact.is_dir() and artifact.suffix == ".app" and (artifact / "Contents/Info.plist").is_file()):
        raise ResignError(f"Not an app bundle: {artifact}")
    if output.exists():
        raise ResignError(f"Output already exists: {output}")
    if not (output.suffix == ".app" or output.name.endswith(".app.new")):
        raise ResignError(f"Output should be a staging bundle path such as /Applications/Wisp.app.new: {output}")
    copy = copier or (lambda src, dst: _ditto(src, dst, runner))

    # The artifact must be the verified thing the pipeline produced, as it is.
    verify(artifact, runner)
    before = describe(artifact, runner)
    build_number, commit = bundle_meta(artifact)
    tree_before = tree_digest(artifact)
    nested_before = {p.relative_to(artifact).as_posix(): describe(p, runner)["cdhash"] for p in nested_targets(artifact)}

    record = Record(at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), artifact=str(artifact),
                    output=str(output), build_number=build_number, commit=commit,
                    artifact_cdhash=before["cdhash"], artifact_signature=before["signature"],
                    artifact_requirement=before["requirement"], resigned=False, identity=identity,
                    identity_hash=None, installed_cdhash=before["cdhash"], installed_signature=before["signature"],
                    installed_requirement=before["requirement"],
                    previous_requirement=previous_requirement(log, installed, runner),
                    files_compared=len(tree_before))
    created = False

    def fresh_copy() -> None:
        nonlocal created
        if output.exists() or output.is_symlink():
            if not created:
                raise ResignError(f"{output} appeared while this ran; refusing to remove what this run did not create")
            shutil.rmtree(output)
            if output.exists() or output.is_symlink():
                raise ResignError(f"Could not remove {output}")
        created = True
        copy(artifact, output)
        # The copy must be the artifact, nothing less: same strict verify, same cdhash.
        verify(output, runner)
        copied = describe(output, runner)
        if copied["cdhash"] != before["cdhash"] or tree_digest(output) != tree_before:
            raise ResignError(f"The copy at {output} differs from the artifact")

    def fallback(reason: str) -> Record:
        record.reason = reason
        if require:
            raise NotResigned(reason)
        return _finish(record, log)

    done = False
    try:
        fresh_copy()
        identity_hash, why = find_identity(identity, runner)
        if identity_hash is None:
            record = fallback(f"Not re-signed: {why}. Installing the ad-hoc artifact; Calendar, Reminders and "
                              "Full Disk Access grants WILL RESET.")
            done = True
            return record
        record.identity_hash = identity_hash
        code, text = runner(sign_command(output, identity_hash))
        if code:
            # Leave the verified ad-hoc copy behind, never a half-signed one.
            if not require:
                fresh_copy()
            record = fallback(f'Not re-signed: codesign refused to sign with "{identity}" ({text.strip()[-300:]}). '
                              "Installing the ad-hoc artifact; Calendar, Reminders and Full Disk Access grants "
                              "WILL RESET. If macOS asked whether codesign may use the key, that prompt is the "
                              "user's to answer; run again afterwards.")
            done = True
            return record

        verify(output, runner)
        after = describe(output, runner)
        problems = []
        if after["identifier"] != BUNDLE_ID:
            problems.append(f"identifier changed to {after['identifier']}")
        if not requirement_matches(after["requirement"], identity_hash):
            problems.append(f"designated requirement is not exactly this bundle id and this identity's leaf: {after['requirement']}")
        if after["authority"][:1] != [identity]:
            problems.append(f"authority is {after['authority'] or 'none'}, not {identity}")
        if after["flags"] and "adhoc" in after["flags"]:
            problems.append(f"still ad-hoc: {after['flags']}")
        if after["cdhash"] in (None, before["cdhash"]):
            problems.append("the outer signature did not change")
        tree_after = tree_digest(output)
        if tree_after != tree_before:
            changed = sorted(set(tree_before) ^ set(tree_after)) + sorted(
                k for k in tree_before.keys() & tree_after.keys() if tree_before[k] != tree_after[k])
            problems.append("files other than the outer signature changed: " + ", ".join(changed[:8]))
        nested_after = {}
        for p in nested_targets(output):
            try:
                nested_after[p.relative_to(output).as_posix()] = describe(p, runner)["cdhash"]
            except ResignError:
                nested_after[p.relative_to(output).as_posix()] = None   # unreadable counts as changed
        for rel, cdhash in nested_before.items():
            same = cdhash is not None and nested_after.get(rel) == cdhash
            record.nested_cdhash[rel] = {"cdhash": cdhash, "unchanged": same}
            if not same:
                problems.append(f"nested signature changed or unreadable: {rel}")
        if problems:
            raise ResignError("Re-signed bundle failed verification: " + "; ".join(problems))
        record.resigned = True
        record.requirement_stable = True
        record.installed_cdhash = after["cdhash"]
        record.installed_signature = "; ".join(after["authority"])
        record.installed_requirement = after["requirement"]
        if record.previous_requirement is not None:
            record.grants_carry_over = record.previous_requirement == record.installed_requirement
        record = _finish(record, log)
        done = True
        return record
    except ResignError:
        raise
    except BaseException as exc:   # noqa: BLE001  (an interrupt too: nothing half-signed may stay)
        raise ResignError(f"Re-sign failed: {exc!r}") from exc
    finally:
        if not done and created:
            shutil.rmtree(output, ignore_errors=True)


def _ditto(src: Path, dst: Path, runner=None) -> None:
    runner = runner or run
    code, text = runner([DITTO, str(src), str(dst)], timeout=1800)
    if code:
        raise ResignError(f"ditto failed: {text.strip()[-300:]}")


def _finish(record: Record, log: Path | None) -> Record:
    if log:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        try:
            os.chmod(log, 0o600)   # a log created by an older run keeps nothing wider
        except OSError:
            pass
    return record


def summary(record: Record) -> str:
    lines = [f"artifact  {record.artifact}",
             f"          build {record.build_number or '?'} commit {(record.commit or '?')[:12]} "
             f"cdhash {record.artifact_cdhash} ({record.artifact_signature})",
             f"output    {record.output}"]
    if record.resigned:
        lines.append(f"          re-signed with {record.identity} ({record.identity_hash}); cdhash {record.installed_cdhash}")
        lines.append(f"          requirement: {record.installed_requirement}")
        lines.append(f"          {record.files_compared} files outside the outer signature unchanged"
                     + (f"; nested cdhash unchanged: {', '.join(sorted(record.nested_cdhash))}" if record.nested_cdhash else ""))
        if record.grants_carry_over:
            lines.append("          Same requirement as the current install: Calendar/Reminders/Full Disk Access grants carry over.")
        elif record.grants_carry_over is False:
            lines.append("          Requirement differs from the current install: macOS will ask once more; "
                         "later installs signed this way keep the grants.")
        else:
            lines.append("          No current install known: the first install signed this way re-prompts once; "
                         "later ones keep the grants.")
    else:
        lines.append("          " + (record.reason or "NOT re-signed"))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("artifact", type=Path, help="the verified Wisp.app the pipeline built")
    parser.add_argument("--output", type=Path, required=True, help="where the re-signed copy goes (must not exist), e.g. /Applications/Wisp.app.new")
    parser.add_argument("--identity", default=DEFAULT_IDENTITY, help='common name of the code-signing identity (default "Wisp Dev")')
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="JSONL install log (default ~/.moe/local-installs.jsonl)")
    parser.add_argument("--installed", type=Path, default=Path("/Applications/Wisp.app"),
                        help="the app whose grants should carry over; its requirement is read, never changed (default /Applications/Wisp.app)")
    parser.add_argument("--require", action="store_true", help="exit 2 with nothing staged or logged when not re-signed")
    args = parser.parse_args(argv)
    try:
        record = resign(args.artifact, args.output, identity=args.identity, log=args.log,
                        installed=args.installed, require=args.require)
    except NotResigned as exc:
        print(f"RESIGN FAILED (--require): {exc}", file=sys.stderr)
        return 2
    except ResignError as exc:
        print(f"RESIGN FAILED: {exc}", file=sys.stderr)
        return 1
    print(summary(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
