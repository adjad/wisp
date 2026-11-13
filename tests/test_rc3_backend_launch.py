"""How the app launches its service, read from BackendManager.swift.

No process is started and no socket is opened: the uvicorn arguments are read
from the Swift source and checked against the installed uvicorn's own options
and the service's port parser.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "app" / "Sources" / "WispApp" / "BackendManager.swift"


def _code(text: str) -> str:
    """Swift source without line comments."""
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _manager() -> str:
    return _code(MANAGER.read_text(encoding="utf-8"))


def _uvicorn_arguments() -> list[str]:
    block = re.search(r"proc\.arguments = \[(?P<items>.*?)\n        \]", _manager(), re.DOTALL)
    assert block, "the backend launch arguments are missing"
    items = block.group("items")
    arguments = re.findall(r'"([^"]*)"', items)
    # Literals only: nothing computed can slip an argument past this test.
    assert re.sub(r'"[^"]*"|[\s,]', "", items) == "", items
    return arguments


def _value_after(arguments: list[str], flag: str) -> str:
    assert arguments.count(flag) == 1, flag
    return arguments[arguments.index(flag) + 1]


def _stop_wait_seconds() -> float:
    found = re.search(r"func stopAndWait\(timeout: TimeInterval = (?P<s>[0-9.]+)\)", _manager())
    assert found, "stopAndWait's default wait is missing"
    return float(found.group("s"))


def _engine_stop_grace() -> float:
    from service import main
    found = re.search(r"stop_if_running, final=True, grace=(?P<s>[0-9.]+)\)", inspect.getsource(main.lifespan))
    assert found, "lifespan's engine stop is missing"
    return float(found.group("s"))


def test_the_service_is_never_started_trusting_proxy_headers():
    """Uvicorn trusts X-Forwarded-For from 127.0.0.1 unless told not to, and reports the address
    and port in it as the caller. The service proves "this is the Wisp app" from the caller's
    port, so every launch of the backend turns that off."""
    assert _uvicorn_arguments().count("--no-proxy-headers") == 1
    assert "--proxy-headers" not in _uvicorn_arguments() and "--forwarded-allow-ips" not in _uvicorn_arguments()
    launches = []
    for folder, patterns in (("scripts", ("*.sh", "*.py")), ("sandbox", ("*.sh", "*.py")),
                             ("build-support", ("*.sh", "*.py")), ("service", ("*.py",))):
        for pattern in patterns:
            for path in sorted((ROOT / folder).rglob(pattern)):
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if "uvicorn" in line and "service.main:app" in line and not line.lstrip().startswith("#"):
                        launches.append((f"{path.relative_to(ROOT)}:{number}", line))
    assert {where.split(":")[0] for where, _ in launches} >= {"scripts/run.sh", "sandbox/run.sh", "scripts/eval_arms.py"}
    missing = [where for where, line in launches if "--no-proxy-headers" not in line]
    assert missing == [], missing


def test_the_launch_still_names_the_service_host_and_port_first():
    arguments = _uvicorn_arguments()
    assert arguments[:7] == ["-m", "uvicorn", "service.main:app", "--host", "127.0.0.1", "--port", "8765"]
    from service import identity
    assert identity.bind_port(arguments) == 8765


def test_every_option_is_one_the_installed_uvicorn_accepts():
    import importlib
    # The package rebinds `uvicorn.main` to its command, so take the module itself.
    command = importlib.import_module("uvicorn.main").main
    known = {name for parameter in command.params
             for name in (*parameter.opts, *parameter.secondary_opts)}
    options = [argument for argument in _uvicorn_arguments() if argument.startswith("--")]
    assert options and [option for option in options if option not in known] == []


def test_shutdown_is_bounded_so_an_open_event_stream_cannot_hold_it(  # l05-live-logs#3
):
    """Without the bound uvicorn waits for the app's /assistant/events response
    (open for up to 25 s), the app's wait runs out and the service is killed
    before its own shutdown stops the engine."""
    seconds = _value_after(_uvicorn_arguments(), "--timeout-graceful-shutdown")
    assert re.fullmatch(r"[1-9][0-9]*", seconds), "uvicorn takes whole seconds"
    wait = _stop_wait_seconds()
    assert int(seconds) < wait
    # The bound and then the engine's own stop both fit inside the app's wait.
    assert int(seconds) + _engine_stop_grace() < wait
    from service import main
    assert int(seconds) < main.ASSISTANT_EVENTS_ROTATE_S
    # Quit and restart still wait for the child; the wait itself is unchanged.
    manager = _manager()
    assert re.search(r"func restart\(\) async \{\s*guard !portConflict else \{ return \}\s*"
                     r"backendChanges \+= 1\s*await stopAndWait\(\)\s*await startIfNeeded\(\)\s*"
                     r"endBackendChange\(\)\s*\}", manager)


# -- the backend log (a01-app-lifecycle#6) ----------------------------------------

def test_the_request_log_is_off():
    """One line per request, with its query string (Memory search text among
    them), about twice a second: none of it is kept."""
    arguments = _uvicorn_arguments()
    assert arguments.count("--no-access-log") == 1
    assert "--access-log" not in arguments
    # Start-up errors and tracebacks still reach the log: the level is not raised.
    assert "--log-level" not in arguments and "--log-config" not in arguments
    # A flag without a value must not sit where a value is read.
    for flag in ("--host", "--port", "--timeout-graceful-shutdown"):
        assert not _value_after(arguments, flag).startswith("--"), flag


def _function(source: str, signature: str) -> str:
    start = source.index(signature)
    open_at = source.index("{", source.index(")", start))
    depth = 0
    for i in range(open_at, len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            return source[open_at:i + 1]
    raise AssertionError(f"unbalanced body after {signature!r}")


def test_the_log_is_rotated_before_this_launchs_offset_is_read():
    manager = _manager()
    launch = _function(manager, "private func startIfNeeded(freshRecovery: Bool) async")
    rotate = launch.index("Self.rotateBackendLogIfNeeded(at: logURL)")
    assert launch.count("rotateBackendLogIfNeeded(") == 1
    # The path is unchanged (the README names it).
    assert launch.index('appendingPathComponent("moe-backend.log")') < rotate
    # Before the offset: otherwise the start-failure reason would be read from past the end.
    assert rotate < launch.index("let logStartOffset = Self.fileSize(at: logURL)")
    # Only on the way to a launch: after the port decision and with no child of ours alive.
    assert launch.index("case .proceed:") < launch.index("guard process == nil") < rotate < launch.index("try proc.run()")
    # Appending and the per-launch segment read are as before.
    assert "try handle.seekToEnd()" in launch
    assert "Self.logText(at: logURL, after: logStartOffset)" in launch
    # Nowhere else: never while a backend is running.
    assert manager.count("rotateBackendLogIfNeeded(") == 2


def test_the_rotation_keeps_one_old_log_and_has_a_small_limit():
    manager = _manager()
    limit = re.search(r"static let backendLogRotateBytes: UInt64 = (?P<n>[0-9]+) \* 1024 \* 1024", manager)
    assert limit and 1 <= int(limit.group("n")) <= 8
    helper = _function(manager, "static func rotateBackendLogIfNeeded(")
    assert "guard fileSize(at: url) > maxBytes else { return false }" in helper
    assert 'url.appendingPathExtension("1")' in helper
    assert helper.index("removeItem(at: previous)") < helper.index("moveItem(at: url, to: previous)")
    # It touches the log and its one old copy, nothing else, and signals nothing.
    for forbidden in ("terminate", "kill(", "receipt", "contentsOfDirectory", "NSHomeDirectory"):
        assert forbidden not in helper, forbidden


# -- the "Wisp isn't using its service" alert (a01-app-lifecycle#3) -----------------

APP = ROOT / "app" / "Sources" / "WispApp" / "AppDelegate.swift"
TRUST = ROOT / "app" / "Sources" / "WispApp" / "BackendTrust.swift"


def _app() -> str:
    return _code(APP.read_text(encoding="utf-8"))


def test_the_refusal_alert_is_decided_before_anything_is_shown():
    """The alert used to come up for Wisp's own restart and, at launch, under the
    port-conflict alert. BackendTrust.refusalNotice decides (its table is checked
    in BackendTrustChecks.swift); here: it is asked first, with the real state."""
    present = _function(_app(), "private func presentBackendRefusal(_ message: String)")
    asked = present.index("BackendTrust.refusalNotice(")
    assert asked < present.index("lastRefusalShown = message") < present.index("alert.runModal()")
    for argument in ("startupJudged: startupPortJudged", "changingOwnBackend: backend.changingBackend",
                     "portConflict: backend.portConflict", "quitting: shutdownStarted || appTerminating"):
        assert argument in present, argument
    # A held or dropped refusal is not remembered as shown, so it can be shown later.
    assert re.search(r"case \.hold: refusalHeld = true; return\s*case \.drop: return", present)
    # A held one is never shown from memory: the gate is asked to say it again.
    release = _function(_app(), "private func releaseHeldRefusal()")
    assert "BackendTrustGate.shared.announceAgain()" in release
    assert "runModal" not in release and "presentBackendRefusal" not in release


def test_at_launch_the_port_conflict_alert_is_the_only_alert():
    app = _app()
    conflict = re.search(r"if case \.conflict\(let foreign\) = verdict \{(?P<body>.*?)\n            \}", app, re.DOTALL)
    assert conflict, "the startup conflict branch is missing"
    body = conflict.group("body")
    # The conflict is recorded before the port counts as judged, so a refusal for the
    # same holder is dropped, not shown under (or after) the conflict alert.
    assert (body.index("self.backend.portConflict = true") < body.index("self.startupPortWasJudged()")
            < body.index("self.presentPortConflict("))
    # Judged only once the settled verdict is in, on both paths.
    assert app.index("settledStartupVerdict(") < app.index("self.startupPortWasJudged()")
    assert app.count("self.startupPortWasJudged()") == 2
    assert app.count("startupPortJudged = true") == 1


def test_the_manager_marks_its_own_stop_and_restart():
    manager = _manager()
    stop = _function(manager, "func stopAndWait(timeout: TimeInterval = 5) async")
    assert (stop.index("detachForStop()") < stop.index("backendChanges += 1")
            < stop.index("terminateAndWait(") < stop.index("endBackendChange()"))
    end = _function(manager, "private func endBackendChange()")
    assert "backendChanges -= 1" in end and "if backendChanges == 0 { onBackendChangeEnded() }" in end
    assert manager.count("backendChanges += 1") == manager.count("endBackendChange()") - 1 == 2
    assert "backend.onBackendChangeEnded = { [weak self] in self?.releaseHeldRefusal() }" in _app()


def test_quit_on_the_refusal_alert_stops_wisps_own_service_first():
    present = _function(_app(), "private func presentBackendRefusal(_ message: String)")
    assert re.search(r"if backend\.childRunning \{ quit\(\) \} else \{ terminateWithoutShutdown\(\) \}", present)
    assert present.count("terminateWithoutShutdown()") == 1


def test_a_port_that_changes_mid_check_is_never_trusted():
    """Whatever the second look finds, the request is not sent: every way out of
    the mismatch branch is unreachable or refused."""
    trust = _code(TRUST.read_text(encoding="utf-8"))
    tail = trust[trust.index("let current = inspect()"):trust.index("return decide(current, answer)")]
    assert "guard let current else { return .refused(.cannotInspect) }" in tail
    assert "if current.isEmpty { return .unreachable }" in tail
    returns = re.findall(r"return ([^\n}]+)", tail)
    assert returns and all(value.strip().startswith((".refused(", ".unreachable", "why", "nil")) for value in returns), returns
    assert ".trusted" not in tail
    # The pure policy: an exited process is unreachable only when no stranger is listed.
    decide = trust[trust.index("static func decide("):trust.index("let pids = listeners.map")]
    assert decide.index("if !unproven.isEmpty { return .refused(.unproven(unproven)) }") < decide.index("if exited { return .unreachable }")
    assert "if known == nil { exited = true } else { return .refused(.cannotInspect) }" in decide
