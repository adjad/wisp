"""Bundling Ideate's harness (build-support/ideate_harness.py) on a tiny fake Pi and a fake Node.

Nothing is executed and nothing is signed: the fake Node is a few Mach-O magic bytes. What it
cannot show (that the pruned Pi starts under the bundled Node) is covered by the preview
build's own run and the orchestrator's end-to-end test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build-support"))

import ideate_harness as h  # noqa: E402

MACHO = b"\xcf\xfa\xed\xfe" + b"\0" * 60
SECRET = "invented-secret-marker"


def write(path: Path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    if mode:
        path.chmod(mode)
    return path


@pytest.fixture
def source(tmp_path):
    """A Pi install root with one release, private files beside it, and a Node install."""
    root = tmp_path / "pi-agent"
    write(root / "install" / "current-version", "9.9.9\n")
    release = root / "install" / "releases" / "9.9.9"
    agent = release / "node_modules/@earendil-works/pi-coding-agent"
    write(release / "package.json", '{"name": "pi-release"}')
    write(agent / "package.json", json.dumps({"version": "9.9.9", "author": "Invented Author <a@example.com>"}))
    write(agent / "dist/bundle/cli.js", "#!/usr/bin/env node\nconsole.log('pi')\n", 0o755)
    write(agent / "docs/rpc.md", "docs")
    write(agent / "docs/images/shot.png", b"png")
    write(agent / "examples/extensions/gate.ts", "example")
    write(agent / "CHANGELOG.md", "changes")
    native = release / "node_modules/@earendil-works/pi-tui/native"
    write(native / "darwin/prebuilds/darwin-arm64/darwin-platform.node", MACHO)
    write(native / "darwin/prebuilds/darwin-x64/darwin-platform.node", MACHO)
    write(native / "linux/prebuilds/linux-x64/linux-platform-x11.node", b"\x7fELF")
    write(native / "win32/prebuilds/win32-x64/win32-platform.node", b"MZ")
    foo = release / "node_modules/foo"
    write(foo / "index.js", "module.exports = 1")
    write(foo / "index.js.map", "{}")
    write(foo / "index.d.ts", "export {}")
    write(foo / "test/t.js", "test")
    write(foo / "LICENSE", "MIT foo")
    write(foo / ".DS_Store", b"x")
    write(release / "node_modules/@types/node/index.d.ts", "types")
    (release / "node_modules/.bin").mkdir(parents=True)
    os.symlink("../@earendil-works/pi-coding-agent/dist/bundle/cli.js", release / "node_modules/.bin/pi")
    # The user's private Pi files: never copied.
    write(root / "auth.json", SECRET)
    write(root / "settings.json", SECRET)
    write(root / "models-store.json", SECRET)
    write(root / "sessions" / "s.jsonl", SECRET)
    node_home = tmp_path / "node-install"
    node = write(node_home / "bin" / "node", MACHO, 0o755)
    write(node_home / "include/node/node_version.h",
          "#define NODE_MAJOR_VERSION 24\n#define NODE_MINOR_VERSION 21\n#define NODE_PATCH_VERSION 0\n")
    write(node_home / "LICENSE", "Node.js is licensed for use as follows: (invented test text)")
    return root, release, node


def staged(tmp_path, source, **kwargs):
    root, _release, node = source
    bundle = tmp_path / "Wisp.app"
    bundle.mkdir()
    record = h.stage(bundle, root, node, lock={"schema": 1, "harness": "pi"}, **kwargs)
    return bundle, record


def test_stage_prunes_copies_and_validates(tmp_path, source):
    bundle, record = staged(tmp_path, source)
    target = bundle / h.BUNDLE_DIR
    pi = target / "pi"
    h.validate(bundle, record)
    assert record["version"] == "9.9.9" and record["node_version"] == "24.21.0" and record["pinned"] is False
    assert record["node_license"] == "copied"
    assert (pi / h.PI_ENTRY).is_file() and os.access(pi / h.PI_ENTRY, os.X_OK)
    assert (target / h.NODE_PATH).read_bytes() == MACHO and os.access(target / h.NODE_PATH, os.X_OK)
    agent = pi / "node_modules/@earendil-works/pi-coding-agent"
    assert (agent / "docs/rpc.md").is_file() and not (agent / "docs/images").exists()
    assert not (agent / "examples").exists() and not (agent / "CHANGELOG.md").exists()
    native = pi / "node_modules/@earendil-works/pi-tui/native"
    assert (native / "darwin/prebuilds/darwin-arm64/darwin-platform.node").is_file()
    assert not (native / "darwin/prebuilds/darwin-x64").exists()
    assert not (native / "linux").exists() and not (native / "win32").exists()
    foo = pi / "node_modules/foo"
    assert (foo / "index.js").is_file() and (foo / "LICENSE").is_file()
    for gone in ("index.js.map", "index.d.ts", "test", ".DS_Store"):
        assert not (foo / gone).exists(), gone
    assert not (pi / "node_modules/@types").exists()
    link = pi / "node_modules/.bin/pi"
    assert link.is_symlink() and not os.path.isabs(os.readlink(link)) and link.resolve().is_file()
    mit = (target / "LICENSES/Pi-MIT.txt").read_text()
    assert "Copyright (c) Invented Author" in mit and "a@example.com" not in mit
    assert (target / "LICENSES/Node-LICENSE").is_file()
    assert json.loads((target / "HARNESS.json").read_text()) == record
    for path in bundle.rglob("*"):
        assert path.name not in ("auth.json", "settings.json", "models-store.json", "sessions")
        if path.is_file() and not path.is_symlink():
            assert SECRET.encode() not in path.read_bytes()


def test_manifest_is_deterministic_and_pins(tmp_path, source):
    bundle, record = staged(tmp_path, source)
    again = tmp_path / "again"
    again.mkdir()
    second = staged(again, source)[1]
    assert second["manifest_sha256"] == record["manifest_sha256"] and second["entries"] == record["entries"]
    root, _release, node = source
    lock = {"schema": 1, "harness": "pi", "version": "9.9.9", "manifest_sha256": record["manifest_sha256"],
            "node": {"sha256": record["node_sha256"]}}
    pinned = tmp_path / "pinned" / "Wisp.app"
    pinned.mkdir(parents=True)
    assert h.stage(pinned, root, node, lock=lock, release=True)["pinned"] is True
    other = tmp_path / "other" / "Wisp.app"
    other.mkdir(parents=True)
    with pytest.raises(h.HarnessError, match="lock"):
        h.stage(other, root, node, lock=dict(lock, version="9.9.8"), release=True)


def test_validate_catches_tampering(tmp_path, source):
    bundle, record = staged(tmp_path, source)
    target = bundle / h.BUNDLE_DIR
    js = target / "pi/node_modules/foo/index.js"
    js.write_text("module.exports = 2")
    with pytest.raises(h.HarnessError, match="manifest"):
        h.validate(bundle, record)
    js.write_text("module.exports = 1")
    h.validate(bundle, record)
    with pytest.raises(h.HarnessError, match="does not declare"):
        h.validate(bundle, None)
    with pytest.raises(h.HarnessError, match="differs"):
        h.validate(bundle, dict(record, version="1.0.0"))
    os.symlink("/etc/hosts", target / "pi/node_modules/foo/abs")
    with pytest.raises(h.HarnessError, match="Non-relocatable"):
        h.validate(bundle, record)
    (target / "pi/node_modules/foo/abs").unlink()
    (target / h.NODE_PATH).chmod(0o644)
    with pytest.raises(h.HarnessError, match="Node"):
        h.validate(bundle, record)


def test_validate_without_harness(tmp_path):
    bundle = tmp_path / "Wisp.app"
    bundle.mkdir()
    h.validate(bundle, None)


def test_bad_sources_are_refused(tmp_path, source):
    root, release, node = source
    os.symlink("/etc/hosts", release / "node_modules/foo/abs")
    bundle = tmp_path / "Wisp.app"
    bundle.mkdir()
    with pytest.raises(h.HarnessError, match="leaves it"):
        h.stage(bundle, root, node, lock={"schema": 1, "harness": "pi"})
    (release / "node_modules/foo/abs").unlink()
    write(root / "install" / "current-version", "../../evil\n")
    with pytest.raises(h.HarnessError, match="not a version"):
        h.release_dir(root)
    with pytest.raises(h.HarnessError, match="not a Pi release"):
        h.release_dir(tmp_path)


def test_old_node_and_missing_licence(tmp_path, source):
    root, _release, node = source
    header = node.parent.parent / "include/node/node_version.h"
    header.write_text("#define NODE_MAJOR_VERSION 20\n#define NODE_MINOR_VERSION 1\n#define NODE_PATCH_VERSION 0\n")
    bundle = tmp_path / "a" / "Wisp.app"
    bundle.mkdir(parents=True)
    with pytest.raises(h.HarnessError, match="older"):
        h.stage(bundle, root, node, lock={"schema": 1, "harness": "pi"})
    header.write_text("#define NODE_MAJOR_VERSION 24\n#define NODE_MINOR_VERSION 21\n#define NODE_PATCH_VERSION 0\n")
    (node.parent.parent / "LICENSE").unlink()
    bundle = tmp_path / "b" / "Wisp.app"
    bundle.mkdir(parents=True)
    record = h.stage(bundle, root, node, lock={"schema": 1, "harness": "pi"})
    assert record["node_license"] == "reference"
    assert "nodejs/node" in (bundle / h.BUNDLE_DIR / "LICENSES/Node-NOTICE.txt").read_text()
    bundle = tmp_path / "c" / "Wisp.app"
    bundle.mkdir(parents=True)
    with pytest.raises(h.HarnessError, match="LICENSE"):
        h.stage(bundle, root, node, lock={"schema": 1, "harness": "pi"}, release=True)


def test_repository_lock_and_pipeline_wiring():
    lock = h.load_lock()
    assert lock["version"] and len(lock["manifest_sha256"]) == 64 and len(lock["node"]["sha256"]) == 64
    source = (ROOT / "build-support/pipeline.py").read_text()
    assert 'p.add_argument("--ideate-harness", type=Path' in source
    assert "ideate_source=ideate_source, ideate_node=args.ideate_node" in source
    assert "ideate_harness.validate(bundle, meta.get(\"ideate_harness\"))" in source
    entitlements = (ROOT / h.NODE_ENTITLEMENTS).read_text()
    assert "com.apple.security.cs.allow-jit" in entitlements
    assert "com.apple.security.cs.allow-unsigned-executable-memory" in entitlements


def _pipeline_checks():
    spec = importlib.util.spec_from_file_location("pipeline_checks_for_ideate",
                                                  ROOT / "tests/build_pipeline/pipeline_checks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pipeline_validate_structure_accepts_and_checks_the_harness(tmp_path, source):
    checks = _pipeline_checks()
    case = checks.PipelineTests()
    case.setUp()
    try:
        bundle = case.fixture()
        p = checks.p
        p.validate_structure(bundle, case.meta)                   # no harness, no record: fine
        root, _release, node = source
        record = h.stage(bundle, root, node, lock={"schema": 1, "harness": "pi"})
        p.validate_structure(bundle, dict(case.meta, ideate_harness=record))
        with pytest.raises(p.BuildError, match="does not declare"):
            p.validate_structure(bundle, dict(case.meta, ideate_harness=None))
        with pytest.raises(p.BuildError, match="does not declare"):
            p.validate_structure(bundle, case.meta)
    finally:
        case.tearDown()
