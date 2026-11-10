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
                     r"await stopAndWait\(\)\s*await startIfNeeded\(\)\s*\}", manager)
