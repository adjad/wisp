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

from pipeline import (BuildError, CONFIG, ROOT, SUPPORT, archive, checksums, digest,
                      git, inventory, is_macho, json_write, relocation_smoke, distribution_roundtrip,
                      validate_native, validate_structure, verify_artifacts)

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


def release(runner, args):
    # All validation and credential checks precede any signing/upload/publication.
    preflight(args)
    candidate = args.output.resolve()
    if not candidate.is_relative_to((ROOT / "dist").resolve()):
        raise BuildError("Release input must be beneath this checkout's dist/")
    verify_artifacts(candidate)
    provenance = json.loads((candidate / "provenance.json").read_text())
    meta = provenance["source"]
    if meta["dirty"] or meta["commit"] != git("rev-parse", "HEAD") or git("status", "--porcelain"):
        raise BuildError("Release candidate must match this clean checkout exactly")
    if not provenance["toolchain"]["strict_toolchain"]:
        raise BuildError("Candidate must pass --strict-toolchain before signing")
    if any(row["exit_code"] for row in provenance["tests"]):
        raise BuildError("Release candidate contains failed validation steps")
    runner.run("tag-on-main", ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"])
    if git("rev-parse", f"refs/tags/v{CONFIG['version']}^{{commit}}") != meta["commit"]:
        raise BuildError("Tag does not point to candidate source")
    env = dict(runner.env, GH_TOKEN=os.environ["GH_TOKEN"], GH_REPO=os.environ["GITHUB_REPOSITORY"])
    tag = "v" + CONFIG["version"]
    # Refuse to overwrite an existing release, including a previous draft.
    existing = subprocess.run(["gh", "api", f"repos/{env['GH_REPO']}/releases/tags/{tag}"],
                              env=env, capture_output=True, text=True, timeout=60)
    if existing.returncode == 0:
        raise BuildError("Release already exists; recover the existing draft manually after review")
    if "404" not in existing.stderr:
        raise BuildError("Could not safely establish that the GitHub release is absent")
    destination = candidate.parent / (candidate.name + "-signed")
    destination.mkdir(exist_ok=False)
    bundle = destination / "Wisp.app"
    shutil.copytree(candidate / "Wisp.app", bundle, symlinks=True)
    for name in ("dependencies.json", "release-notes.md"):
        shutil.copyfile(candidate / name, destination / name)
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
            for path in (p for p in bundle.rglob("*") if is_macho(p)):
                secret_run(["codesign", "--force", "--sign", identity, "--keychain", keychain, "--timestamp", "--options", "runtime", "--entitlements", SUPPORT / "python.entitlements", path])
            secret_run(["codesign", "--force", "--sign", identity, "--keychain", keychain, "--timestamp", "--options", "runtime", "--entitlements", SUPPORT / "app.entitlements", bundle])
            runner.run("verify-developer-signature", ["codesign", "--verify", "--deep", "--strict", "--verbose=2", bundle])
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
                      notarization=safe_receipt, candidate_sha256=digest(candidate / "SHA256SUMS"))
    json_write(destination / "provenance.json", provenance)
    zip_path = destination / f"Wisp-{meta['version']}-{meta['build_number']}-arm64.zip"
    # Preserve the stapled ticket and any required extended attributes.
    runner.run("archive-notarized-app", ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", bundle, zip_path])
    distribution_roundtrip(runner, zip_path, bundle, meta, notarized=True)
    checksums(destination)
    verify_artifacts(destination)
    files = sorted(p for p in destination.iterdir() if p.is_file())
    # Create as draft first: an upload failure must not expose an incomplete release.
    runner.run("create-draft-release", ["gh", "release", "create", tag, "--verify-tag", "--draft",
        "--title", f"Wisp {meta['version']}", "--notes-file", destination / "release-notes.md", *files], env=env)
    runner.run("publish-release", ["gh", "release", "edit", tag, "--draft=false"], env=env)
    print(f"Published verified release {tag}; signed artifacts: {destination}")
