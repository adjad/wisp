"""Explicit, protected-CI-only Apple signing, notarization and publication hook.

Never called by `all`, branch pushes or tag pushes. Credentials are never logged.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile

from pipeline import (BoundReleaseAssets, BuildError, CONFIG, GitHubReleaseUploader, ROOT, SUPPORT, archive,
                      git, inventory, json_write, relocation_smoke, distribution_roundtrip,
                      validate_native, validate_structure, verify_artifacts, signing_targets, verify_bundle_signature, scan_host_paths)

REQUIRED_SECRETS = (
    "WISP_SIGNING_P12_BASE64", "WISP_SIGNING_P12_PASSWORD", "WISP_SIGNING_IDENTITY",
    "WISP_APPLE_API_KEY_BASE64", "WISP_APPLE_KEY_ID", "WISP_APPLE_ISSUER_ID", "GH_TOKEN",
)


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


def secret_run(command, *, env=None):
    """Suppress argv/output: keychain/import utilities take secrets as arguments."""
    try:
        result = subprocess.run([str(x) for x in command], env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=120)
    except (subprocess.SubprocessError, OSError):
        raise BuildError("Private credential operation failed; command and output suppressed") from None
    if result.returncode:
        raise BuildError(f"Private credential operation failed ({Path(command[0]).name}); output intentionally suppressed")


def developer_sign(runner, bundle, identity, keychain):
    """Replace every local ad-hoc signature; called only after release preflight."""
    scan_host_paths(bundle)
    for target in signing_targets(bundle):
        entitlement = SUPPORT / ("app.entitlements" if target == bundle else "python.entitlements")
        secret_run(["/usr/bin/codesign", "--force", "--sign", identity, "--keychain", keychain,
                    "--timestamp", "--options", "runtime", "--entitlements", entitlement, target])
    verify_bundle_signature(bundle, "Developer ID Application", runner, "developer-verification")


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
    return bundle


def release(runner, args):
    # All validation and credential checks precede any signing/upload/publication.
    preflight(args)
    candidate = args.output.resolve()
    if not candidate.is_relative_to((ROOT / "dist").resolve()):
        raise BuildError("Release input must be beneath this checkout's dist/")
    destination = candidate.parent / (candidate.name + "-signed")
    # Keep every candidate metadata object bound while it is verified and
    # copied. The bundle copy is compared with the retained manifest before
    # any release credential, signing identity, or publication token is used.
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
        bundle = copy_bound_candidate(candidate, destination, candidate_assets)
        candidate_checksum = candidate_assets.digests["SHA256SUMS"]
    env = dict(runner.env, GH_TOKEN=os.environ["GH_TOKEN"], GH_REPO=os.environ["GITHUB_REPOSITORY"])
    tag = "v" + CONFIG["version"]
    # Refuse to overwrite an existing release, including a previous draft.
    existing = subprocess.run(["gh", "api", f"repos/{env['GH_REPO']}/releases/tags/{tag}"],
                              env=env, capture_output=True, text=True, timeout=60)
    if existing.returncode == 0:
        raise BuildError("Release already exists; recover the existing draft manually after review")
    if "404" not in existing.stderr:
        raise BuildError("Could not safely establish that the GitHub release is absent")
    # Ephemeral keychain only. Never change default keychain or global search list.
    with tempfile.TemporaryDirectory(prefix="wisp-signing-") as tmp:
        private = Path(tmp)
        private.chmod(0o700)
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
            developer_sign(runner, bundle, identity, keychain)
            validate_structure(bundle, meta)
            validate_native(runner, bundle, meta)
            relocation_smoke(runner, bundle)
            submission = private / "notarization.zip"
            archive(bundle, submission, meta["source_epoch"])
            # API key content and private args never appear in command/output logs.
            try:
                result = subprocess.run(["xcrun", "notarytool", "submit", str(submission), "--key", str(api_key),
                    "--key-id", os.environ["WISP_APPLE_KEY_ID"], "--issuer", os.environ["WISP_APPLE_ISSUER_ID"],
                    "--wait", "--timeout", "30m", "--output-format", "json"], capture_output=True, text=True, timeout=1900)
            except (subprocess.SubprocessError, OSError):
                raise BuildError("Notarization did not complete; inspect Apple's submission history before retrying") from None
            try:
                receipt = json.loads(result.stdout)
            except ValueError:
                raise BuildError("Notarization returned no valid receipt; no publication performed") from None
            safe_receipt = {key: receipt.get(key) for key in ("id", "status", "message")}
            json_write(destination / "notarization.json", safe_receipt)
            if result.returncode or receipt.get("status") != "Accepted":
                raise BuildError(f"Notarization was not accepted; submission {receipt.get('id')}. See recovery documentation.")
            runner.run("staple-ticket", ["xcrun", "stapler", "staple", bundle])
            runner.run("validate-ticket", ["xcrun", "stapler", "validate", bundle])
            runner.run("gatekeeper", ["spctl", "--assess", "--type", "execute", "--verbose=2", bundle])
            runner.run("verify-stapled-signature", ["codesign", "--verify", "--deep", "--strict", bundle])
        finally:
            if created:
                secret_run(["security", "delete-keychain", keychain])
    json_write(destination / "bundle-manifest.json", inventory(bundle))
    provenance.update(signature="Developer ID Application", notarized=True,
                      notarization=safe_receipt, candidate_sha256=candidate_checksum)
    json_write(destination / "provenance.json", provenance)
    zip_path = destination / f"Wisp-{meta['version']}-{meta['build_number']}-arm64.zip"
    # Preserve the stapled ticket and any required extended attributes.
    runner.run("archive-notarized-app", ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", bundle, zip_path])
    with BoundReleaseAssets(destination) as assets:
        archive_descriptor = assets.descriptors[zip_path.name]
        distribution_roundtrip(runner, zip_path, bundle, meta, notarized=True,
                               archive_descriptor=archive_descriptor)
        assets.write_checksums(exclude={"release-notes.md"})
        verify_artifacts(destination, bound_assets=assets, publication_asset_set=True)
        # Create as draft first. Notes have their own retained reader and are not
        # also uploaded, so their shared open-file offset cannot affect an asset.
        notes_descriptor = assets.descriptors["release-notes.md"]
        os.lseek(notes_descriptor, 0, os.SEEK_SET)
        notes = f"/dev/fd/{notes_descriptor}"
        runner.run("create-draft-release", ["gh", "release", "create", tag, "--verify-tag", "--draft",
            "--title", f"Wisp {meta['version']}", "--notes-file", notes],
            env=env, pass_fds=(notes_descriptor,))
        _, release_log = runner.run("resolve-draft-release", ["gh", "api",
            f"repos/{env['GH_REPO']}/releases/tags/{tag}", "--jq", ".id"], env=env)
        release_id = release_log.read_text().strip()
        uploader = GitHubReleaseUploader(env["GH_REPO"], release_id, env["GH_TOKEN"])
        for name, content_type, descriptor in assets.upload_assets():
            uploader.upload(name, content_type, descriptor)
    runner.run("publish-release", ["gh", "release", "edit", tag, "--draft=false"], env=env)
    print(f"Published verified release {tag}; signed artifacts: {destination}")
