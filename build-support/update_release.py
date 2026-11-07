"""Publish a verified build as an in-app update (Sparkle): `appcast` and `release-update`.

`appcast` (local, no publication): from a verified candidate directory, writes
the public ZIP and a signed appcast with this build's item added, for a person
publishing by hand. It signs with the login keychain's EdDSA key (Sparkle's
generate_keys put it there), or --ed-key-file.

`release-update` (CI only, explicit workflow dispatch): the same, then creates
the GitHub release v<label> at the candidate's commit with the ZIP as its only
asset, and only after the release is public adds the item to appcast.xml on the
repository's `appcast` branch. It fails closed: without SPARKLE_ED_PRIVATE_KEY
nothing is created, and nothing is published unless the archive's signature
verifies against the public key inside the app being published.

With toolchain.json updates.signing = "self-signed", the outer app is re-signed
with the stable self-signed identity first (docs/updates.md: macOS keeps
privacy grants across updates only when the designated requirement stays the
same). The certificate's SHA-1 must equal updates.self_signed_leaf_sha1.
"""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote

import sparkle
from pipeline import (BoundReleaseAssets, BuildError, CONFIG, GitHubReleaseUploader, ROOT, archive, clean_env,
                      digest, distribution_roundtrip, git, verify_artifacts)
from release import FIRST_LAUNCH_NOTE, github_release_for_tag, created_draft_release_id, require_release_features, secret_run

sys.path.insert(0, str(ROOT / "scripts"))
import resign_local_install as resign  # noqa: E402  (tree digests and requirement parsing)

BRANCH = "appcast"
FEED_PATH = "appcast.xml"
SELF_SIGNED_SECRETS = ("WISP_UPDATE_SIGNING_P12_BASE64", "WISP_UPDATE_SIGNING_P12_PASSWORD")


def _settings():
    try:
        return sparkle.settings(CONFIG)
    except sparkle.SparkleError as exc:
        raise BuildError(str(exc)) from None


def public_name(label: str, build: str) -> str:
    return f"01-Wisp-{label}-{build}-arm64.zip"


def check_label(label: str | None, meta: dict) -> str:
    """The public name of this build: CONFIG version, optionally with a suffix."""
    if not label or not sparkle.LABEL.fullmatch(label):
        raise BuildError("--label must look like 0.5.0-rc1-beta2")
    if label != meta["version"] and not label.startswith(meta["version"] + "-"):
        raise BuildError(f"--label must start with this build's version {meta['version']}")
    return label


def channel_for(label: str, requested: str | None) -> str:
    """Pre-release labels go to the beta channel; a plain x.y.z is stable."""
    channel = requested or (sparkle.BETA if label != CONFIG["version"] else "stable")
    if channel not in sparkle.CHANNELS:
        raise BuildError("--channel must be stable or beta")
    return channel


def release_page_notes(label: str) -> str | None:
    page = ROOT / "docs/releases" / f"{label}.md"
    return page.read_text(encoding="utf-8") if page.is_file() else None


def _bundle_public_key(bundle: Path) -> str:
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    key = info.get("SUPublicEDKey")
    if not key or info.get("SUFeedURL") != _settings()["feed_url"]:
        raise BuildError("This build has no update feed or EdDSA public key; set updates.public_ed_key "
                         "in toolchain.json (docs/updates.md) and rebuild")
    return key


def _sign_update():
    products = ROOT / ".wisp-build/swift/release"   # scratch() searches upward from here
    try:
        return sparkle.tool(products, "sign_update")
    except sparkle.SparkleError as exc:
        raise BuildError(str(exc)) from None


def self_sign(runner, bundle: Path, keychain: Path, leaf_sha1: str) -> None:
    """Re-sign the outer app only, like scripts/resign_local_install.py, and prove the
    designated requirement is the stable one and nothing nested changed."""
    before = resign.tree_digest(bundle)
    runner.run("self-sign-app", ["/usr/bin/codesign", "--force", "--sign", leaf_sha1.upper(), "--keychain", keychain,
                                 "--timestamp=none", "--preserve-metadata=identifier,entitlements,flags,runtime", bundle])
    _, log = runner.run("self-signed-requirement", ["/usr/bin/codesign", "-d", "-r-", bundle])
    if not resign.requirement_matches(resign.parse_requirement(log.read_text()), leaf_sha1.lower()):
        raise BuildError("Self-signed app does not carry the pinned designated requirement")
    runner.run("self-signed-verify", ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", bundle])
    if resign.tree_digest(bundle) != before:
        raise BuildError("Self-signing changed more than the outer app's signature")


@contextlib.contextmanager
def self_signing_keychain(private: Path):
    """An ephemeral keychain holding the self-signed identity from CI secrets. The
    default keychain and search list are never changed."""
    missing = [name for name in SELF_SIGNED_SECRETS if not os.environ.get(name)]
    if missing:
        raise BuildError("updates.signing is self-signed but CI secrets are missing: " + ", ".join(missing))
    keychain = private / "update-signing.keychain-db"
    p12 = private / "update-identity.p12"
    password = secrets.token_urlsafe(32)
    created = False
    try:
        p12.write_bytes(base64.b64decode(os.environ["WISP_UPDATE_SIGNING_P12_BASE64"], validate=True))
        p12.chmod(0o600)
        secret_run(["security", "create-keychain", "-p", password, keychain])
        created = True
        secret_run(["security", "set-keychain-settings", "-lut", "3600", keychain])
        secret_run(["security", "unlock-keychain", "-p", password, keychain])
        secret_run(["security", "import", p12, "-k", keychain, "-P", os.environ["WISP_UPDATE_SIGNING_P12_PASSWORD"],
                    "-T", "/usr/bin/codesign"])
        secret_run(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s",
                    "-k", password, keychain])
        p12.unlink()
        yield keychain
    finally:
        p12.unlink(missing_ok=True)
        if created:
            secret_run(["security", "delete-keychain", keychain])


def prepare(runner, candidate: Path, private: Path, *, label: str, channel: str, repository: str,
            feed: bytes, key_file: Path | None, ci: bool, keychain_factory=None) -> dict:
    """Verify the candidate, (optionally) self-sign, sign the ZIP, and return the new
    signed feed. Writes only below `private`."""
    settings = _settings()
    if ci:
        if key_file is not None:
            raise BuildError("CI signs only with SPARKLE_ED_PRIVATE_KEY")
        try:
            sparkle.require_key_source(None, ci=True)
        except sparkle.SparkleError as exc:
            raise BuildError(str(exc)) from None
    with BoundReleaseAssets(candidate) as assets:
        verify_artifacts(candidate, bound_assets=assets)
        provenance = json.loads(assets.read_text("provenance.json"))
        meta = provenance["source"]
        if provenance.get("preview") or meta.get("dirty") or meta.get("preview"):
            raise BuildError("A preview build is never published as an update")
        if provenance.get("signature") != "ad-hoc" or provenance.get("notarized") is not False:
            raise BuildError("Update publication starts from a verified ad-hoc candidate")
        if any(row["exit_code"] and not row.get("optional") for row in provenance.get("tests", [])):
            raise BuildError("Candidate contains failed validation steps")
        check_label(label, meta)
        archives = [name for name in assets.descriptors if name.endswith(".zip")]
        if len(archives) != 1:
            raise BuildError("Update publication requires exactly one verified ZIP")
        bundle = private / "Wisp.app"
        zip_path = private / public_name(label, meta["build_number"])
        # Bound copy: the private archive is byte-identical to the verified one.
        with os.fdopen(os.dup(assets.descriptors[archives[0]]), "rb") as source, zip_path.open("xb") as target:
            source.seek(0)
            shutil.copyfileobj(source, target, 1024 * 1024)
        if digest(zip_path) != assets.digests[archives[0]]:
            raise BuildError("Private archive copy differs from the verified candidate")
        assets.assert_paths_unchanged()
    runner.run("extract-update-app", ["ditto", "-x", "-k", zip_path, private])
    public_key = _bundle_public_key(bundle)
    signing = "ad-hoc"
    if settings["signing"] == "self-signed":
        leaf = settings.get("self_signed_leaf_sha1", "")
        if not re.fullmatch(r"[0-9A-Fa-f]{40}", leaf or ""):
            raise BuildError("updates.self_signed_leaf_sha1 must be the identity's 40-hex SHA-1")
        with (keychain_factory or self_signing_keychain)(private) as keychain:
            self_sign(runner, bundle, keychain, leaf)
        zip_path.unlink()
        archive(bundle, zip_path, meta["source_epoch"])
        signing = "self-signed"
    sign_update = _sign_update()
    try:
        signature, length = sparkle.sign_archive(sign_update, zip_path, key_file, clean_env())
        if not sparkle.verify_file(public_key, signature, zip_path):
            raise BuildError("The update's EdDSA signature does not verify against the app's SUPublicEDKey; "
                             "the private key does not belong to updates.public_ed_key")
        tag = "v" + label
        url = (f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/"
               f"{quote(zip_path.name, safe='')}")
        node = sparkle.item(
            label=label, build=meta["build_number"], channel=channel, url=url, signature=signature, length=length,
            minimum_macos=meta["minimum_macos"],
            notes_markdown=sparkle.notes_markdown(label, meta["build_number"],
                                                  f"https://github.com/{repository}/releases/tag/{quote(tag, safe='')}",
                                                  release_page_notes(label)),
            published=datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"))
        feed_path = private / FEED_PATH
        feed_path.write_bytes(sparkle.merge(feed or sparkle.empty_feed(), node))
        sparkle.sign_feed(sign_update, feed_path, key_file, clean_env())
        signed_feed = feed_path.read_bytes()
        sparkle.check_feed(signed_feed, public_key=public_key, archive=zip_path, build=meta["build_number"])
    except sparkle.SparkleError as exc:
        raise BuildError(str(exc)) from None
    return {"meta": meta, "provenance": provenance, "zip": zip_path, "feed": signed_feed, "tag": tag,
            "channel": channel, "signing": signing, "sha256": digest(zip_path)}


# --- GitHub ---------------------------------------------------------------------

def _gh(env, *args, payload=None, check=True):
    result = subprocess.run(["gh", "api", *args, *(["--input", "-"] if payload is not None else [])],
                            input=json.dumps(payload) if payload is not None else None,
                            env=env, capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise BuildError(f"GitHub API call failed: {args[0] if args else ''}")
    return result


def read_feed(env, repository: str) -> tuple[bytes, str | None]:
    """The current appcast.xml and its blob sha, or (b"", None) on first publication."""
    result = _gh(env, f"repos/{repository}/contents/{FEED_PATH}?ref={BRANCH}", check=False)
    if result.returncode:
        branch = _gh(env, f"repos/{repository}/branches/{BRANCH}", check=False)
        if branch.returncode == 0:
            raise BuildError("The appcast branch exists but appcast.xml could not be read")
        return b"", None
    data = json.loads(result.stdout)
    return base64.b64decode(data["content"]), data["sha"]


def write_feed(env, repository: str, feed: bytes, previous_sha: str | None, message: str) -> None:
    """One commit on the appcast branch. An existing feed is replaced only if it is
    still the version read (GitHub refuses a stale sha)."""
    content = base64.b64encode(feed).decode()
    if previous_sha is not None:
        _gh(env, "-X", "PUT", f"repos/{repository}/contents/{FEED_PATH}",
            payload={"message": message, "content": content, "sha": previous_sha, "branch": BRANCH})
        return
    # First publication: an orphan branch holding only appcast.xml.
    blob = json.loads(_gh(env, "-X", "POST", f"repos/{repository}/git/blobs",
                          payload={"content": content, "encoding": "base64"}).stdout)["sha"]
    tree = json.loads(_gh(env, "-X", "POST", f"repos/{repository}/git/trees",
                          payload={"tree": [{"path": FEED_PATH, "mode": "100644", "type": "blob",
                                             "sha": blob}]}).stdout)["sha"]
    commit = json.loads(_gh(env, "-X", "POST", f"repos/{repository}/git/commits",
                            payload={"message": message, "tree": tree, "parents": []}).stdout)["sha"]
    _gh(env, "-X", "POST", f"repos/{repository}/git/refs", payload={"ref": f"refs/heads/{BRANCH}", "sha": commit})


def _existing_release_matches(release: dict, name: str, sha256: str, size: int) -> bool:
    assets = release.get("assets") or []
    return (release.get("draft") is False and len(assets) == 1 and assets[0].get("name") == name
            and assets[0].get("size") == size
            # GitHub may not report a digest yet; a different one always refuses.
            and assets[0].get("digest") in (None, "sha256:" + sha256))


def release_update(runner, args):
    """CI: publish the verified candidate as release v<label> and add it to the appcast."""
    env_in = os.environ
    if args.allow_dirty or args.test_python or args.offline:
        raise BuildError("Update release does not accept preview, audit-interpreter or offline flags")
    if (env_in.get("GITHUB_ACTIONS") != "true" or env_in.get("WISP_UPDATE_RELEASE_APPROVED") != "true"
            or env_in.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise BuildError("Update release requires explicit CI workflow dispatch")
    if not args.output or not env_in.get("GH_TOKEN") or not env_in.get("GITHUB_REPOSITORY"):
        raise BuildError("Update release requires candidate output and GitHub credentials")
    try:
        sparkle.require_key_source(None, ci=True)
    except sparkle.SparkleError as exc:
        raise BuildError(str(exc)) from None
    candidate = args.output.resolve()
    if not candidate.is_relative_to((ROOT / "dist").resolve()):
        raise BuildError("Release input must be beneath this checkout's dist/")
    repository = env_in["GITHUB_REPOSITORY"]
    env = dict(clean_env(), GH_TOKEN=env_in["GH_TOKEN"], GH_REPO=repository)
    with tempfile.TemporaryDirectory(prefix=".wisp-update-", dir=candidate.parent) as tmp:
        private = Path(tmp)
        private.chmod(0o700)
        feed, feed_sha = read_feed(env, repository)
        label = args.label
        result = prepare(runner, candidate, private, label=label, channel=channel_for(label or "", args.channel),
                         repository=repository, feed=feed, key_file=None, ci=True)
        meta = result["meta"]
        require_release_features(meta)
        if (meta["commit"] != git("rev-parse", "HEAD") or git("status", "--porcelain")
                or not result["provenance"].get("toolchain", {}).get("strict_toolchain")):
            raise BuildError("Update candidate must match this clean checkout, built with --strict-toolchain")
        runner.run("update-on-main", ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"])
        tag, zip_path = result["tag"], result["zip"]
        size = zip_path.stat().st_size
        existing = github_release_for_tag(env, tag)
        if existing and not _existing_release_matches(existing, zip_path.name, result["sha256"], size):
            raise BuildError(f"Release {tag} already exists with other assets; refusing to modify it")
        if not existing:
            download = f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/{quote(zip_path.name, safe='')}"
            notes = release_page_notes(label) or f"Wisp {label} (build {meta['build_number']})\n"
            body = (f"**[Download Wisp for Mac (Apple silicon)]({download})**\n\n" + FIRST_LAUNCH_NOTE
                    + "Already have a Wisp beta with in-app updates? Choose **Check for Updates…** in Wisp's "
                      "menu bar menu instead.\n\n" + notes)
            notes_file = private / "release-body.md"
            notes_file.write_text(body)
            runner.run("create-update-draft", ["gh", "release", "create", tag, "--target", meta["commit"], "--draft",
                                               *(["--prerelease"] if result["channel"] == sparkle.BETA else []),
                                               "--title", f"Wisp {label}", "--notes-file", notes_file], env=env)
            release_id = created_draft_release_id(env, tag)
            with zip_path.open("rb") as handle:
                GitHubReleaseUploader(repository, release_id, env["GH_TOKEN"]).upload(
                    zip_path.name, "application/zip", handle.fileno(), result["sha256"], size)
            runner.run("publish-update-release", ["gh", "release", "edit", tag, "--draft=false"], env=env)
            published = github_release_for_tag(env, tag)
            if not published or not _existing_release_matches(published, zip_path.name, result["sha256"], size):
                raise BuildError("Published release does not carry exactly the signed archive; appcast left unchanged")
        # Only now, with the download public, does the appcast point at it.
        write_feed(env, repository, result["feed"], feed_sha,
                   f"Wisp {label} (build {meta['build_number']}, {result['channel']})")
        current, _ = read_feed(env, repository)
        if current != result["feed"]:
            raise BuildError("The appcast branch does not hold the feed just signed")
    print(f"Published {tag} ({result['channel']}, {result['signing']}) and updated {BRANCH}/{FEED_PATH}")


def appcast(runner, args):
    """Local: write the public ZIP and the signed appcast to --update-output for manual publication."""
    if not args.output or not args.update_output:
        raise BuildError("appcast requires --output (verified candidate) and --update-output")
    destination = args.update_output.resolve()
    if destination.exists():
        raise BuildError("--update-output must not exist yet")
    repository = args.repository or "adjad/wisp"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise BuildError("--repository must be owner/name")
    feed = args.feed.read_bytes() if args.feed else b""
    destination.mkdir(parents=True)
    destination.chmod(0o700)
    label = args.label
    result = prepare(runner, args.output.resolve(), destination, label=label,
                     channel=channel_for(label or "", args.channel), repository=repository, feed=feed,
                     key_file=args.ed_key_file, ci=False)
    shutil.rmtree(destination / "Wisp.app")
    print(json.dumps({"archive": str(result["zip"]), "sha256": result["sha256"],
                      "appcast": str(destination / FEED_PATH), "tag": result["tag"],
                      "channel": result["channel"], "signing": result["signing"]}, indent=2))
    print(f"Publish {result['zip'].name} as the only asset of release {result['tag']} first, "
          f"then commit {FEED_PATH} to the {BRANCH} branch (docs/updates.md).")
