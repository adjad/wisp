"""Sparkle 2 in-app updates: the bundled framework, the Info.plist keys, and the
signed appcast.

Wisp is not sandboxed, so Sparkle installs updates with its own Autoupdate tool
and Updater.app; the framework's Installer and Downloader XPC services exist only
for sandboxed apps (SUEnableInstallerLauncherService/SUEnableDownloaderService
stay unset) and are removed, together with the headers and module maps, so the
bundle ships no unused code.

The appcast lives on the repository's `appcast` branch and is served from
raw.githubusercontent.com (docs/updates.md says why). Every item's archive is
EdDSA-signed, the feed itself is signed, and before anything is published the
archive's signature is verified here, independently of Sparkle's tools, against
the public key compiled into the app being published.

No private key is created, printed, logged or written here. It comes from the
login keychain (sign_update's default) or, in CI, from SPARKLE_ED_PRIVATE_KEY on
sign_update's standard input; without one, nothing is signed and nothing is
published.
"""
from __future__ import annotations

import base64
import hashlib
import html
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

FRAMEWORK = "Contents/Frameworks/Sparkle.framework"
# Present only for sandboxed apps or for building against the framework.
REMOVED = ("XPCServices", "Headers", "PrivateHeaders", "Modules")
REQUIRED = ("Versions/Current/Sparkle", "Versions/Current/Autoupdate",
            "Versions/Current/Updater.app/Contents/MacOS/Updater", "Versions/Current/Resources/Info.plist")
PRIVATE_KEY_ENV = "SPARKLE_ED_PRIVATE_KEY"
SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
DC_NS = "http://purl.org/dc/elements/1.1/"
BETA = "beta"
CHANNELS = ("stable", BETA)
# 0.5.0-rc1-beta2, 0.5.0-dev9, 0.5.0: the public name of one build.
LABEL = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?")


class SparkleError(RuntimeError):
    pass


def settings(config: dict) -> dict:
    updates = config.get("updates")
    if not isinstance(updates, dict):
        raise SparkleError("toolchain.json has no updates section")
    for key in ("sparkle_version", "feed_url", "public_ed_key", "channel", "signing"):
        if not isinstance(updates.get(key), str):
            raise SparkleError(f"toolchain.json updates.{key} must be a string")
    if not updates["feed_url"].startswith("https://"):
        raise SparkleError("The update feed must be https")
    if updates["channel"] not in CHANNELS:
        raise SparkleError("updates.channel must be stable or beta")
    if updates["signing"] not in ("ad-hoc", "self-signed"):
        raise SparkleError("updates.signing must be ad-hoc or self-signed")
    if updates["public_ed_key"] and not _is_public_key(updates["public_ed_key"]):
        raise SparkleError("updates.public_ed_key must be a base64 32-byte EdDSA public key")
    return updates


def _is_public_key(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except ValueError:
        return False


def info_plist(config: dict) -> dict:
    """Info.plist keys. No network contact unless the person asks; no profile, ever.
    A build without a public key has no feed key and the app says updates aren't set up."""
    updates = settings(config)
    keys = {
        "SUEnableAutomaticChecks": False, "SUAutomaticallyUpdate": False,
        "SUEnableSystemProfiling": False, "SURequireSignedFeed": True,
        "SUVerifyUpdateBeforeExtraction": True, "WispUpdateChannel": updates["channel"],
    }
    if updates["public_ed_key"]:
        keys.update(SUFeedURL=updates["feed_url"], SUPublicEDKey=updates["public_ed_key"])
    return keys


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def stage(bundle: Path, products: Path, config: dict) -> dict:
    """Copy SwiftPM's Sparkle.framework into Contents/Frameworks, minus REMOVED."""
    updates = settings(config)
    source = products / "Sparkle.framework"
    if source.is_symlink() or not source.is_dir():
        raise SparkleError("SwiftPM did not produce Sparkle.framework")
    target = bundle / FRAMEWORK
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True)
    for name in REMOVED:
        _remove(target / name)
        for version in (target / "Versions").iterdir():
            if not version.is_symlink():
                _remove(version / name)
    # SwiftPM checked the downloaded artifact against the checksum in Sparkle's own
    # manifest at the pinned tag; that checksum must be the one toolchain.json records.
    manifest = scratch(products) / "checkouts/Sparkle/Package.swift"
    try:
        pinned = re.search(r'^let checksum = "([0-9a-f]{64})"$', manifest.read_text(), re.M)
    except OSError:
        pinned = None
    if not pinned or pinned.group(1) != updates.get("sparkle_artifact_sha256"):
        raise SparkleError("Sparkle's binary artifact checksum differs from toolchain.json")
    record = {"version": framework_version(bundle), "removed": list(REMOVED),
              "artifact_sha256": pinned.group(1)}
    if record["version"] != updates["sparkle_version"]:
        raise SparkleError(f"Bundled Sparkle {record['version']} is not the pinned {updates['sparkle_version']}")
    validate(bundle, record, config)
    return record


def framework_version(bundle: Path) -> str | None:
    try:
        info = plistlib.loads((bundle / FRAMEWORK / "Versions/Current/Resources/Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    return info.get("CFBundleShortVersionString")


def validate(bundle: Path, record: dict | None, config: dict) -> None:
    """The bundle ships exactly the pinned framework, without the removed parts."""
    if record is None:
        if (bundle / FRAMEWORK).exists():
            raise SparkleError("Bundle has Sparkle its build-info does not declare")
        return
    framework = bundle / FRAMEWORK
    if framework.is_symlink() or not framework.is_dir():
        raise SparkleError("Missing bundled Sparkle.framework")
    for name in REQUIRED:
        if not (framework / name).is_file():
            raise SparkleError(f"Bundled Sparkle is missing {name}")
    leftover = [str(p.relative_to(framework)) for p in framework.rglob("*")
                if p.name in REMOVED or p.suffix == ".xpc"]
    if leftover:
        raise SparkleError("Bundled Sparkle still has " + ", ".join(sorted(leftover)))
    version = framework_version(bundle)
    if version != settings(config)["sparkle_version"] or version != record.get("version"):
        raise SparkleError("Bundled Sparkle version differs from toolchain.json")


def check_linkage(load_commands: str, dylibs: str) -> None:
    """The app loads Sparkle from @rpath and searches Contents/Frameworks."""
    if not re.search(r"^\s*@rpath/Sparkle\.framework/Versions/B/Sparkle ", dylibs, re.M):
        raise SparkleError("Wisp does not link @rpath/Sparkle.framework")
    if "path @executable_path/../Frameworks " not in load_commands:
        raise SparkleError("Wisp has no @executable_path/../Frameworks search path")


# --- Ed25519 verification (RFC 8032), independent of Sparkle's tools ------------

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A, B = (y1 - x1) * (y2 - x2) % _P, (y1 + x1) * (y2 + x2) % _P
    C, Dd = 2 * t1 * t2 * _D % _P, 2 * z1 * z2 % _P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s, point):
    result = (0, 1, 1, 0)
    while s:
        if s & 1:
            result = _add(result, point)
        point = _add(point, point)
        s >>= 1
    return result


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decompress(data: bytes):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _compress(point) -> bytes:
    zinv = pow(point[2], _P - 2, _P)
    x, y = point[0] * zinv % _P, point[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


_G = (_recover_x(4 * pow(5, _P - 2, _P) % _P, 0), 4 * pow(5, _P - 2, _P) % _P, 1, 0)
_G = (_G[0], _G[1], 1, _G[0] * _G[1] % _P)


def public_key_from_seed(seed: bytes) -> bytes:
    """For test keys only (the E2E fixture); a real private key never reaches this module."""
    digest_ = hashlib.sha512(seed).digest()
    a = int.from_bytes(digest_[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _compress(_mul(a, _G))


def verify_file(public_key_b64: str, signature_b64: str, path: Path) -> bool:
    """Ed25519-verify a whole file, streaming it through SHA-512."""
    try:
        public = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        return False
    if len(public) != 32 or len(signature) != 64:
        return False
    point_a, point_r = _decompress(public), _decompress(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if point_a is None or point_r is None or s >= _L:
        return False
    h = hashlib.sha512(signature[:32] + public)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    k = int.from_bytes(h.digest(), "little") % _L
    return _equal(_mul(s, _G), _add(point_r, _mul(k, point_a)))


# --- Signing (Sparkle's sign_update) -------------------------------------------

def scratch(products: Path) -> Path:
    """SwiftPM's scratch directory (checkouts/, artifacts/) above its products
    directory, whichever build system laid the products out."""
    for parent in products.parents:
        if (parent / "checkouts").is_dir() and (parent / "artifacts").is_dir():
            return parent
    raise SparkleError("No SwiftPM scratch directory above " + str(products))


def tool(products: Path, name: str) -> Path:
    """Sparkle's command-line tools from SwiftPM's checked-out binary artifact."""
    candidates = sorted(scratch(products).glob(f"artifacts/sparkle/Sparkle/bin/{name}"))
    if len(candidates) != 1 or not os.access(candidates[0], os.X_OK):
        raise SparkleError(f"Sparkle's {name} is not in the SwiftPM artifacts; run the Swift build first")
    return candidates[0]


def _sign_update(sign_update: Path, arguments: list, key_file: Path | None, env: dict):
    """Runs sign_update with the private key from the keychain, a key file, or
    SPARKLE_ED_PRIVATE_KEY on standard input. The key is never on a command line
    and never in a log; failures report only sign_update's exit status."""
    secret = os.environ.get(PRIVATE_KEY_ENV, "")
    command = [str(sign_update), *map(str, arguments)]
    stdin = None
    if key_file is not None:
        command += ["--ed-key-file", str(key_file)]
    elif secret:
        command += ["--ed-key-file", "-"]
        stdin = secret.strip() + "\n"
    result = subprocess.run(command, input=stdin, capture_output=True, text=True, env=env, timeout=600)
    if result.returncode:
        raise SparkleError(f"sign_update failed ({result.returncode}); is the EdDSA private key available?")
    return result.stdout


def require_key_source(key_file: Path | None, *, ci: bool) -> None:
    """CI must have the secret; it never falls back to a keychain it doesn't have."""
    if ci and key_file is None and not os.environ.get(PRIVATE_KEY_ENV, "").strip():
        raise SparkleError(f"{PRIVATE_KEY_ENV} is not set; refusing to publish an unsigned update")


def sign_archive(sign_update: Path, archive: Path, key_file: Path | None, env: dict) -> tuple[str, int]:
    out = _sign_update(sign_update, ["-p", archive], key_file, env).strip()
    if not re.fullmatch(r"[A-Za-z0-9+/]{86}==", out):
        raise SparkleError("sign_update did not print one EdDSA signature")
    return out, archive.stat().st_size


def sign_feed(sign_update: Path, feed: Path, key_file: Path | None, env: dict) -> None:
    _sign_update(sign_update, [feed], key_file, env)
    _sign_update(sign_update, ["--verify", feed], key_file, env)


# --- Appcast ---------------------------------------------------------------------

ET.register_namespace("sparkle", SPARKLE_NS)
ET.register_namespace("dc", DC_NS)


def empty_feed(title: str = "Wisp") -> bytes:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def _strip_signature(feed: bytes) -> bytes:
    # sign_update appends the feed's signature as trailing XML comments; a
    # re-signed feed gets a fresh one.
    return re.sub(rb"\s*<!--\s*sparkle-signatures:.*?-->\s*", b"\n", feed, flags=re.S)


def item(*, label: str, build: str, channel: str, url: str, signature: str, length: int,
         minimum_macos: str, notes_markdown: str, published: str) -> ET.Element:
    if not LABEL.fullmatch(label) or channel not in CHANNELS or not re.fullmatch(r"\d+(?:\.\d+){0,2}", build):
        raise SparkleError("Invalid update label, build or channel")
    if not url.startswith("https://"):
        raise SparkleError("Update downloads must be https")
    node = ET.Element("item")
    ET.SubElement(node, "title").text = f"Wisp {label}"
    ET.SubElement(node, "pubDate").text = published
    ET.SubElement(node, f"{{{SPARKLE_NS}}}version").text = build
    ET.SubElement(node, f"{{{SPARKLE_NS}}}shortVersionString").text = label
    ET.SubElement(node, f"{{{SPARKLE_NS}}}minimumSystemVersion").text = minimum_macos
    ET.SubElement(node, f"{{{SPARKLE_NS}}}hardwareRequirements").text = "arm64"
    if channel == BETA:
        ET.SubElement(node, f"{{{SPARKLE_NS}}}channel").text = BETA
    ET.SubElement(node, "description", {f"{{{SPARKLE_NS}}}format": "markdown"}).text = notes_markdown
    ET.SubElement(node, "enclosure", {
        "url": url, "type": "application/octet-stream", "length": str(length),
        f"{{{SPARKLE_NS}}}edSignature": signature,
    })
    return node


def merge(feed: bytes, new: ET.Element) -> bytes:
    """Newest first; a build already in the feed is refused, never replaced."""
    root = ET.fromstring(_strip_signature(feed))
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise SparkleError("Existing appcast is not an RSS feed")
    build = new.findtext(f"{{{SPARKLE_NS}}}version")
    for existing in channel.findall("item"):
        if existing.findtext(f"{{{SPARKLE_NS}}}version") == build:
            raise SparkleError(f"The appcast already has build {build}")
    items = channel.findall("item")
    position = list(channel).index(items[0]) if items else len(channel)
    channel.insert(position, new)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def check_feed(feed: bytes, *, public_key: str, archive: Path, build: str) -> None:
    """The item for this build names this archive's size and an EdDSA signature
    that verifies against the app's own public key."""
    root = ET.fromstring(_strip_signature(feed))
    for node in root.iter("item"):
        if node.findtext(f"{{{SPARKLE_NS}}}version") != build:
            continue
        enclosure = node.find("enclosure")
        if enclosure is None or int(enclosure.get("length", "-1")) != archive.stat().st_size:
            raise SparkleError("Appcast enclosure length differs from the archive")
        if not verify_file(public_key, enclosure.get(f"{{{SPARKLE_NS}}}edSignature", ""), archive):
            raise SparkleError("The archive's EdDSA signature does not verify against the app's public key")
        return
    raise SparkleError(f"The appcast has no item for build {build}")


def notes_markdown(label: str, build: str, release_page: str, notes: str | None) -> str:
    """The update window's notes: the release's own page notes when there are any,
    with the release page linked at the end."""
    body = (notes or "").strip()
    body = (body + "\n\n" if body else "") + f"[Release page for Wisp {label} (build {build})]({release_page})\n"
    # Markdown in a CDATA-free text node: ElementTree escapes it; keep it plain text.
    return html.unescape(body)
