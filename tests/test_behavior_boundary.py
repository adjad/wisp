"""service.behavior must stay portable: a pure engine the Mac runtime calls
in-process. Importing service.assistant / service.tools /
service.main / service.config / service.inference would build live stores
against ~/.moe or require macOS, so no module in the package may import them.

Checked statically (every import at any depth, including function-local ones)
so the result does not depend on what other tests already put in sys.modules.
The only third-party import allowed is httpx (phraser.py). The exact shared
publication-authority primitive is also permitted, provided its entire dependency
surface stays standard-library-only and importing it has no live effects."""
import ast
import builtins
import io
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "service" / "behavior"
BANNED = ("service.assistant", "service.tools", "service.main", "service.config", "service.inference")
ALLOWED_THIRD_PARTY = {"httpx"}
SHARED_PRIMITIVE = "service.publication_authority"
# These reviewed dependencies provide only the primitive's memory/clock/types
# machinery. New dependencies require review rather than inheriting a broad
# exception for all service modules or all standard-library IO facilities.
PRIMITIVE_STDLIB = {
    "__future__", "contextlib", "dataclasses", "functools", "inspect", "math",
    "threading", "time", "weakref",
}


def _imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A second leading dot reaches service; it must not be mistaken
                # for an internal behavior import and bypass the live-store ban.
                assert node.level <= 2, "relative import escapes the service package"
                prefix = "service.behavior" if node.level == 1 else "service"
                if node.module:
                    yield prefix + "." + node.module
                else:
                    yield from (prefix + "." + alias.name for alias in node.names)
            else:
                yield node.module or ""


def _assert_portable_import(name):
    top = name.split(".")[0]
    if top == "service":
        assert (name == "service.behavior" or name.startswith("service.behavior.")
                or name == SHARED_PRIMITIVE), f"nonportable service import: {name}"
    else:
        assert top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY or top == "__future__", \
            f"third-party import: {name}"


def _assert_primitive_dependencies(source):
    """Check all depths, including imports hidden inside a function or class."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "primitive must not import another service module relatively"
            names = [node.module or ""]
        else:
            names = []
        for name in names:
            assert name in PRIMITIVE_STDLIB, f"unreviewed primitive dependency: {name}"
            assert name in sys.stdlib_module_names or name == "__future__", name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"__import__", "eval", "exec", "compile"}, \
                "primitive must not hide dependencies in dynamic code or imports"


def test_behavior_package_never_imports_the_live_assistant():
    files = sorted(PACKAGE.glob("*.py"))
    assert {"engine.py", "miners.py", "store.py"} <= {f.name for f in files}
    for path in files:
        for name in _imports(path):
            assert not any(name == b or name.startswith(b + ".") for b in BANNED), f"{path.name} imports {name}"


def test_behavior_package_uses_only_stdlib_itself_and_httpx():
    for path in sorted(PACKAGE.glob("*.py")):
        for name in _imports(path):
            _assert_portable_import(name)


def test_shared_primitive_has_only_reviewed_stdlib_dependencies():
    _assert_primitive_dependencies((PACKAGE.parent / "publication_authority.py").read_text())


@pytest.mark.parametrize("name", [
    "service.behavior", "service.behavior.engine", SHARED_PRIMITIVE, "sqlite3", "httpx",
])
def test_portable_import_rule_accepts_only_the_intended_shared_module(name):
    _assert_portable_import(name)


@pytest.mark.parametrize("name", [
    "service.behavior_extra", "service.publication_authority_extra",
    "service.publication_authority.child", "service", "service.config",
    "service.assistant", "service.tools", "service.main", "service.inference", "requests",
])
def test_portable_import_rule_rejects_neighboring_and_live_dependencies(name):
    with pytest.raises(AssertionError):
        _assert_portable_import(name)


@pytest.mark.parametrize("source, expected", [
    ("from .types import Observation", "service.behavior.types"),
    ("from . import types", "service.behavior.types"),
    ("from .. import publication_authority", SHARED_PRIMITIVE),
])
def test_relative_imports_resolve_to_their_actual_dependency(source, expected, tmp_path):
    path = tmp_path / "synthetic_behavior_module.py"
    path.write_text(source)
    assert list(_imports(path)) == [expected]
    _assert_portable_import(expected)


@pytest.mark.parametrize("source", [
    "from ..config import models_config", "from .. import config", "from ... import config",
])
def test_relative_imports_cannot_hide_live_or_outside_dependencies(source, tmp_path):
    path = tmp_path / "synthetic_behavior_module.py"
    path.write_text(source)
    with pytest.raises(AssertionError):
        for name in _imports(path):
            _assert_portable_import(name)


@pytest.mark.parametrize("source", [
    "import math\nfrom threading import RLock\n",
    "def f():\n    from weakref import ref\n",
])
def test_primitive_dependency_rule_accepts_reviewed_stdlib_at_any_depth(source):
    _assert_primitive_dependencies(source)


@pytest.mark.parametrize("source", [
    "import service.config", "from service.assistant import store", "from . import config",
    "def f():\n    import service.tools", "import httpx", "import requests",
    "import socket", "import subprocess", "import ctypes", "import importlib",
    "__import__('service.main')", "eval('1')", "exec('import service.main')",
    "compile('import service.main', 'hidden', 'exec')",
])
def test_primitive_dependency_rule_rejects_live_transport_and_dynamic_code(source):
    with pytest.raises(AssertionError):
        _assert_primitive_dependencies(source)


def _load_inert_primitive(source, monkeypatch):
    _assert_primitive_dependencies(source)
    code = compile(source, str(PACKAGE.parent / "publication_authority.py"), "exec")
    # Preload only reviewed stdlib dependencies before refusing import-time IO.
    for name in PRIMITIVE_STDLIB:
        __import__(name)
    original_import = builtins.__import__

    def reviewed_import(name, globals=None, locals=None, fromlist=(), level=0):
        assert level == 0 and name in PRIMITIVE_STDLIB, f"live import during primitive initialization: {name}"
        return original_import(name, globals, locals, fromlist, level)

    def denied(*args, **kwargs):
        raise AssertionError("shared primitive import attempted IO, a thread, a process or a clock read")

    module = types.ModuleType("_wisp_publication_boundary_fixture")
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, module.__name__, module)
        patch.setattr(builtins, "__import__", reviewed_import)
        for owner, name in [(builtins, "open"), (io, "open"), (os, "open"),
                            (os, "system"), (socket, "socket"), (sqlite3, "connect"),
                            (subprocess, "Popen"), (threading.Thread, "start"),
                            (time, "time"), (time, "monotonic"), (time, "sleep")]:
            patch.setattr(owner, name, denied)
        exec(code, module.__dict__)
    return module


def test_shared_primitive_import_is_inert_even_when_live_subsystems_are_cached(monkeypatch):
    """Execute the actual module afresh; cached application modules confer no access."""
    source = (PACKAGE.parent / "publication_authority.py").read_text()
    module = _load_inert_primitive(source, monkeypatch)
    assert module.LIVE_CLOCK._reading is None
    assert module._local.__dict__ == {}


@pytest.mark.parametrize("source", [
    "import time\ntime.time()", "from time import sleep\nsleep(0.01)",
    "from threading import Thread\nThread(target=lambda: None).start()",
])
def test_primitive_import_guard_rejects_clock_wait_and_thread_effects(source, monkeypatch):
    with pytest.raises(AssertionError, match="attempted IO"):
        _load_inert_primitive(source, monkeypatch)


def test_primitive_import_guard_rejects_file_io_before_creation(tmp_path, monkeypatch):
    target = tmp_path / "must-not-be-created"
    with pytest.raises(AssertionError, match="attempted IO"):
        _load_inert_primitive(f"open({str(target)!r}, 'w')", monkeypatch)
    assert not target.exists()


def test_engine_modules_do_not_import_httpx():
    """The engine side is pure stdlib; only the phraser talks to a network."""
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "phraser.py":
            continue
        assert "httpx" not in {n.split(".")[0] for n in _imports(path)}, path.name
