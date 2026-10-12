"""service.behavior must stay portable: a pure engine the Mac runtime calls
in-process. Importing service.assistant / service.tools /
service.main / service.config / service.inference would build live stores
against ~/.moe or require macOS, so no module in the package may import them.

Checked statically (every import at any depth, including function-local ones)
so the result does not depend on what other tests already put in sys.modules.
The only third-party import allowed is httpx (phraser.py)."""
import ast
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "service" / "behavior"
BANNED = ("service.assistant", "service.tools", "service.main", "service.config", "service.inference")
ALLOWED_THIRD_PARTY = {"httpx"}


def _imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # relative import: stays inside the package
                yield "service.behavior." + (node.module or "")
            else:
                yield node.module or ""


def test_behavior_package_never_imports_the_live_assistant():
    files = sorted(PACKAGE.glob("*.py"))
    assert {"engine.py", "miners.py", "store.py"} <= {f.name for f in files}
    for path in files:
        for name in _imports(path):
            assert not any(name == b or name.startswith(b + ".") for b in BANNED), f"{path.name} imports {name}"


def test_behavior_package_uses_only_stdlib_itself_and_httpx():
    for path in sorted(PACKAGE.glob("*.py")):
        for name in _imports(path):
            top = name.split(".")[0]
            if top == "service":
                assert name.startswith("service.behavior"), f"{path.name} imports {name}"
            else:
                assert top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY or top == "__future__", \
                    f"{path.name} imports third-party {name}"


def test_engine_modules_do_not_import_httpx():
    """The engine side is pure stdlib; only the phraser talks to a network."""
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "phraser.py":
            continue
        assert "httpx" not in {n.split(".")[0] for n in _imports(path)}, path.name
