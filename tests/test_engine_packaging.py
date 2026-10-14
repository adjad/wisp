"""Packaging the bundled engine: selection, pruning, pinning, staging, validation, signing hooks.

Uses a synthetic oMLX.app (no signing, no network, no executing staged binaries), so it
runs on any host. What it cannot show — that a pruned, signed engine actually starts and
serves a model — is covered by scripts/engine_smoke.py and the pipeline's engine smoke step.

    python tests/test_engine_packaging.py
"""
from __future__ import annotations

import json
import contextlib
import importlib.util
import io
import os
import plistlib
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build-support"))
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402

PASS = FAIL = 0
MACHO = b"\xcf\xfa\xed\xfe" + b"\0" * 60


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def write(path: Path, data: bytes | str = b"x", mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(mode)
    return path


def make_app(tmp: Path, *, version: str = "0.7.1", bundle_id: str = "app.omlx") -> Path:
    app = tmp / "oMLX.app"
    res = app / "Contents/Resources"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version, "CFBundleVersion": "7"}))
    py = res / "Python/cpython-3.11"
    write(py / "bin/python3.11", MACHO, 0o755)
    os.symlink("python3.11", py / "bin/python3")
    os.symlink("python3.11", py / "bin/python")
    write(py / "lib/libpython3.11.dylib", MACHO, 0o755)
    write(py / "lib/python3.11/os.py", "# os")
    write(py / "lib/python3.11/__pycache__/os.cpython-311.pyc", b"pyc")
    sp = res / "Python/framework-mlx-base/lib/python3.11/site-packages"
    write(sp / "mlx/__init__.py", "")
    write(sp / "mlx/core.cpython-311-darwin.so", MACHO, 0o755)
    write(sp / "mlx/lib/libmlx.dylib", MACHO, 0o755)
    write(sp / "mlx/lib/mlx.metallib", b"metal")
    write(sp / "numpy/__init__.py", "")
    write(sp / "numpy-2.0.dist-info/METADATA", "Name: numpy")
    write(sp / "cv2/__init__.py", "")
    write(sp / "cv2-4.9.dist-info/METADATA", "Name: opencv")
    write(sp / "modelscope.py", "")
    write(sp / "onnxruntime/__init__.py", "")
    write(sp / "onnxruntime-1.0.dist-info/METADATA", "Name: onnxruntime")
    write(sp / "__pycache__/x.cpython-311.pyc", b"pyc")
    write(res / "omlx/server.py", "# server")
    write(res / "omlx/cli.py", "# cli")
    write(res / "omlx/__pycache__/server.cpython-311.pyc", b"pyc")
    write(res / "omlx_web/index.html", "<html>")
    return app


def reader(path: Path) -> list[str]:
    return ["26.2"] if "mlx" in path.name or path.suffix == ".so" else ["14.0"]


def stage(tmp: Path, app: Path, **kw):
    bundle = tmp / "out/Wisp.app"
    bundle.mkdir(parents=True, exist_ok=True)
    defaults = dict(lock=None, release=False, check_signature=False, minos_reader=reader)
    defaults.update(kw)
    return bundle, engine.stage(bundle, app, **defaults)


def names(bundle: Path) -> set[str]:
    base = bundle / engine.ENGINE_DIR
    return {str(p.relative_to(base)) for p in base.rglob("*")}


def test_selection_and_pruning() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bundle, doc = stage(tmp, make_app(tmp))
        have = names(bundle)
        check("kept packages staged", {"sp/mlx/core.cpython-311-darwin.so", "sp/numpy/__init__.py",
                                       "sp/numpy-2.0.dist-info/METADATA", "omlx/server.py",
                                       "omlx_web/index.html", "py/bin/python3.11"} <= have)
        check("pruned package directories gone", not any(n.startswith(("sp/cv2", "sp/onnxruntime")) for n in have))
        check("pruned dist-info gone", not any("cv2-4.9" in n or "onnxruntime-1.0" in n for n in have))
        check("pruned module file gone", "sp/modelscope.py" not in have)
        check("no bytecode caches", not any("__pycache__" in n or n.endswith(".pyc") for n in have))
        check("python3 is a relative link", os.readlink(bundle / engine.ENGINE_DIR / "py/bin/python3") == "python3.11")
        check("ENGINE.json written", (bundle / engine.ENGINE_DIR / "ENGINE.json").is_file())
        check("minimum macOS is the highest Mach-O target", doc["minimum_macos"] == "26.2", doc["minimum_macos"])
        check("signing identifier recorded", doc["signing_identifier"] == "com.wisp.engine.python3")
        check("not release eligible without a lock", doc["release_eligible"] is False)
        mode = (bundle / engine.ENGINE_DIR / "py/bin/python3.11").stat().st_mode
        check("executables stay executable", bool(mode & stat.S_IXUSR))


def test_digest_and_lock() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        app = make_app(tmp)
        prune = engine.load_prune()
        digest = engine.tree_digest(engine.select_files(app, prune), engine.link_entries(app, prune))
        again = engine.tree_digest(engine.select_files(app, prune), engine.link_entries(app, prune))
        check("digest is deterministic", digest == again and len(digest) == 64)
        lock = {"schema": 1, "omlx_version": "0.7.1", "source_tree_sha256": digest, "minimum_macos": "26.2"}
        bundle, doc = stage(tmp, app, lock=lock, release=True)
        check("matching lock is release eligible", doc["release_eligible"] is True and doc["locked"] is True)

        # Content, mode, and link changes all change the digest.
        write(app / "Contents/Resources/omlx/server.py", "# tampered")
        changed = engine.tree_digest(engine.select_files(app, prune), engine.link_entries(app, prune))
        check("changed byte changes the digest", changed != digest)
        shutil.rmtree(tmp / "out")
        try:
            stage(tmp, app, lock=lock, release=True)
            check("tampered source refused by the lock", False)
        except engine.EngineBuildError as exc:
            check("tampered source refused by the lock", "does not match" in str(exc), str(exc))

        write(app / "Contents/Resources/omlx/server.py", "# server")      # restore
        os.chmod(app / "Contents/Resources/omlx/server.py", 0o755)
        check("changed mode changes the digest",
              engine.tree_digest(engine.select_files(app, prune), engine.link_entries(app, prune)) != digest)
        os.chmod(app / "Contents/Resources/omlx/server.py", 0o644)

        shutil.rmtree(tmp / "out")
        try:
            stage(tmp, app, lock=dict(lock, omlx_version="9.9.9"), release=True)
            check("version mismatch refused", False)
        except engine.EngineBuildError:
            check("version mismatch refused", True)
        shutil.rmtree(tmp / "out")
        try:
            stage(tmp, app, lock=dict(lock, minimum_macos="14.0"), release=True)
            check("minimum-macOS mismatch refused", False)
        except engine.EngineBuildError as exc:
            check("minimum-macOS mismatch refused", "differs from the lock" in str(exc), str(exc))
        shutil.rmtree(tmp / "out")
        try:
            stage(tmp, app, lock=None, release=True)
            check("release without a lock refused", False)
        except engine.EngineBuildError:
            check("release without a lock refused", True)


def test_lock_writer() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        app = make_app(tmp)
        output = tmp / "engine.lock.json"
        document = engine.create_lock(app, output, check_signature=False, minos_reader=reader)
        check("lock has the validated schema", engine.load_lock(output) == document)
        check("lock pins the source version", document["omlx_version"] == "0.7.1")
        check("lock records the selected-tree digest", document["source_tree_sha256"] == engine.tree_digest(
            engine.select_files(app), engine.link_entries(app)))
        check("lock records the engine macOS floor", document["minimum_macos"] == "26.2")
        original = output.read_text()
        try:
            engine.create_lock(app, output, check_signature=False, minos_reader=reader)
            check("existing lock is protected", False)
        except engine.EngineBuildError as exc:
            check("existing lock is protected", "--force" in str(exc))
        check("refused lock is unchanged", output.read_text() == original)
        write(app / "Contents/Resources/omlx/server.py", "# revised")
        replacement = engine.create_lock(app, output, force=True, check_signature=False, minos_reader=reader)
        check("force replaces an existing lock", replacement["source_tree_sha256"] != document["source_tree_sha256"]
              and engine.load_lock(output) == replacement)


def test_third_party_inventory() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        app = make_app(tmp)
        resources = app / "Contents/Resources"
        write(resources / "Python/framework-mlx-base/lib/python3.11/site-packages/numpy-2.0.dist-info/METADATA",
              "Name: numpy\nVersion: 2.0\nLicense-Expression: BSD-3-Clause\n")
        write(resources / "Python/framework-mlx-base/lib/python3.11/site-packages/fixture-1.0.dist-info/METADATA",
              "Name: fixture\nVersion: 1.0\nClassifier: License :: OSI Approved :: MIT License\n")
        write(resources / "Python/framework-mlx-base/lib/python3.11/site-packages/simple-1.0.dist-info/METADATA",
              "Name: simple\nVersion: 1.0\nLicense: Apache-2.0\n")
        write(resources / "omlx/LICENSE", "oMLX license text\n")
        inventory = engine.third_party_inventory(app, check_signature=False)
        by_name = {item["name"]: item for item in inventory}
        check("inventory is sorted", inventory == sorted(
            inventory, key=lambda item: (item["name"].casefold(), item["version"].casefold())))
        check("License-Expression is reported", by_name.get("numpy", {}).get("license") == "BSD-3-Clause")
        check("license classifier is reported", by_name.get("fixture", {}).get("license") ==
              "OSI Approved :: MIT License")
        check("License field is reported", by_name.get("simple", {}).get("license") == "Apache-2.0")
        check("pruned distribution metadata is omitted", "opencv" not in by_name and "onnxruntime" not in by_name)
        check("oMLX license file is included", by_name.get("oMLX", {}).get("license_file") == "omlx/LICENSE")


def test_smoke_arguments_and_port_refusal() -> None:
    spec = importlib.util.spec_from_file_location("engine_smoke", ROOT / "scripts/engine_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(smoke)
    args = smoke.build_parser().parse_args([
        "--engine-root", "/tmp/engine", "--model-dir", "/tmp/models", "--model", "small-model"])
    check("smoke arguments are parsed", args.engine_root == Path("/tmp/engine")
          and args.model_dir == Path("/tmp/models") and args.model == "small-model")
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            smoke.build_parser().parse_args([])
            check("required smoke arguments are enforced", False)
        except SystemExit as exc:
            check("required smoke arguments are enforced", exc.code == 2)
    for port in (8000, 8765, 8791):
        try:
            smoke.validate_port(port)
            check(f"reserved smoke port {port} refused", False)
        except smoke.SmokeError:
            check(f"reserved smoke port {port} refused", True)


def test_source_identity() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        try:
            stage(tmp, make_app(tmp, bundle_id="com.example.other"))
            check("non-oMLX bundle refused", False)
        except engine.EngineBuildError:
            check("non-oMLX bundle refused", True)
        original = engine._codesign_info
        try:
            engine._codesign_info = lambda app: "Authority=Developer ID Application: Someone Else\nTeamIdentifier=ABCDE12345\n"
            try:
                engine.verify_source_identity(make_app(tmp / "x" if (tmp / "x").mkdir() is None else tmp))
                check("wrong team refused", False)
            except engine.EngineBuildError as exc:
                check("wrong team refused", "Team ID" in str(exc))
            engine._codesign_info = lambda app: f"TeamIdentifier={engine.OMLX_TEAM_ID}\n"
            check("oMLX team accepted", engine.verify_source_identity(tmp / "x" / "oMLX.app")["version"] == "0.7.1")
        finally:
            engine._codesign_info = original


def test_structural_refusals() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        app = make_app(tmp)
        os.symlink("/etc/hosts", app / "Contents/Resources/omlx/escape")
        try:
            stage(tmp, app)
            check("absolute symlink refused", False)
        except engine.EngineBuildError:
            check("absolute symlink refused", True)
        os.unlink(app / "Contents/Resources/omlx/escape")
        os.symlink("../../../../outside", app / "Contents/Resources/omlx/rel-escape")
        write(tmp / "outside", "x")
        shutil.rmtree(tmp / "out", ignore_errors=True)
        try:
            stage(tmp, app)
            check("symlink leaving the tree refused", False)
        except engine.EngineBuildError:
            check("symlink leaving the tree refused", True)
        os.unlink(app / "Contents/Resources/omlx/rel-escape")
        shutil.rmtree(app / "Contents/Resources/omlx_web")
        shutil.rmtree(tmp / "out", ignore_errors=True)
        try:
            stage(tmp, app)
            check("missing piece refused", False)
        except engine.EngineBuildError:
            check("missing piece refused", True)
        # oMLX 0.7.0 keeps the admin UI inside the server package instead of a separate omlx_web
        write(app / "Contents/Resources/omlx/admin/templates/index.html", "<html>")
        shutil.rmtree(tmp / "out", ignore_errors=True)
        try:
            staged_070, _ = stage(tmp, app)
            check("an admin UI inside omlx/admin (oMLX 0.7.0 layout) is accepted without omlx_web",
                  not (staged_070 / "Contents/Resources/engine/omlx_web").exists()
                  and (staged_070 / "Contents/Resources/engine/omlx/admin/templates/index.html").is_file())
            engine.validate_structure(staged_070, None)
            check("and the staged 0.7.0-layout engine validates", True)
        except engine.EngineBuildError as exc:
            check("an admin UI inside omlx/admin (oMLX 0.7.0 layout) is accepted without omlx_web", False, str(exc))
        shutil.rmtree(tmp / "out", ignore_errors=True)
        app2 = make_app(tmp / "second" if (tmp / "second").mkdir() is None else tmp)
        bundle, _ = stage(tmp / "second", app2)
        try:
            engine.stage(bundle, app2, lock=None, release=False, check_signature=False, minos_reader=reader)
            check("staging twice refused", False)
        except engine.EngineBuildError:
            check("staging twice refused", True)


def test_validate_structure() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bundle, doc = stage(tmp, make_app(tmp))
        check("a clean stage validates", engine.validate_structure(bundle, doc) == doc)
        eng = bundle / engine.ENGINE_DIR

        def refuses(label, mutate, restore) -> None:
            mutate()
            try:
                engine.validate_structure(bundle, doc)
                check(label, False)
            except engine.EngineBuildError:
                check(label, True)
            finally:
                restore()

        real = eng / "py/bin/python3.11"
        saved = real.read_bytes()
        refuses("text interpreter refused", lambda: real.write_bytes(b"#!/bin/sh\n"), lambda: real.write_bytes(saved))
        refuses("non-executable interpreter refused", lambda: real.chmod(0o644), lambda: real.chmod(0o755))
        link = eng / "py/bin/python3"
        refuses("wrong python3 link refused",
                lambda: (link.unlink(), os.symlink("/usr/bin/python3", link)),
                lambda: (link.unlink(), os.symlink("python3.11", link)))
        metal = eng / "sp/mlx/lib/mlx.metallib"
        metal_bytes = metal.read_bytes()
        refuses("missing metallib refused", metal.unlink, lambda: metal.write_bytes(metal_bytes))
        leak = eng / "sp/cv2"
        refuses("pruned package present refused", lambda: write(leak / "__init__.py", ""), lambda: shutil.rmtree(leak))
        refuses("ENGINE.json mismatch refused", lambda: (eng / "ENGINE.json").write_text(
            json.dumps(dict(doc, omlx_version="other"))), lambda: (eng / "ENGINE.json").write_text(json.dumps(doc)))
        refuses("wrong signing identifier refused", lambda: (eng / "ENGINE.json").write_text(
            json.dumps(dict(doc, signing_identifier="python3"))), lambda: (eng / "ENGINE.json").write_text(json.dumps(doc)))
        check("restored bundle validates again", bool(engine.validate_structure(bundle, doc)))


def test_signing_hooks() -> None:
    bundle = Path("/b/Wisp.app")
    real = bundle / engine.ENGINE_PYTHON
    check("engine python carries the attribution identifier",
          engine.codesign_identifier_args(bundle, real) == ["-i", "com.wisp.engine.python3"])
    check("other engine binaries keep the default identifier",
          engine.codesign_identifier_args(bundle, bundle / engine.ENGINE_DIR / "sp/mlx/lib/libmlx.dylib") == [])
    check("backend python keeps the default identifier",
          engine.codesign_identifier_args(bundle, bundle / "Contents/Resources/backend/.venv/bin/python3") == [])
    check("engine targets recognised", engine.is_engine_target(bundle, bundle / engine.ENGINE_DIR / "sp/x.so"))
    check("backend targets not engine", not engine.is_engine_target(bundle, bundle / "Contents/Resources/backend/x.so"))

    import release
    check("engine role has its own entitlements", release.ENTITLEMENT_SOURCES["engine"] == engine.ENGINE_ENTITLEMENTS)
    check("engine files use the engine role",
          release.entitlement_role(bundle, bundle / engine.ENGINE_DIR / "sp/x.so", bundle / "Contents/MacOS/Wisp") == "engine")
    check("backend files keep the python role",
          release.entitlement_role(bundle, bundle / "Contents/Resources/backend/x.so", bundle / "Contents/MacOS/Wisp") == "python")
    check("app keeps the app role",
          release.entitlement_role(bundle, bundle / "Contents/MacOS/Wisp", bundle / "Contents/MacOS/Wisp") == "app")

    wanted = {"com.apple.security.cs.allow-jit", "com.apple.security.cs.allow-unsigned-executable-memory",
              "com.apple.security.cs.disable-library-validation"}
    plist = plistlib.loads((ROOT / engine.ENGINE_ENTITLEMENTS).read_bytes())
    check("engine entitlements are exactly oMLX's three", set(plist) == wanted and all(plist.values()))

    from service.inference import bundled_peer
    check("identifier matches the attribution check", engine.ENGINE_SIGNING_ID == bundled_peer.ENGINE_SIGNING_ID)


def test_runtime_gate() -> None:
    from service.engine.mode import unsupported_reason
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        check("no ENGINE.json (source checkout) is not gated", unsupported_reason(root, "14.0") is None)
        (root / "ENGINE.json").write_text(json.dumps({"minimum_macos": "26.2"}))
        check("older macOS is refused", "26.2" in (unsupported_reason(root, "15.5") or ""))
        check("26.1 is refused", unsupported_reason(root, "26.1") is not None)
        check("exact minimum is allowed", unsupported_reason(root, "26.2") is None)
        check("newer macOS is allowed", unsupported_reason(root, "27.0") is None)
        check("unknown macOS version is refused", unsupported_reason(root, "") is not None)
        (root / "ENGINE.json").write_text("{not json")
        check("unreadable ENGINE.json refuses", unsupported_reason(root, "27.0") is not None)


def test_pinned_download() -> None:
    import hashlib
    source = engine.load_source()
    check("source pin is for the locked oMLX release", source["omlx_version"] == engine.load_lock()["omlx_version"])
    check("asset URL is the official release path",
          engine.asset_url(source) == "https://github.com/jundot/omlx/releases/download/v0.7.0/oMLX-0.7.0-macos26-27.dmg")
    for field, value in (("repo", "evil.example/x"), ("tag", "latest"), ("asset", "../x.dmg"), ("sha256", "00"),
                         ("size", -1), ("omlx_version", "9.9")):
        with tempfile.TemporaryDirectory() as t:
            bad = Path(t) / "src.json"
            bad.write_text(json.dumps({**source, field: value}))
            try:
                engine.load_source(bad)
                check(f"a bad {field} is refused", False)
            except engine.EngineBuildError:
                check(f"a bad {field} is refused", True)
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        payload = b"fake dmg bytes" * 10
        fake = {**source, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        calls: list[str] = []

        def fetch(url: str, dest: Path, limit: int) -> None:
            calls.append(url)
            dest.write_bytes(payload)

        got = engine.verified_download(fake, t / "cache", fetch=fetch)
        check("a matching download is accepted", got.read_bytes() == payload and len(calls) == 1)
        engine.verified_download(fake, t / "cache", fetch=fetch)
        check("a verified cache entry is reused without downloading", len(calls) == 1)
        got.write_bytes(b"tampered")
        engine.verified_download(fake, t / "cache", fetch=fetch)
        check("a tampered cache entry is replaced, never reused", len(calls) == 2 and got.read_bytes() == payload)

        def bad_fetch(url: str, dest: Path, limit: int) -> None:
            dest.write_bytes(b"x" * len(payload))          # right size, wrong digest

        for kind, fn, want in (("a wrong digest", bad_fetch, "does not match"),):
            try:
                engine.verified_download(fake, t / "cache2", fetch=fn)
                check(f"{kind} is refused", False)
            except engine.EngineBuildError as exc:
                check(f"{kind} is refused", want in str(exc))
        check("and leaves no partial file behind", not list((t / "cache2").glob("*")))
        try:
            engine.verified_download(fake, t / "cache3", offline=True, fetch=fetch)
            check("an offline build with no cached release is refused", False)
        except engine.EngineBuildError:
            check("an offline build with no cached release is refused", True)

        events: list[str] = []

        def attach(dmg: Path, mount: Path) -> None:
            events.append("attach")
            (mount / "oMLX.app").mkdir()

        def detach(mount: Path) -> None:
            events.append("detach")
            shutil.rmtree(mount / "oMLX.app", ignore_errors=True)

        with engine.fetched_app(t / "cache", source=fake, fetch=fetch, attach=attach, detach=detach) as app:
            check("the mounted oMLX.app is yielded", app.name == "oMLX.app" and app.is_dir())
        check("and unmounted afterwards", events == ["attach", "detach"])
        events.clear()
        try:
            with engine.fetched_app(t / "cache", source=fake, fetch=fetch, attach=attach, detach=detach):
                raise RuntimeError("build failed")
        except RuntimeError:
            pass
        check("an unmount still happens when the build fails", events == ["attach", "detach"])
        events.clear()

        def attach_empty(dmg: Path, mount: Path) -> None:
            events.append("attach")

        try:
            with engine.fetched_app(t / "cache", source=fake, fetch=fetch, attach=attach_empty, detach=detach):
                pass
            check("an image without oMLX.app is refused", False)
        except engine.EngineBuildError:
            check("an image without oMLX.app is refused", events == ["attach", "detach"])


def test_smoke_plan() -> None:
    with tempfile.TemporaryDirectory() as t:
        bundle = Path(t) / "Wisp.app"
        doc = bundle / engine.ENGINE_DIR / "ENGINE.json"
        doc.parent.mkdir(parents=True)
        doc.write_text(json.dumps({"minimum_macos": "26.2"}))
        for host, mode in (("15.5", "skip"), ("26.1", "skip"), ("26.2", "run"), ("26.5.1", "run"), ("27.0", "run"),
                           ("", "run"), ("garbage", "run")):
            plan = engine.smoke_plan(bundle, host)
            check(f"host {host or '(unreadable)'} -> {mode}", plan["mode"] == mode, str(plan))
        plan = engine.smoke_plan(bundle, "15.5")
        check("the skip says why", "15.5" in plan["reason"] and "26.2" in plan["reason"])


def test_prune_file() -> None:
    prune = engine.load_prune()
    check("prune list is non-empty and safe", len(prune["drop"]) > 20)
    for essential in ("mlx", "mlx_lm", "transformers", "numpy", "tokenizers", "fastapi", "uvicorn",
                      "pydantic", "jinja2", "huggingface_hub", "mlx_embeddings", "safetensors", "xgrammar"):
        check(f"{essential} is never pruned", essential not in prune["drop"])
    check("bytecode is always removed", ".pyc" in prune["always_remove_suffixes"])


def main() -> int:
    for fn in (test_selection_and_pruning, test_digest_and_lock, test_lock_writer,
               test_third_party_inventory, test_smoke_arguments_and_port_refusal, test_source_identity,
               test_structural_refusals, test_validate_structure, test_signing_hooks,
               test_runtime_gate, test_prune_file, test_pinned_download, test_smoke_plan):
        print(fn.__name__)
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
