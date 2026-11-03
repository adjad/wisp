"""In-app updates, release side (build-support/sparkle.py, update_release.py).

Pure checks: Info.plist keys, the independent Ed25519 verifier (RFC 8032 test
vector, never a real key), appcast items and merging, channel and label rules,
and the fail-closed paths. No network, keychain, signing tool or GitHub.
"""
import base64
import copy
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build-support"))
import pipeline  # noqa: E402

sys.modules.setdefault("pipeline", pipeline)
import sparkle  # noqa: E402
import update_release  # noqa: E402

# RFC 8032 section 7.1, test 1 (empty message).
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
SIGNATURE = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555"
                          "fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
B64 = lambda data: base64.b64encode(data).decode()  # noqa: E731


def config(**updates):
    result = copy.deepcopy(pipeline.CONFIG)
    result["updates"].update(updates)
    return result


def test_ed25519_matches_rfc_8032(tmp_path):
    assert sparkle.public_key_from_seed(SEED) == PUBLIC
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert sparkle.verify_file(B64(PUBLIC), B64(SIGNATURE), empty)
    tampered = tmp_path / "tampered"
    tampered.write_bytes(b"x")
    assert not sparkle.verify_file(B64(PUBLIC), B64(SIGNATURE), tampered)
    other = bytearray(SIGNATURE)
    other[40] ^= 1
    assert not sparkle.verify_file(B64(PUBLIC), B64(bytes(other)), empty)
    assert not sparkle.verify_file("not base64!", B64(SIGNATURE), empty)
    assert not sparkle.verify_file(B64(PUBLIC[:31]), B64(SIGNATURE), empty)


def test_info_plist_is_private_by_default():
    keys = sparkle.info_plist(config(public_ed_key=""))
    assert keys["SUEnableAutomaticChecks"] is False and keys["SUEnableSystemProfiling"] is False
    assert keys["SUAutomaticallyUpdate"] is False
    assert keys["SURequireSignedFeed"] is True and keys["SUVerifyUpdateBeforeExtraction"] is True
    # No key, no feed: the app says updates aren't set up instead of fetching anything.
    assert "SUFeedURL" not in keys and "SUPublicEDKey" not in keys
    keys = sparkle.info_plist(config(public_ed_key=B64(PUBLIC)))
    assert keys["SUPublicEDKey"] == B64(PUBLIC)
    assert keys["SUFeedURL"].startswith("https://raw.githubusercontent.com/")
    assert pipeline.plist({"version": "0.5.0", "build_number": "1", "commit": "a" * 40,
                           "dirty": False})["SUEnableSystemProfiling"] is False


@pytest.mark.parametrize("bad", [
    {"feed_url": "http://example.com/appcast.xml"}, {"channel": "nightly"}, {"signing": "developer-id"},
    {"public_ed_key": B64(b"short")},
])
def test_settings_refuse_unsafe_values(bad):
    with pytest.raises(sparkle.SparkleError):
        sparkle.settings(config(**bad))


def test_pinned_sparkle_matches_the_package_manifest():
    updates = pipeline.CONFIG["updates"]
    manifest = (ROOT / "app/Package.swift").read_text()
    assert f'exact: "{updates["sparkle_version"]}"' in manifest
    resolved = json.loads((ROOT / "app/Package.resolved").read_text())
    pins = {pin["identity"]: pin["state"] for pin in resolved["pins"]}
    assert pins == {"sparkle": {"revision": pins["sparkle"]["revision"], "version": updates["sparkle_version"]}}


def make_item(build="3400", channel="beta", label="0.5.0-rc1-beta2"):
    return sparkle.item(label=label, build=build, channel=channel,
                        url=f"https://github.com/adjad/wisp/releases/download/v{label}/01-Wisp-{label}-{build}-arm64.zip",
                        signature=B64(SIGNATURE), length=0, minimum_macos="14.0",
                        notes_markdown="Notes", published="Mon, 02 Nov 2026 00:00:00 +0000")


def test_appcast_items_newest_first_and_never_replaced():
    feed = sparkle.merge(sparkle.empty_feed(), make_item("3400"))
    feed = sparkle.merge(feed, make_item("3401", "stable", "0.5.0"))
    root = ET.fromstring(feed)
    items = root.findall("channel/item")
    version = "{%s}version" % sparkle.SPARKLE_NS
    channel = "{%s}channel" % sparkle.SPARKLE_NS
    assert [node.findtext(version) for node in items] == ["3401", "3400"]
    assert items[0].find(channel) is None and items[1].findtext(channel) == "beta"
    enclosure = items[1].find("enclosure")
    assert enclosure.get("{%s}edSignature" % sparkle.SPARKLE_NS) == B64(SIGNATURE)
    assert items[1].findtext("{%s}hardwareRequirements" % sparkle.SPARKLE_NS) == "arm64"
    with pytest.raises(sparkle.SparkleError, match="already has build 3400"):
        sparkle.merge(feed, make_item("3400"))


def test_merge_drops_the_old_feed_signature():
    signed = sparkle.empty_feed() + b"<!-- sparkle-signatures:\nedSignature: abc\nlength: 1\n-->\n"
    merged = sparkle.merge(signed, make_item())
    assert b"sparkle-signatures" not in merged and b"<item>" in merged


def test_check_feed_needs_the_apps_key(tmp_path):
    archive = tmp_path / "empty.zip"
    archive.write_bytes(b"")
    feed = sparkle.merge(sparkle.empty_feed(), make_item())
    sparkle.check_feed(feed, public_key=B64(PUBLIC), archive=archive, build="3400")
    other = B64(sparkle.public_key_from_seed(bytes(32)))
    with pytest.raises(sparkle.SparkleError, match="does not verify"):
        sparkle.check_feed(feed, public_key=other, archive=archive, build="3400")
    with pytest.raises(sparkle.SparkleError, match="no item"):
        sparkle.check_feed(feed, public_key=B64(PUBLIC), archive=archive, build="9999")


@pytest.mark.parametrize("kwargs", [
    {"url": "http://github.com/x.zip"}, {"channel": "nightly"}, {"label": "beta2"}, {"build": "3400a"},
])
def test_item_refuses_bad_input(kwargs):
    base = dict(label="0.5.0-rc1-beta2", build="3400", channel="beta", url="https://github.com/x.zip",
                signature=B64(SIGNATURE), length=0, minimum_macos="14.0", notes_markdown="", published="x")
    base.update(kwargs)
    with pytest.raises(sparkle.SparkleError):
        sparkle.item(**base)


def test_labels_and_channels():
    meta = {"version": "0.5.0"}
    assert update_release.check_label("0.5.0-rc1-beta2", meta) == "0.5.0-rc1-beta2"
    for bad in (None, "", "0.6.0-rc1", "v0.5.0", "0.5.0-Beta", "0.5.0 beta"):
        with pytest.raises(pipeline.BuildError):
            update_release.check_label(bad, meta)
    assert update_release.channel_for("0.5.0-rc1-beta2", None) == "beta"
    assert update_release.channel_for(pipeline.CONFIG["version"], None) == "stable"
    assert update_release.channel_for("0.5.0-rc1-beta2", "stable") == "stable"
    assert update_release.public_name("0.5.0-rc1-beta2", "3400") == "01-Wisp-0.5.0-rc1-beta2-3400-arm64.zip"


def test_ci_without_the_private_key_publishes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv(sparkle.PRIVATE_KEY_ENV, raising=False)
    with pytest.raises(sparkle.SparkleError, match="refusing to publish an unsigned update"):
        sparkle.require_key_source(None, ci=True)
    sparkle.require_key_source(None, ci=False)          # local: the login keychain
    monkeypatch.setenv(sparkle.PRIVATE_KEY_ENV, "  ")
    with pytest.raises(sparkle.SparkleError):
        sparkle.require_key_source(None, ci=True)

    class Args:
        allow_dirty = test_python = offline = False
        output, label, channel = tmp_path, "0.5.0-rc1-beta2", None
    env = {"GITHUB_ACTIONS": "true", "WISP_UPDATE_RELEASE_APPROVED": "true",
           "GITHUB_EVENT_NAME": "workflow_dispatch", "GH_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(pipeline.BuildError, match="SPARKLE_ED_PRIVATE_KEY is not set"):
        update_release.release_update(None, Args())
    monkeypatch.setenv("WISP_UPDATE_RELEASE_APPROVED", "false")
    with pytest.raises(pipeline.BuildError, match="explicit CI workflow dispatch"):
        update_release.release_update(None, Args())


def test_self_signed_switch_needs_its_secrets(monkeypatch, tmp_path):
    for name in update_release.SELF_SIGNED_SECRETS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(pipeline.BuildError, match="CI secrets are missing"):
        with update_release.self_signing_keychain(tmp_path):
            pass


def test_existing_release_must_hold_exactly_the_signed_archive():
    good = {"draft": False, "assets": [{"name": "a.zip", "size": 3, "digest": "sha256:" + "0" * 64}]}
    assert update_release._existing_release_matches(good, "a.zip", "0" * 64, 3)
    assert not update_release._existing_release_matches(dict(good, draft=True), "a.zip", "0" * 64, 3)
    assert not update_release._existing_release_matches(good, "a.zip", "1" * 64, 3)
    two = dict(good, assets=good["assets"] * 2)
    assert not update_release._existing_release_matches(two, "a.zip", "0" * 64, 3)


def test_linkage_check():
    sparkle.check_linkage("cmd LC_RPATH\n path @executable_path/../Frameworks (offset 12)",
                          "Wisp:\n\t@rpath/Sparkle.framework/Versions/B/Sparkle (compatibility version 1.6.0)")
    with pytest.raises(sparkle.SparkleError):
        sparkle.check_linkage("path @loader_path (offset 12)",
                              "\t@rpath/Sparkle.framework/Versions/B/Sparkle (compatibility version 1.6.0)")


def test_absolute_rpaths_go_only_when_frameworks_resolve_inside(tmp_path):
    bundle = tmp_path / "Wisp.app"
    main = bundle / "Contents/MacOS/Wisp"
    main.parent.mkdir(parents=True)
    main.write_bytes(b"")
    dep = "@rpath/Sparkle.framework/Versions/B/Sparkle"
    rpaths = ["/usr/lib/swift", "/Applications/Xcode.app/x", "@executable_path/../Frameworks"]
    assert not pipeline.resolves_in_bundle(bundle, main, dep, rpaths)       # not bundled yet
    framework = bundle / "Contents/Frameworks/Sparkle.framework/Versions/B/Sparkle"
    framework.parent.mkdir(parents=True)
    framework.write_bytes(b"")
    assert pipeline.resolves_in_bundle(bundle, main, dep, rpaths)
    assert not pipeline.resolves_in_bundle(bundle, main, dep, rpaths[:2])   # only absolute paths
    assert not pipeline.resolves_in_bundle(bundle, main, "/opt/lib/x.dylib", rpaths)
    assert not pipeline.resolves_in_bundle(bundle, main, "@rpath/../../../../etc/hosts", rpaths)
