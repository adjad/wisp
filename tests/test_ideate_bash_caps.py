"""Ideate's gate puts a time limit on every bash call and keeps the tail of its output; a timeout
reaches the model as one line plus the last lines. Runs the generated extension's helpers in node
with invented output; no Pi, no model."""
import json
import re
import shutil
import subprocess

import pytest

from service.ideate import config, harness

NODE = shutil.which("node") or "/usr/local/bin/node"


def _helpers() -> str:
    ext = harness.gate_extension()
    consts = "\n".join(l for l in ext.splitlines() if l.startswith("const "))
    funcs = []
    for name in ("bashTimeout", "tail", "bashResult"):
        start = ext.index(f"function {name}(")
        end = ext.index("\n}\n", start) + 3
        funcs.append(ext[start:end])
    return consts + "\n" + "\n".join(funcs)


def _run(expr: str):
    if not shutil.which(NODE):
        pytest.skip("node not installed")
    out = subprocess.run([NODE, "-e", _helpers() + f"\nprocess.stdout.write(JSON.stringify({expr}));"],
                         capture_output=True, text=True, timeout=20, check=True)
    return json.loads(out.stdout)


def test_every_bash_call_gets_a_time_limit():
    assert config.BASH_TIMEOUT_S == 120 and config.BASH_TIMEOUT_MAX_S == 600
    assert _run('bashTimeout({command: "sleep 9999"})') == 120                 # none asked: the default
    assert _run('bashTimeout({command: "make test", timeout: 30})') == 30        # smaller stays
    assert _run('bashTimeout({command: "make test", timeout: 99999})') == 600    # never past the max
    assert _run('bashTimeout({command: "x", timeout: -5})') == 120
    loop = _run('bashTimeout({command: "ideate-loop --until \\"test -f ready\\" --max 600", timeout: 99999})')
    assert loop == config.LOOP_BASH_TIMEOUT_S > config.LOOP_HARD_MAX_S          # the loop stops itself first
    ext = harness.gate_extension()
    assert "event.input.timeout = bashTimeout(event.input);" in ext


def test_long_output_keeps_its_tail():
    text = "line\n" * 2000 + "FAILED test_parser: expected 3, got 4"
    out = _run(f"bashResult({json.dumps(text)})")
    assert len(out) <= config.TOOL_RESULT_CHARS + 40
    assert out.endswith("FAILED test_parser: expected 3, got 4")
    assert out.startswith("[... ") and "characters cut" in out
    assert _run('bashResult("ok")') == "ok"


def test_a_timeout_is_one_line_plus_the_last_lines():
    text = "starting server\n" + "waiting\n" * 500 + "still waiting on port 4100\n\nCommand timed out after 120 seconds"
    out = _run(f"bashResult({json.dumps(text)})")
    assert out.startswith("stopped after 120 s, last lines:\n")
    assert out.rstrip().endswith("still waiting on port 4100")
    assert "Command timed out" not in out
    assert len(out) <= config.TOOL_RESULT_CHARS + 40
