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


def test_port_guard_has_no_file_access():
    # FileHandle.nullDevice only discards lsof's stderr; it opens nothing.
    code = _code(_read("PortGuard.swift")).replace("FileHandle.nullDevice", "")
    hits = [pattern for pattern in FILE_IO if re.search(pattern, code)]
    assert hits == []


def test_backend_manager_never_persists_the_receipt():
    """BackendManager legitimately reads logs and credential state, so it is judged line by line:
    nothing that touches the launch receipt may also touch a file."""
    code = _code(_read("BackendManager.swift"))
    # (The unrelated credentials "receipt" in this file is a different, existing mechanism.)
    launch_receipt = r"receiptStore|LaunchReceipt|BackendOwnership\.Receipt|launchReceipt"
    touching_receipt = [line for line in code.splitlines() if re.search(launch_receipt, line)]
    assert touching_receipt, "expected BackendManager to record a launch receipt in memory"
    hits = [(pattern, line.strip()) for line in touching_receipt for pattern in FILE_IO if re.search(pattern, line)]
    assert hits == []
    assert not re.search(r"launch-receipt|receipt\.json|receiptURL|receiptPath", code)


def test_a_healthy_responder_is_not_assumed_to_be_wisp():
    manager = _code(_read("BackendManager.swift"))
    # The old early return treated ANY responder on the port as Wisp's backend.
    assert not re.search(r"if\s+healthy\s*&&\s*!freshRecovery\s*\{\s*return\s*\}", manager)
    # The decision is acted on exactly as BackendOwnership.portDecision gives it (its truth table is
    # checked behaviourally by PortGuardChecks): a stranger is reported and nothing is launched,
    # only our own live child is accepted as already running, and launching happens only on `.proceed`.
    switch = re.search(
        r"switch\s+BackendOwnership\.portDecision\((?P<args>.*?)\)\s*\{(?P<body>.*?)\n        \}\n",
        manager, re.DOTALL)
    assert switch, "startIfNeeded must switch over BackendOwnership.portDecision"
    cases = dict(re.findall(r"case\s+\.(\w+):\s*(.*?)(?=\n\s*case\s+\.|\Z)", switch.group("body"), re.DOTALL))
    assert set(cases) == {"ownBackendRunning", "stranger", "proceed"}
    assert re.fullmatch(r"return", cases["ownBackendRunning"].strip())
    assert re.fullmatch(r"reportPortConflict\(\)\s*return", " ".join(cases["stranger"].split()))
    assert cases["proceed"].strip() == "break"
    assert "liveChildPID: child" in switch.group("args")
    assert "receipt: receiptStore.current" in switch.group("args")


def test_startup_conflict_is_retried_only_with_no_receipt_and_never_acted_on():
    manager = _code(_read("BackendManager.swift"))
    helper = re.search(r"static func settledStartupVerdict\(.*?\n    \}\n", manager, re.DOTALL)
    assert helper, "settledStartupVerdict is missing"
    body = helper.group(0)
    # It only looks again; it never adopts, reclaims or signals the holder.
    for forbidden in ("terminate", "kill(", "terminateOwned", "record(", "receiptStore"):
        assert forbidden not in body, forbidden
    app = _code(_read("AppDelegate.swift"))
    assert re.search(r"settledStartupVerdict\(\s*check:\s*\{\s*PortGuard\.check\(\s*port:\s*8765\s*,\s*receipt:\s*\.none\s*\)", app)
    # Quit waits for the child it spawned before the app terminates.
    assert re.search(r"await backend\.stopAndWait\(\)\s*await MainActor\.run \{ NSApp\.terminate", app)


def test_backend_trust_uses_only_the_in_memory_ownership_policy():
    """H-16's per-request check must not reintroduce a disk receipt, a folder-based trust signal,
    or an unstated claim: it judges only the process this app spawned and records its limits."""
    trust = _read("BackendTrust.swift")
    code = _code(trust)
    # (It parses the /identity JSON the backend sends over the network, hence JSONSerialization.)
    file_io = [pattern for pattern in FILE_IO if "JSONSerialization" not in pattern]
    assert [pattern for pattern in file_io if re.search(pattern, code)] == []
    for removed in ("ownedPrefixes", "executableInOwnedDirectory", "foreignExecutable", "foreignListener",
                    ".unusable", "configure(directory", "defaultDirectory"):
        assert removed not in code, removed
    app_text = _read("AppDelegate.swift")
    assert "BackendTrustConfiguration.set()" in _code(app_text)
    assert "ownedBackendPrefixes" not in _code(app_text)


def _comment_block(text: str, ending_at: str) -> str:
    head = text[:text.index(ending_at)]
    return " ".join(line.removeprefix("//").strip() for line in head.splitlines() if line.startswith("//"))


def test_the_n7_residual_is_stated_where_the_trust_claim_is_made():
    """The guarantee is honest only if the limits sit next to it: the header says what is and is not
    guaranteed and why (a separate, unauthenticated connection after the final inspection), and
    every other place that describes the gate repeats the caveat instead of saying 'proven'."""
    header = _comment_block(_read("BackendTrust.swift"), "enum BackendTrust {")
    for needed in ("WHAT THIS GUARANTEES", "DOES NOT GUARANTEE", "N7", "NATIVE-A2 is NOT closed",
                   "not itself authenticated", "after the final inspection",
                   "never evicted", "authenticated transport"):
        assert needed in header, needed
    protocol_doc = _read("BackendTrust.swift")
    protocol_doc = protocol_doc[:protocol_doc.index("final class BackendTrustProtocol")].rsplit("\n\n", 1)[-1]
    protocol_doc = " ".join(line.removeprefix("///").strip() for line in protocol_doc.splitlines())
    assert "N7" in protocol_doc and "does not authenticate the connection" in protocol_doc
    app = _read("AppDelegate.swift")
    start = app.index("BackendTrustConfiguration.set()")
    assert "N7" in app[max(0, start - 500):start]
    assert "proven to be Wisp's own backend" not in app and "proven to be Wisp's own backend" not in protocol_doc
    # A 404 from /identity is no evidence for a backend this app spawned.
    trust = _code(_read("BackendTrust.swift"))
    missing = re.search(r"case \.missing:\s*return (?P<what>[^\n]+)", trust)
    assert missing and ".refused" in missing.group("what") and ".trusted" not in missing.group("what")


def test_other_descriptions_match_the_memory_only_receipt():
    assert "would be trusted by the real app" not in (ROOT / "sandbox" / "run.sh").read_text(encoding="utf-8")
    identity = (ROOT / "service" / "identity.py").read_text(encoding="utf-8")
    assert "memory only" in " ".join(identity.split()) or "in the app's memory only" in " ".join(identity.split())
