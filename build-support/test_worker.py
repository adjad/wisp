"""Run one trusted repository test with fail-closed side-effect guards.

Not a sandbox for hostile code. The caller also supplies a macOS Seatbelt profile.
HOME is never changed. Only owned scratch is writable; accidental real actions
fail the suite even when service code catches the exception.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import runpy
import sys
from urllib.parse import urlsplit, unquote


def install_guard(root: Path, scratch: Path, runtime: Path) -> list[str]:
    violations: list[str] = []
    real_home = Path.home().resolve()
    allowed_reads = (root.resolve(), scratch.resolve(), runtime.resolve(), Path(sys.prefix).resolve())
    scratch = scratch.resolve()

    def deny(event: str) -> None:
        violations.append(event)
        raise PermissionError(f"Wisp test isolation denied {event}")

    def check_path(value, write=False):
        if value is None or isinstance(value, int):
            return
        raw = os.fsdecode(value)
        if raw == ":memory:":
            return
        if raw.startswith("file:"):
            parsed = urlsplit(raw)
            if parsed.netloc or not parsed.path.startswith("/"):
                deny("non-local SQLite URI")
            raw = unquote(parsed.path)
        path = Path(raw).resolve()
        if write and path != Path("/dev/null") and not path.is_relative_to(scratch):
            deny("write outside test scratch")
        if path.is_relative_to(real_home) and not any(path.is_relative_to(p) for p in allowed_reads):
            deny("read outside allowed repository/runtime paths")

    def audit(event, args):
        if event == "open":
            flags = args[2] or 0
            check_path(args[0], bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        elif event == "sqlite3.connect":
            check_path(args[0], True)
        elif event in ("os.listdir", "os.scandir"):
            check_path(args[0])
        elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"):
            # fd-relative operations are resolved/enforced by native Seatbelt.
            fd_index = {"os.mkdir": 2, "os.remove": 1, "os.rmdir": 1, "os.chmod": 2, "os.utime": 3}.get(event)
            if fd_index is None or len(args) <= fd_index or args[fd_index] in (None, -1):
                check_path(args[0], True)
        elif event in ("os.rename", "os.link", "os.symlink"):
            check_path(args[0], True)
            check_path(args[1], True)
        elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "pty.spawn"):
            deny("child process")
        elif event in ("socket.connect", "socket.connect_ex", "socket.sendto", "socket.getaddrinfo", "socket.bind"):
            deny("network")
    sys.addaudithook(audit)
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("script", "pytest", "smoke", "fixture", "guard-probe"))
    parser.add_argument("target")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.root))
    runtime = Path(sys.base_prefix)
    violations = install_guard(args.root, args.scratch, runtime)
    result = 0
    try:
        if args.kind == "guard-probe":
            # Catching a denial must still produce a failing worker exit code.
            try:
                Path(args.target).write_text("must never be written")
            except PermissionError:
                pass
        else:
            # Redirect oMLX configuration explicitly; WISP_HOME only covers Wisp.
            import service.memory.identity as identity
            identity._full_name = ""  # deterministic identity; never query the real macOS account
            import service.config as config
            config.OMLX_SETTINGS = args.scratch / "omlx-settings.json"
            config.OMLX_MODEL_SETTINGS = args.scratch / "omlx-model-settings.json"
            config.OMLX_SETTINGS.write_text('{"server":{"host":"127.0.0.1","port":8000},"auth":{"api_key":""}}')
            config.OMLX_MODEL_SETTINGS.write_text('{}')
            if args.kind == "fixture":
                import json
                from tests.research_library_fixtures import fixtures
                print(json.dumps(fixtures()))
            elif args.kind == "script":
                sys.argv = [args.target]
                runpy.run_path(args.target, run_name="__main__")
            elif args.kind == "pytest":
                import pytest
                result = pytest.main([args.target, "-q", "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider", "--basetemp", str(args.scratch / "pytest")])
            else:
                import importlib
                import pkgutil
                import service
                for module in pkgutil.walk_packages(service.__path__, "service."):
                    importlib.import_module(module.name)
                import service.main
                assert service.main.app
                for name in ("fastapi", "uvicorn", "httpx", "yaml", "pydantic", "docx", "openpyxl", "pptx", "pypdf"):
                    importlib.import_module(name)
                from io import BytesIO
                from PIL import Image
                for format in ("PNG", "JPEG"):
                    encoded = BytesIO()
                    Image.new("RGB", (2, 2), "white").save(encoded, format=format)
                    encoded.seek(0)
                    with Image.open(encoded) as decoded:
                        decoded.load()
                        assert decoded.size == (2, 2)
                print("Packaged backend imports and resources OK (lifespan not started)")
    except SystemExit as exc:
        result = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
    finally:
        if violations:
            print("ISOLATION VIOLATIONS: " + ", ".join(sorted(set(violations))), file=sys.stderr)
    return 86 if violations else result


if __name__ == "__main__":
    raise SystemExit(main())
