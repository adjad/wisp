#!/usr/bin/env python3
"""Re-sign a verified Wisp.app for a LOCAL install with the "Wisp Dev" identity.

Why: every pipeline artifact is ad-hoc signed, so its designated requirement is
`cdhash H"…"` and macOS treats each build as a new app. The Calendar, Reminders
and Full Disk Access grants the user gave the previous build die on every
install. A stable self-signed code-signing identity ("Wisp Dev", in the login
keychain) gives every build the same designated requirement
(`identifier "com.wisp.assistant" and certificate leaf = H"…"`), so the grants
survive.

What this does, and only this:
  * copies the verified artifact to --output (never signs the artifact in place),
  * re-signs the OUTER bundle only (no --deep): the nested backend interpreter
    and the bundled engine keep the signatures the pipeline gave them, which the
    attribution checks (service/inference/bundled_peer.py, attributed_transport.py)
    read from the running processes,
  * preserves identifier, entitlements, flags and runtime; lets codesign derive
    the new designated requirement from the certificate,
  * verifies the result (`codesign --verify --deep --strict`, the requirement,
    unchanged nested signatures) and appends one JSON line to the local install
    log with the artifact's cdhash next to the installed one.

When the identity is missing or codesign refuses, the output is the untouched
ad-hoc copy and this says so plainly: the grants will reset again.

This is a local-install transformation. It is NOT part of the CI artifact
pipeline, release.py, or the provenance rule that a verified artifact is ad-hoc
or Developer ID signed; those are unchanged and the artifact itself is never
modified.

Usage:
  scripts/resign_local_install.py dist/preview/Wisp.app --output /Applications/Wisp.app.new
"""
from __future__ import annotations

import argparse
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
# Nested code whose signatures must come out exactly as they went in.
NESTED = ("Contents/Resources/backend/.venv/bin/python3",
          "Contents/Resources/engine")


def clean_env() -> dict:
    return {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "LANG": "C"}


def run(argv, timeout=600):
    """Run one tool; stdout+stderr as text, plus the return code."""
    result = subprocess.run([str(a) for a in argv], env=clean_env(), capture_output=True,
                            text=True, timeout=timeout)
    return result.returncode, result.stdout + result.stderr


# ---------------------------------------------------------------- pure parsing

def parse_identities(listing: str) -> dict[str, str]:
    """`security find-identity -v -p codesigning` → {common name: SHA-1 hash}.
    Only lines of the valid-identities table; a name that appears twice keeps the
    first (the keychain search order), as codesign itself would pick."""
    found: dict[str, str] = {}
    for line in listing.splitlines():
        match = re.match(r'\s*\d+\)\s+([0-9A-F]{40})\s+"(.+)"\s*$', line)
        if match and match.group(2) not in found:
            found[match.group(2)] = match.group(1)
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
    """The designated requirement line of `codesign -d -r-`, without its prefix."""
    for line in text.splitlines():
        if line.startswith("designated =>"):
            return line[len("designated =>"):].strip()
    return None


def requirement_is_stable(requirement: str | None, identity_hash: str | None = None) -> bool:
    """A requirement grants survive: bound to the bundle identifier and a
    certificate (leaf), never to one build's cdhash."""
    if not requirement or "cdhash" in requirement:
        return False
    if f'identifier "{BUNDLE_ID}"' not in requirement:
        return False
    return "certificate leaf" in requirement or "certificate root" in requirement or "anchor" in requirement


def sign_command(bundle: Path, identity_hash: str) -> list[str]:
    """The one codesign invocation: outer bundle only, by hash (never by name, so
    two identities called the same can't be confused), no network timestamp for a
    self-signed cert, keep what the pipeline set except the ad-hoc requirement."""
    return [CODESIGN, "--force", "--sign", identity_hash, "--timestamp=none",
            "--preserve-metadata=identifier,entitlements,flags,runtime", str(bundle)]


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
    nested_unchanged: dict = field(default_factory=dict)
    grants_survive: bool = False
    note: str = ("local-install transformation of a verified artifact; not a release artifact; "
                 "the artifact file was not modified")
    reason: str | None = None


# ---------------------------------------------------------------- the steps

class ResignError(Exception):
    pass


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


def verify(path: Path, runner=None, deep=True) -> None:
    runner = runner or run
    command = [CODESIGN, "--verify", "--strict", "--verbose=2", str(path)]
    if deep:
        command.insert(2, "--deep")
    code, text = runner(command)
    if code:
        raise ResignError(f"codesign --verify failed for {path}: {text.strip()[-600:]}")


def nested_targets(bundle: Path) -> list[Path]:
    """The nested code whose signatures are compared before and after: the backend
    interpreter, and every Mach-O or bundle directly under the engine root when one
    is shipped (preview builds ship none)."""
    targets = []
    for rel in NESTED:
        path = bundle / rel
        if not path.exists():
            continue
        if path.is_dir() and rel.endswith("engine"):
            for child in sorted(path.iterdir()):
                if child.suffix in (".app", ".framework") or (child.is_file() and os.access(child, os.X_OK)):
                    targets.append(child)
        else:
            targets.append(path)
    return targets


def find_identity(name: str, runner=None) -> str | None:
    runner = runner or run
    code, text = runner([SECURITY, "find-identity", "-v", "-p", "codesigning"])
    if code:
        return None
    return parse_identities(text).get(name)


def resign(artifact: Path, output: Path, *, identity: str = DEFAULT_IDENTITY, log: Path = DEFAULT_LOG,
           runner=None, copier=None, now=None) -> Record:
    runner = runner or run   # resolved at call time, so tests can swap the module's `run`
    artifact = Path(artifact)
    output = Path(output)
    if not (artifact.is_dir() and artifact.suffix == ".app" and (artifact / "Contents/Info.plist").is_file()):
        raise ResignError(f"Not an app bundle: {artifact}")
    if output.exists():
        raise ResignError(f"Output already exists: {output}")
    if output.suffix not in (".app", ".new") and not output.name.endswith(".app.new"):
        raise ResignError(f"Output should be a staging bundle path such as /Applications/Wisp.app.new: {output}")

    # The artifact must be the verified thing the pipeline produced, as it is.
    verify(artifact, runner)
    before = describe(artifact, runner)
    build_number, commit = bundle_meta(artifact)
    nested_before = {str(p.relative_to(artifact)): describe(p, runner)["cdhash"] for p in nested_targets(artifact)}

    record = Record(at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), artifact=str(artifact),
                    output=str(output), build_number=build_number, commit=commit,
                    artifact_cdhash=before["cdhash"], artifact_signature=before["signature"],
                    artifact_requirement=before["requirement"], resigned=False, identity=identity,
                    identity_hash=None, installed_cdhash=before["cdhash"], installed_signature=before["signature"],
                    installed_requirement=before["requirement"])

    # Copy first; the artifact is never signed in place.
    (copier or (lambda src, dst: _ditto(src, dst, runner)))(artifact, output)
    try:
        identity_hash = find_identity(identity, runner)
        if identity_hash is None:
            record.reason = (f'No valid "{identity}" code-signing identity in the keychain; installing the '
                             "ad-hoc artifact. Calendar, Reminders and Full Disk Access grants WILL RESET.")
            return _finish(record, log)
        record.identity_hash = identity_hash
        code, text = runner(sign_command(output, identity_hash))
        if code:
            # Leave a clean ad-hoc copy behind, never a half-signed one.
            shutil.rmtree(output, ignore_errors=True)
            (copier or (lambda src, dst: _ditto(src, dst, runner)))(artifact, output)
            record.reason = (f'codesign refused to sign with "{identity}" ({text.strip()[-300:]}); installing the '
                             "ad-hoc artifact. Calendar, Reminders and Full Disk Access grants WILL RESET. "
                             "A keychain prompt to allow codesign to use the key must be answered by the user.")
            return _finish(record, log)

        verify(output, runner)
        after = describe(output, runner)
        nested_after = {str(p.relative_to(output)): describe(p, runner)["cdhash"] for p in nested_targets(output)}
        for rel, cdhash in nested_before.items():
            record.nested_unchanged[rel] = nested_after.get(rel) == cdhash
        problems = []
        if after["identifier"] != BUNDLE_ID:
            problems.append(f"identifier changed to {after['identifier']}")
        if not requirement_is_stable(after["requirement"], identity_hash):
            problems.append(f"designated requirement is not certificate-bound: {after['requirement']}")
        if identity not in " ".join(after["authority"]):
            problems.append(f"authority is {after['authority'] or 'none'}, not {identity}")
        if not all(record.nested_unchanged.values()) or set(nested_after) != set(nested_before):
            problems.append("a nested signature changed: " + json.dumps(record.nested_unchanged))
        if problems:
            shutil.rmtree(output, ignore_errors=True)
            raise ResignError("Re-signed bundle failed verification: " + "; ".join(problems))
        record.resigned = True
        record.installed_cdhash = after["cdhash"]
        record.installed_signature = "; ".join(after["authority"]) or after["signature"]
        record.installed_requirement = after["requirement"]
        record.grants_survive = True
        return _finish(record, log)
    except ResignError:
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(output, ignore_errors=True)
        raise ResignError(f"Re-sign failed: {exc}") from exc


def _ditto(src: Path, dst: Path, runner=None) -> None:
    runner = runner or run
    code, text = runner([DITTO, str(src), str(dst)], timeout=1800)
    if code:
        raise ResignError(f"ditto failed: {text.strip()[-300:]}")


def _finish(record: Record, log: Path) -> Record:
    if log:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        try:
            os.chmod(log, 0o600)
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
        lines.append("          nested signatures unchanged: " + (", ".join(sorted(record.nested_unchanged)) or "none shipped"))
        lines.append("          Calendar/Reminders/Full Disk Access grants survive installs signed this way.")
    else:
        lines.append("          NOT re-signed: " + (record.reason or "unknown reason"))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("artifact", type=Path, help="the verified Wisp.app the pipeline built")
    parser.add_argument("--output", type=Path, required=True, help="where the re-signed copy goes (must not exist), e.g. /Applications/Wisp.app.new")
    parser.add_argument("--identity", default=DEFAULT_IDENTITY, help='common name of the code-signing identity (default "Wisp Dev")')
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="JSONL install log (default ~/.moe/local-installs.jsonl)")
    parser.add_argument("--require", action="store_true", help="fail instead of falling back to the ad-hoc copy")
    args = parser.parse_args(argv)
    try:
        record = resign(args.artifact, args.output, identity=args.identity, log=args.log)
    except ResignError as exc:
        print(f"RESIGN FAILED: {exc}", file=sys.stderr)
        return 1
    print(summary(record))
    if args.require and not record.resigned:
        print("RESIGN FAILED: --require set and the bundle was not re-signed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
