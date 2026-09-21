"""Explicit, protected-CI-only Apple signing, notarization and publication hook.

Never called by `all`, branch pushes or tag pushes. Credentials are never logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import stat
import struct
import subprocess
import tempfile
import time

from pipeline import (BoundReleaseAssets, BuildError, CONFIG, GitHubReleaseUploader, ROOT, SUPPORT, archive, clean_env,
                      git, inventory, json_write, relocation_smoke, distribution_roundtrip,
                      validate_native, validate_structure, verify_artifacts, signing_targets, verify_bundle_signature, scan_host_paths)

REQUIRED_SECRETS = (
    "WISP_SIGNING_P12_BASE64", "WISP_SIGNING_P12_PASSWORD", "WISP_SIGNING_IDENTITY",
    "WISP_APPLE_API_KEY_BASE64", "WISP_APPLE_KEY_ID", "WISP_APPLE_ISSUER_ID", "GH_TOKEN",
)


def github_release_for_tag(env, tag):
    """Return one public or draft release for tag, failing closed on API ambiguity."""
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp",
         f"repos/{env['GH_REPO']}/releases?per_page=100"],
        env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise BuildError("Could not safely enumerate existing GitHub releases")
    try:
        pages = json.loads(result.stdout)
        releases = [release for page in pages for release in page]
    except (json.JSONDecodeError, TypeError, ValueError):
        raise BuildError("GitHub release enumeration returned invalid data") from None
    if (not isinstance(pages, list)
            or any(not isinstance(page, list) for page in pages)
            or any(not isinstance(release, dict) for release in releases)):
        raise BuildError("GitHub release enumeration returned invalid data")
    matches = [release for release in releases if release.get("tag_name") == tag]
    if len(matches) > 1:
        raise BuildError("GitHub returned duplicate releases for the version tag")
    return matches[0] if matches else None


def created_draft_release_id(env, tag):
    # GitHub can acknowledge draft creation before the paginated releases API
    # exposes it. Retry only absence; malformed or conflicting records fail now.
    for attempt in range(10):
        release = github_release_for_tag(env, tag)
        if release:
            release_id = release.get("id")
            if release.get("draft") is not True or not isinstance(release_id, int):
                raise BuildError("Could not resolve the newly created GitHub draft release")
            return str(release_id)
        if attempt < 9:
            time.sleep(1)
    raise BuildError("Could not resolve the newly created GitHub draft release")


def preflight(args, env=None):
    env = os.environ if env is None else env
    if args.allow_dirty or args.test_python or args.offline:
        raise BuildError("Release does not accept preview, audit-interpreter or offline flags")
    if env.get("GITHUB_ACTIONS") != "true" or env.get("WISP_RELEASE_APPROVED") != "true":
        raise BuildError("Release requires explicit dispatch through the protected release CI job")
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise BuildError("Release is only available through explicit workflow_dispatch")
    if env.get("GITHUB_REF") != "refs/tags/v" + CONFIG["version"]:
        raise BuildError("Release requires the exact version tag as the dispatch ref")
    missing = [name for name in REQUIRED_SECRETS if not env.get(name)]
    if missing:
        raise BuildError("Missing required CI secrets: " + ", ".join(missing))
    if not env["WISP_SIGNING_IDENTITY"].startswith("Developer ID Application:"):
        raise BuildError("Signing identity must be a Developer ID Application certificate")
    if not args.output:
        raise BuildError("release requires --output pointing to the verified candidate directory")


def ad_hoc_preflight(args, env=None):
    env = os.environ if env is None else env
    if args.allow_dirty or args.test_python or args.offline:
        raise BuildError("Ad-hoc release does not accept preview, audit-interpreter or offline flags")
    if (env.get("GITHUB_ACTIONS") != "true"
            or env.get("WISP_AD_HOC_RELEASE_APPROVED") != "true"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise BuildError("Ad-hoc release requires explicit CI workflow dispatch")
    if env.get("GITHUB_REF") != "refs/tags/v" + CONFIG["version"]:
        raise BuildError("Ad-hoc release requires the exact version tag")
    if not args.output or not env.get("GH_TOKEN") or not env.get("GITHUB_REPOSITORY"):
        raise BuildError("Ad-hoc release requires candidate output and GitHub credentials")


def release_ad_hoc(runner, args):
    """Publish the complete verified asset set; any failed upload stays a draft."""
    ad_hoc_preflight(args)
    candidate = args.output.resolve()
    if not candidate.is_relative_to((ROOT / "dist").resolve()):
        raise BuildError("Release input must be beneath this checkout's dist/")
    tag = "v" + CONFIG["version"]
    with BoundReleaseAssets(candidate) as assets:
        verify_artifacts(candidate, bound_assets=assets)
        provenance = json.loads(assets.read_text("provenance.json"))
        meta = provenance["source"]
        if (provenance.get("signature") != "ad-hoc" or provenance.get("notarized") is not False
                or meta["dirty"] or meta["commit"] != git("rev-parse", "HEAD")
                or meta["version"] != CONFIG["version"] or git("status", "--porcelain")):
            raise BuildError("Ad-hoc candidate must match this clean tagged checkout exactly")
        if (not provenance.get("toolchain", {}).get("strict_toolchain")
                or not provenance.get("tests")
                or any(row["exit_code"] and not row.get("optional") for row in provenance["tests"])):
            raise BuildError("Ad-hoc candidate requires strict toolchain and passing validation")
        if git("rev-parse", f"refs/tags/{tag}^{{commit}}") != meta["commit"]:
            raise BuildError("Tag does not point to candidate source")
        runner.run("tag-on-main", ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"])
        archives = [name for name in assets.descriptors if name.endswith(".zip")]
        if len(archives) != 1:
            raise BuildError("Ad-hoc release requires exactly one verified ZIP")
        archive_name = archives[0]
        distribution_roundtrip(runner, candidate / archive_name, candidate / "Wisp.app", meta,
                               archive_descriptor=assets.descriptors[archive_name])
        assets.assert_paths_unchanged()
        env = dict(clean_env(), GH_TOKEN=os.environ["GH_TOKEN"],
                   GH_REPO=os.environ["GITHUB_REPOSITORY"])
        if github_release_for_tag(env, tag):
            raise BuildError("Release already exists; refusing to modify it")
        notes_descriptor = assets.descriptors["release-notes.md"]
        assets.assert_paths_unchanged()
        os.lseek(notes_descriptor, 0, os.SEEK_SET)
        runner.run("create-draft-release", ["gh", "release", "create", tag, "--verify-tag",
                   "--draft", "--title", f"Wisp {meta['version']}",
                   "--notes-file", f"/dev/fd/{notes_descriptor}"], env=env,
                   pass_fds=(notes_descriptor,))
        release_id = created_draft_release_id(env, tag)
        uploader = GitHubReleaseUploader(env["GH_REPO"], release_id, env["GH_TOKEN"])
        # Unlike the signed path, preserve the candidate checksum manifest byte
        # for byte, including its release-notes entry. Upload every listed file.
        uploads = assets.upload_assets() + [("release-notes.md", "text/markdown; charset=utf-8",
            notes_descriptor, assets.digests["release-notes.md"], os.fstat(notes_descriptor).st_size)]
        for asset in uploads:
            assets.assert_paths_unchanged()
            uploader.upload(*asset)
        assets.assert_paths_unchanged()
        runner.run("publish-release", ["gh", "release", "edit", tag, "--draft=false"], env=env)


def secret_run(command, *, env=None, pass_fds=()):
    """Suppress argv/output: keychain/import utilities take secrets as arguments."""
    env = clean_env() if env is None else env
    try:
        result = subprocess.run([str(x) for x in command], env=env, pass_fds=pass_fds,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=120)
    except (subprocess.SubprocessError, OSError):
        raise BuildError("Private credential operation failed; command and output suppressed") from None
    if result.returncode:
        raise BuildError(f"Private credential operation failed ({Path(command[0]).name}); output intentionally suppressed")


ENTITLEMENT_SOURCES = {
    "app": "build-support/app.entitlements",
    "python": "build-support/python.entitlements",
}


def canonical_entitlements(data):
    try:
        value = plistlib.loads(data) if isinstance(data, (bytes, bytearray)) else data
    except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError):
        raise BuildError("Entitlement plist is invalid") from None
    if not isinstance(value, dict):
        raise BuildError("Entitlement plist must contain a dictionary")
    return plistlib.dumps(value, fmt=plistlib.FMT_BINARY, sort_keys=True)


def _git_blob(commit, relative):
    if not re.fullmatch(r"[a-f0-9]{40,64}", commit) or relative not in ENTITLEMENT_SOURCES.values():
        raise BuildError("Invalid immutable entitlement source")
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{commit}:{relative}"],
            env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30)
    except (subprocess.SubprocessError, OSError):
        raise BuildError("Could not bind entitlement source from candidate commit") from None
    if result.returncode:
        raise BuildError("Could not bind entitlement source from candidate commit")
    return result.stdout


class BoundEntitlements:
    """Immutable-commit entitlement copies retained by no-follow descriptors."""
    def __init__(self, commit, destination):
        self.commit = commit
        self.destination = Path(destination)
        self.descriptors = {}
        self.paths = {}
        self.source_bytes = {}
        self.expected = {}

    def __enter__(self):
        try:
            self.destination.mkdir(mode=0o700, exist_ok=False)
            for role, source in ENTITLEMENT_SOURCES.items():
                data = _git_blob(self.commit, source)
                expected = canonical_entitlements(data)
                path = self.destination / f"{role}.plist"
                writer = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600)
                try:
                    offset = 0
                    while offset < len(data):
                        count = os.write(writer, data[offset:])
                        if count <= 0:
                            raise BuildError("Could not bind entitlement source")
                        offset += count
                    os.fsync(writer)
                finally:
                    os.close(writer)
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    os.close(descriptor)
                    raise BuildError("Bound entitlement copy is not private")
                self.descriptors[role] = descriptor
                self.paths[role] = path
                self.source_bytes[role] = data
                self.expected[role] = expected
            return self
        except Exception:
            self.close()
            raise

    def argument(self, role):
        try:
            descriptor = self.descriptors[role]
            expected = self.source_bytes[role]
        except KeyError:
            raise BuildError("Missing bound entitlement role") from None
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(expected):
            raise BuildError("Bound entitlement identity changed")
        if os.pread(descriptor, len(expected) + 1, 0) != expected:
            raise BuildError("Bound entitlement content changed")
        # /dev/fd consumers inherit this open file description and advance its
        # shared offset. Rewind immediately before every codesign invocation so
        # repeated targets using the same entitlement role receive exact bytes.
        os.lseek(descriptor, 0, os.SEEK_SET)
        return f"/dev/fd/{descriptor}", descriptor

    def close(self):
        for descriptor in self.descriptors.values():
            os.close(descriptor)
        self.descriptors.clear()

    def __exit__(self, *_args):
        self.close()


def signed_entitlements(target):
    try:
        result = subprocess.run(
            ["/usr/bin/codesign", "--display", "--entitlements", ":-", str(target)],
            env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except (subprocess.SubprocessError, OSError):
        raise BuildError("Could not inspect signed entitlements") from None
    if result.returncode:
        raise BuildError("Could not inspect signed entitlements")
    for stream in (result.stdout, result.stderr):
        start = stream.find(b"<?xml")
        end = stream.find(b"</plist>", start)
        if start >= 0 and end >= 0:
            return canonical_entitlements(stream[start:end + len(b"</plist>")])
        if stream.startswith(b"bplist00"):
            return canonical_entitlements(stream)
    # codesign may omit an explicitly empty entitlement dictionary.
    return canonical_entitlements({})


def verify_signed_entitlements(bundle, entitlements):
    main_executable = bundle_main_executable(bundle)
    for target in signing_targets(bundle):
        role = entitlement_role(bundle, target, main_executable)
        if signed_entitlements(target) != entitlements.expected[role]:
            raise BuildError(
                f"Signed entitlements differ from candidate source: {target.relative_to(bundle)}")


def bundle_main_executable(bundle):
    try:
        document = plistlib.loads((Path(bundle) / "Contents/Info.plist").read_bytes())
        name = document["CFBundleExecutable"]
    except (OSError, KeyError, plistlib.InvalidFileException, TypeError, ValueError):
        raise BuildError("Bundle executable identity is invalid") from None
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise BuildError("Bundle executable identity is invalid")
    return Path(bundle) / "Contents/MacOS" / name


def entitlement_role(bundle, target, main_executable=None):
    bundle, target = Path(bundle), Path(target)
    main_executable = (bundle_main_executable(bundle) if main_executable is None
                       else Path(main_executable))
    return "app" if target in (bundle, main_executable) else "python"


def developer_sign(runner, bundle, identity, keychain, entitlements):
    """Replace every local ad-hoc signature; called only after release preflight."""
    scan_host_paths(bundle)
    main_executable = bundle_main_executable(bundle)
    for target in signing_targets(bundle):
        role = entitlement_role(bundle, target, main_executable)
        entitlement, descriptor = entitlements.argument(role)
        secret_run(["/usr/bin/codesign", "--force", "--sign", identity, "--keychain", keychain,
                    "--timestamp", "--options", "runtime", "--entitlements", entitlement, target],
                   pass_fds=(descriptor,))
    verify_bundle_signature(bundle, "Developer ID Application", runner, "developer-verification")
    verify_signed_entitlements(bundle, entitlements)


def copy_bound_candidate(candidate, destination, assets):
    candidate = Path(candidate).resolve(strict=True)
    if assets.destination != candidate:
        raise BuildError("Bound candidate assets belong to a different directory")
    destination.mkdir(exist_ok=False)
    bundle = destination / "Wisp.app"
    shutil.copytree(candidate / "Wisp.app", bundle, symlinks=True)
    for name in ("dependencies.json", "release-notes.md", "simulation-qa.json"):
        target = destination / name
        with target.open("xb") as output:
            output.write(assets.read_bytes(name))
    try:
        expected_inventory = json.loads(assets.read_text("bundle-manifest.json"))
    except (KeyError, ValueError, TypeError):
        raise BuildError("Candidate bundle manifest is invalid") from None
    if inventory(bundle) != expected_inventory:
        raise BuildError("Copied release bundle differs from bound candidate inventory")
    return bundle, expected_inventory


def _thin_macho_identity(data):
    formats = {
        b"\xcf\xfa\xed\xfe": ("<", 32), b"\xfe\xed\xfa\xcf": (">", 32),
        b"\xce\xfa\xed\xfe": ("<", 28), b"\xfe\xed\xfa\xce": (">", 28),
    }
    try:
        endian, header_size = formats[data[:4]]
        commands = struct.unpack_from(endian + "I", data, 16)[0]
    except (KeyError, struct.error):
        raise BuildError("Malformed signed Mach-O payload") from None
    canonical = bytearray(data)
    offset, signature, linkedit = header_size, None, 0
    for _ in range(commands):
        try:
            command, size = struct.unpack_from(endian + "II", canonical, offset)
        except struct.error:
            raise BuildError("Malformed signed Mach-O payload") from None
        if size < 8 or offset + size > len(canonical):
            raise BuildError("Malformed signed Mach-O payload")
        if command == 0x19 and size >= 72:
            segment = bytes(canonical[offset + 8:offset + 24]).rstrip(b"\0")
            if segment == b"__LINKEDIT":
                linkedit += 1
                canonical[offset + 32:offset + 40] = b"\0" * 8
                canonical[offset + 48:offset + 56] = b"\0" * 8
        elif command == 0x1 and size >= 56:
            segment = bytes(canonical[offset + 8:offset + 24]).rstrip(b"\0")
            if segment == b"__LINKEDIT":
                linkedit += 1
                canonical[offset + 28:offset + 32] = b"\0" * 4
                canonical[offset + 36:offset + 40] = b"\0" * 4
        if command == 0x1D:
            if size < 16 or signature is not None:
                raise BuildError("Malformed signed Mach-O payload")
            start, length = struct.unpack_from(endian + "II", canonical, offset + 8)
            if start < offset + size or start + length > len(canonical):
                raise BuildError("Malformed signed Mach-O signature")
            signature = (start, length)
            canonical[offset + 8:offset + 16] = b"\0" * 8
        offset += size
    if signature is None or linkedit != 1:
        raise BuildError("Signed Mach-O payload lacks unique signature container")
    start, length = signature
    payload = canonical[:start] + canonical[start + length:]
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}


def _macho_identity(data):
    if data[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                    b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"}:
        return {"thin": _thin_macho_identity(data)}
    fat_formats = {
        b"\xca\xfe\xba\xbe": (">", 20, False), b"\xbe\xba\xfe\xca": ("<", 20, False),
        b"\xca\xfe\xba\xbf": (">", 32, True), b"\xbf\xba\xfe\xca": ("<", 32, True),
    }
    try:
        endian, entry_size, fat64 = fat_formats[data[:4]]
        count = struct.unpack_from(endian + "I", data, 4)[0]
    except (KeyError, struct.error):
        raise BuildError("Malformed signed Mach-O payload") from None
    if count < 1 or count > 64 or 8 + entry_size * count > len(data):
        raise BuildError("Malformed signed universal Mach-O payload")
    slices, ranges = [], []
    for index in range(count):
        try:
            if fat64:
                cpu, subtype, offset, size, align, reserved = struct.unpack_from(
                    endian + "IIQQII", data, 8 + index * entry_size)
            else:
                cpu, subtype, offset, size, align = struct.unpack_from(
                    endian + "IIIII", data, 8 + index * entry_size)
                reserved = 0
        except struct.error:
            raise BuildError("Malformed signed universal Mach-O payload") from None
        if (align > (63 if fat64 else 31) or reserved != 0 or size < 1 or offset < 8 + entry_size * count
                or offset + size > len(data) or offset % (1 << align)):
            raise BuildError("Malformed signed universal Mach-O payload")
        if any(offset < end and start < offset + size for start, end in ranges):
            raise BuildError("Overlapping universal Mach-O slices")
        ranges.append((offset, offset + size))
        slices.append({"cpu": cpu, "subtype": subtype, "align": align,
                       "identity": _thin_macho_identity(data[offset:offset + size])})
    return {"universal": slices, "fat64": fat64}


def signature_insensitive_inventory(bundle):
    bundle = Path(bundle)
    root = bundle.lstat()
    if not stat.S_ISDIR(root.st_mode):
        raise BuildError("Signed inventory root must be a directory")
    rows = {".": {"directory": True, "mode": stat.S_IMODE(root.st_mode)}}
    for path in sorted(Path(bundle).rglob("*")):
        relative = path.relative_to(bundle)
        if "_CodeSignature" in relative.parts:
            continue
        name = relative.as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            rows[name] = {"symlink": os.readlink(path)}
        elif stat.S_ISDIR(info.st_mode):
            rows[name] = {"directory": True, "mode": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode):
            data = path.read_bytes()
            value = {"mode": stat.S_IMODE(info.st_mode)}
            if data[:4] in {
                    b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                    b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
                    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}:
                value["macho"] = _macho_identity(data)
            else:
                value.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
            rows[name] = value
        else:
            raise BuildError("Unsupported release bundle entry")
    return rows


def prepare_private_candidate(candidate, destination, assets):
    bundle, expected_inventory = copy_bound_candidate(candidate, destination, assets)
    # Deliberately repeat the raw check after copy_bound_candidate returns. This
    # closes substitution hooks between the copy helper and credential setup.
    if inventory(bundle) != expected_inventory:
        raise BuildError("Private signing bundle changed after candidate copy")
    return bundle, expected_inventory, signature_insensitive_inventory(bundle)


def _write_sanitized_notarization_evidence(path, receipt):
    path = Path(path)
    body = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        offset = 0
        while offset < len(body):
            count = os.write(descriptor, body[offset:])
            if count <= 0:
                raise BuildError("Could not preserve notarization evidence")
            offset += count
        os.fsync(descriptor)
    except OSError:
        raise BuildError("Could not preserve notarization evidence") from None
    finally:
        os.close(descriptor)


def record_notarization(prepared, result, evidence_path=None):
    try:
        receipt = json.loads(result.stdout)
    except ValueError:
        receipt = {"id": None, "status": "invalid_response", "message": None}
    safe_receipt = {key: receipt.get(key) for key in ("id", "status", "message")}
    json_write(Path(prepared) / "notarization.json", safe_receipt)
    if evidence_path is not None:
        _write_sanitized_notarization_evidence(evidence_path, safe_receipt)
    if result.returncode or receipt.get("status") != "Accepted":
        raise BuildError(
            f"Notarization was not accepted; submission {receipt.get('id')}. See recovery documentation.")
    return safe_receipt


def install_then_publish(runner, prepared, destination, tag, env):
    try:
        os.replace(prepared, destination)
        parent = os.open(Path(destination).parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError:
        raise BuildError("Could not install verified release output; GitHub draft remains unpublished") from None
    runner.run("publish-release", ["gh", "release", "edit", tag, "--draft=false"], env=env)


def release(runner, args):
    # All validation and credential checks precede any signing/upload/publication.
    preflight(args)
    candidate = args.output.resolve()
    if not candidate.is_relative_to((ROOT / "dist").resolve()):
        raise BuildError("Release input must be beneath this checkout's dist/")
    destination = candidate.parent / (candidate.name + "-signed")
    evidence_path = candidate.parent / (candidate.name + "-notarization-evidence.json")
    if destination.exists() or destination.is_symlink():
        raise BuildError("Signed release destination already exists")
    if evidence_path.exists() or evidence_path.is_symlink():
        raise BuildError("Notarization evidence already exists; retain it before retrying")
    with tempfile.TemporaryDirectory(prefix=".wisp-signing-",
                                     dir=candidate.parent) as tmp:
        private = Path(tmp)
        private.chmod(0o700)
        prepared = private / "release"
        # Keep every candidate metadata object bound while it is verified and
        # copied into the unpredictable private signing workspace.
        with BoundReleaseAssets(candidate) as candidate_assets:
            verify_artifacts(candidate, bound_assets=candidate_assets)
            try:
                provenance = json.loads(candidate_assets.read_text("provenance.json"))
                meta = provenance["source"]
            except (KeyError, ValueError, TypeError):
                raise BuildError("Candidate provenance is invalid") from None
            if provenance.get("signature") != "ad-hoc" or provenance.get("notarized") is not False:
                raise BuildError("Protected release requires a verified local ad-hoc candidate")
            if meta["dirty"] or meta["commit"] != git("rev-parse", "HEAD") or git("status", "--porcelain"):
                raise BuildError("Release candidate must match this clean checkout exactly")
            if not provenance["toolchain"]["strict_toolchain"]:
                raise BuildError("Candidate must pass --strict-toolchain before signing")
            if any(row["exit_code"] and not row.get("optional") for row in provenance["tests"]):
                raise BuildError("Release candidate contains failed validation steps")
            runner.run("tag-on-main", ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"])
            if git("rev-parse", f"refs/tags/v{CONFIG['version']}^{{commit}}") != meta["commit"]:
                raise BuildError("Tag does not point to candidate source")
            bundle, expected_inventory, unsigned_identity = prepare_private_candidate(
                candidate, prepared, candidate_assets)
            candidate_checksum = candidate_assets.digests["SHA256SUMS"]
        # Re-establish raw equivalence immediately before any GitHub or signing
        # credential is read. The private parent remains mode 0700 thereafter.
        if inventory(bundle) != expected_inventory:
            raise BuildError("Private signing bundle changed before credential use")
        with BoundEntitlements(meta["commit"], private / "entitlements") as entitlements:
            env = dict(clean_env(), GH_TOKEN=os.environ["GH_TOKEN"],
                       GH_REPO=os.environ["GITHUB_REPOSITORY"])
            tag = "v" + CONFIG["version"]
            if github_release_for_tag(env, tag):
                raise BuildError("Release already exists; recover the existing draft manually after review")
            # Ephemeral keychain only. Never change default keychain or global search list.
            keychain = private / "release.keychain-db"
            p12 = private / "identity.p12"
            api_key = private / "AuthKey.p8"
            keychain_password = secrets.token_urlsafe(32)
            created = False
            try:
                for path, var in ((p12, "WISP_SIGNING_P12_BASE64"), (api_key, "WISP_APPLE_API_KEY_BASE64")):
                    path.write_bytes(base64.b64decode(os.environ[var], validate=True))
                    path.chmod(0o600)
                secret_run(["security", "create-keychain", "-p", keychain_password, keychain])
                created = True
                secret_run(["security", "set-keychain-settings", "-lut", "3600", keychain])
                secret_run(["security", "unlock-keychain", "-p", keychain_password, keychain])
                secret_run(["security", "import", p12, "-k", keychain, "-P", os.environ["WISP_SIGNING_P12_PASSWORD"], "-T", "/usr/bin/codesign"])
                secret_run(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", keychain_password, keychain])
                identity = os.environ["WISP_SIGNING_IDENTITY"]
                if inventory(bundle) != expected_inventory:
                    raise BuildError("Private signing bundle changed immediately before signing")
                developer_sign(runner, bundle, identity, keychain, entitlements)
                if signature_insensitive_inventory(bundle) != unsigned_identity:
                    raise BuildError("Developer signing changed release payload content")
                validate_structure(bundle, meta)
                validate_native(runner, bundle, meta)
                relocation_smoke(runner, bundle)
                submission = private / "notarization.zip"
                archive(bundle, submission, meta["source_epoch"])
                # API key content and private args never appear in command/output logs.
                try:
                    result = subprocess.run(["xcrun", "notarytool", "submit", str(submission), "--key", str(api_key),
                        "--key-id", os.environ["WISP_APPLE_KEY_ID"], "--issuer", os.environ["WISP_APPLE_ISSUER_ID"],
                        "--wait", "--timeout", "30m", "--output-format", "json"], env=clean_env(), capture_output=True, text=True, timeout=1900)
                except (subprocess.SubprocessError, OSError):
                    raise BuildError("Notarization did not complete; inspect Apple's submission history before retrying") from None
                safe_receipt = record_notarization(prepared, result, evidence_path)
                runner.run("staple-ticket", ["xcrun", "stapler", "staple", bundle])
                runner.run("validate-ticket", ["xcrun", "stapler", "validate", bundle])
                runner.run("gatekeeper", ["spctl", "--assess", "--type", "execute", "--verbose=2", bundle])
                runner.run("verify-stapled-signature", ["codesign", "--verify", "--deep", "--strict", bundle])
                if signature_insensitive_inventory(bundle) != unsigned_identity:
                    raise BuildError("Stapling changed release payload content")
                verify_signed_entitlements(bundle, entitlements)
            finally:
                if created:
                    secret_run(["security", "delete-keychain", keychain])
        json_write(prepared / "bundle-manifest.json", inventory(bundle))
        provenance.update(signature="Developer ID Application", notarized=True,
                          notarization=safe_receipt, candidate_sha256=candidate_checksum)
        json_write(prepared / "provenance.json", provenance)
        zip_path = prepared / f"Wisp-{meta['version']}-{meta['build_number']}-arm64.zip"
        runner.run("archive-notarized-app", ["ditto", "-c", "-k", "--sequesterRsrc",
            "--keepParent", bundle, zip_path])
        with BoundReleaseAssets(prepared) as assets:
            archive_descriptor = assets.descriptors[zip_path.name]
            distribution_roundtrip(runner, zip_path, bundle, meta, notarized=True,
                                   archive_descriptor=archive_descriptor)
            assets.write_checksums(exclude={"release-notes.md"})
            verify_artifacts(prepared, bound_assets=assets, publication_asset_set=True)
            notes_descriptor = assets.descriptors["release-notes.md"]
            os.lseek(notes_descriptor, 0, os.SEEK_SET)
            notes = f"/dev/fd/{notes_descriptor}"
            runner.run("create-draft-release", ["gh", "release", "create", tag,
                "--verify-tag", "--draft", "--title", f"Wisp {meta['version']}",
                "--notes-file", notes], env=env, pass_fds=(notes_descriptor,))
            release_id = created_draft_release_id(env, tag)
            uploader = GitHubReleaseUploader(env["GH_REPO"], release_id, env["GH_TOKEN"])
            for name, content_type, descriptor, digest, size in assets.upload_assets():
                uploader.upload(name, content_type, descriptor, digest, size)
        install_then_publish(runner, prepared, destination, tag, env)
    print(f"Published verified release {tag}; signed artifacts: {destination}")
