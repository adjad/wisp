#!/usr/bin/env python3
"""Build and verify the inactive A07 Safari appex in disposable directories.

This does not install an extension, connect to Wisp, or sign a release app.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "browser-extension/safari/WispSafariExtension.xcodeproj"
SAFARI = ROOT / "browser-extension/safari"
SHARED = ROOT / "browser-extension/shared"
HANDLER = ROOT / "app/Sources/WispSafariExtension/SafariWebExtensionHandler.swift"
FIXTURE = ROOT / "tests/browser_safari/SafariPolicyFixture.swift"
IDENTIFIER = "com.wisp.assistant.safari-extension"


def run(*command: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{' '.join(command[:2])} failed:\n{(result.stdout + result.stderr)[-3000:]}")
    return result.stdout + result.stderr


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_bundle(appex: Path) -> None:
    info = plistlib.loads((appex / "Contents/Info.plist").read_bytes())
    extension = info["NSExtension"]
    assert info["CFBundleIdentifier"] == IDENTIFIER
    assert info["LSMinimumSystemVersion"] == "14.0"
    assert extension == {
        "NSExtensionPointIdentifier": "com.apple.Safari.web-extension",
        "NSExtensionPrincipalClass": "WispSafariExtension.SafariWebExtensionHandler",
    }
    resources = appex / "Contents/Resources"
    for name, source in {
        "manifest.json": SAFARI / "manifest.json",
        "background.js": SAFARI / "background.js",
        "contracts.js": SHARED / "contracts.js",
        "page-extractor.js": SHARED / "page-extractor.js",
    }.items():
        assert sha256(resources / name) == sha256(source), f"Resource drift: {name}"
    manifest = json.loads((resources / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert manifest["incognito"] == "not_allowed"
    assert manifest["permissions"] == ["nativeMessaging"]
    assert "host_permissions" not in manifest and "content_scripts" not in manifest
    assert manifest["background"] == {"service_worker": "background.js"}
    executable = appex / "Contents/MacOS/WispSafariExtension"
    assert executable.is_file()
    architectures = run("/usr/bin/lipo", "-archs", str(executable)).split()
    assert "arm64" in architectures
    build_versions = run("xcrun", "vtool", "-show-build", str(executable))
    assert build_versions.count("platform MACOS") == len(architectures)
    assert build_versions.count("minos 14.0") == len(architectures)
    for dependency in run("/usr/bin/otool", "-L", str(executable)).splitlines():
        if dependency.endswith("):"):
            continue
        path = dependency.strip().split(" (", 1)[0]
        assert path.startswith(("/usr/lib/", "/System/Library/")), path


def main() -> None:
    developer = Path("/Applications/Xcode.app/Contents/Developer")
    if not developer.is_dir():
        raise RuntimeError("Full Xcode is required for the Safari extension gate")
    env = dict(os.environ, DEVELOPER_DIR=str(developer))
    with tempfile.TemporaryDirectory(prefix="wisp-safari-") as scratch:
        scratch_path = Path(scratch)
        derived = scratch_path / "DerivedData"
        run("xcodebuild", "-quiet", "-project", str(PROJECT), "-scheme", "WispSafariExtension",
            "-configuration", "Release", "-derivedDataPath", str(derived),
            "CODE_SIGNING_ALLOWED=NO", "build", env=env)
        appex = derived / "Build/Products/Release/WispSafariExtension.appex"
        verify_bundle(appex)
        fixture_binary = scratch_path / "safari-policy-fixture"
        run("xcrun", "swiftc", "-target", "arm64-apple-macos14.0",
            "-module-cache-path", str(scratch_path / "ModuleCache"), str(HANDLER),
            str(FIXTURE), "-o", str(fixture_binary), env=env)
        fixture_result = run(str(fixture_binary), env=env).strip()
        assert fixture_result == "Safari policy: 8 isolated denial cases passed"

        # Signing and nesting are tested in a synthetic host only. The real
        # SwiftPM app assembly has a separate owner and is not modified here.
        host = scratch_path / "SyntheticWisp.app"
        plugins = host / "Contents/PlugIns"
        plugins.mkdir(parents=True)
        (host / "Contents/MacOS").mkdir()
        shutil.copyfile("/usr/bin/true", host / "Contents/MacOS/SyntheticWisp")
        (host / "Contents/MacOS/SyntheticWisp").chmod(0o755)
        (host / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.wisp.assistant.synthetic-safari-fixture",
            "CFBundleExecutable": "SyntheticWisp", "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1", "CFBundleShortVersionString": "1.0",
        }))
        embedded = plugins / appex.name
        shutil.copytree(appex, embedded)
        run("/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(embedded))
        run("/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(host))
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(host))
        verify_bundle(embedded)
        print(json.dumps({"status": "pass", "profile_private_cases": 8,
                          "appex_id": IDENTIFIER, "embedding": "synthetic", "signing": "ad-hoc"},
                         sort_keys=True))


if __name__ == "__main__":
    main()
