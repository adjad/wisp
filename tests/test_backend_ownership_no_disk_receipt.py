"""NATIVE-A1: Wisp must never trust a backend-ownership record read from disk.

A program running as the same user can write any file Wisp can, including a valid
0600 launch receipt naming its own live process. If Wisp believed that file, it could
treat the squatter as its own backend and SIGTERM it on the next launch. The fix keeps
the receipt in memory only, for the one process the running app spawned. These checks
read the shipped Swift sources (no sockets, no subprocesses) so the disk path cannot
quietly come back.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "app" / "Sources" / "WispApp"


def _read(name: str) -> str:
    return (SOURCES / name).read_text(encoding="utf-8")


def _code(text: str) -> str:
    """Swift source with line comments removed, so prose cannot mask or fake a hit."""
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def test_no_shipped_swift_source_names_the_receipt_file():
    swift = sorted(SOURCES.rglob("*.swift"))
    assert swift, "no Swift sources found"
    offenders = [p.name for p in swift if "backend-launch-receipt.json" in p.read_text(encoding="utf-8")]
    assert offenders == []


FILE_IO = [
    r"\bFileManager\b",
    r"\bFileHandle\b",
    r"Data\s*\(\s*contentsOf\s*:",
    r"\bwrite\s*\(\s*to\s*:",
    r"\bDarwin\.write\b",
    r"(?<![\w.])open\s*\(",
    r"\bfopen\s*\(",
    r"\brename\s*\(",
    r"\bunlink\s*\(",
    r"\bJSONSerialization\b",
    r"applicationSupportDirectory",
]


def test_backend_ownership_has_no_file_access():
    code = _code(_read("BackendOwnership.swift"))
    hits = [pattern for pattern in FILE_IO if re.search(pattern, code)]
    assert hits == []


def test_receipt_store_offers_no_disk_api():
    code = _code(_read("BackendOwnership.swift"))
    for removed in ("func configure(", "func read(", "func write(", "defaultDirectory", "func encode(",
                    "func decode(", "fileName"):
        assert removed not in code, removed


def test_startup_never_adopts_or_signals_a_port_holder():
    app = _code(_read("AppDelegate.swift"))
    # No disk receipt is loaded, and nothing is signalled at startup.
    assert "BackendLaunchReceiptStore" not in app
    assert "terminateOwned" not in app
    assert "isResponsive" not in app
    # The startup check is made with no receipt: every holder is a conflict.
    assert re.search(r"PortGuard\.check\(\s*port:\s*8765\s*,\s*receipt:\s*\.none\s*\)", app)


def test_a_healthy_responder_is_not_assumed_to_be_wisp():
    manager = _code(_read("BackendManager.swift"))
    # The old early return treated ANY responder on the port as Wisp's backend.
    assert not re.search(r"if\s+healthy\s*&&\s*!freshRecovery\s*\{\s*return\s*\}", manager)
    assert "BackendOwnership.portDecision(" in manager
