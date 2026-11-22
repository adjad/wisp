"""Ideate's loop runner (service/ideate/loop.py, the `ideate-loop` command) and how the gate,
the prompt and the harness setup treat it.

No model, no engine, no Pi: the runner is run as a real subprocess on invented shell
commands under pytest's tmp_path. The sandbox-exec case skips where a sandbox can't be applied.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from service.ideate import config, gate, harness, loop, sandbox

LOOP_PY = Path(loop.__file__)


def run_loop(tmp_path, *args, timeout=60, env=None):
    """The runner as the agent runs it: python3 with a temp dir for its output, cwd = work."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    full_env = {"PATH": os.environ["PATH"], "TMPDIR": str(tmp) + "/", **(env or {})}
    return subprocess.run([sys.executable, str(LOOP_PY), *args], cwd=work, env=full_env,
                          capture_output=True, text=True, timeout=timeout)


# A child that hangs with the shell alone: Simulation QA's sandbox refuses to run /bin/sleep.
HANG = "/bin/sh -c 'while :; do :; done'"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# 1. succeeds after N iterations -----------------------------------------------------------

def test_loop_succeeds_after_n_checks(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    # Each check counts itself in a file; it passes on the fourth.
    check = 'echo x >> ticks.txt; [ "$(wc -l < ticks.txt)" -ge 4 ]'
    result = run_loop(tmp_path, "--until", check, "--every", "0.2", "--max", "30")
    assert result.returncode == 0, result.stdout
    assert "done: 4 checks" in result.stdout and "last exit 0" in result.stdout
    assert (work / "ticks.txt").read_text().count("x") == 4


def test_do_runs_before_each_check(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = run_loop(tmp_path, "--do", "echo y >> ticks.txt", "--until",
                      '[ "$(wc -l < ticks.txt)" -ge 3 ]', "--every", "0.2")
    assert result.returncode == 0 and "done: 3 checks" in result.stdout


# 2. time cap and kill --------------------------------------------------------------------

def test_time_cap_stops_a_loop_that_never_passes(tmp_path):
    start = time.monotonic()
    result = run_loop(tmp_path, "--until", "false", "--every", "0.2", "--max", "2")
    assert result.returncode == 1 and "hit the time cap" in result.stdout
    assert time.monotonic() - start < 10


def test_a_hung_check_is_killed_with_its_children(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    # The check starts a child that outlives it unless the whole group is killed.
    cmd = HANG + " & echo $! > child.pid; " + HANG   # outlives the check unless its group is killed
    start = time.monotonic()
    result = run_loop(tmp_path, "--until", cmd, "--max", "2", "--check", "1")
    assert result.returncode == 1 and time.monotonic() - start < 15
    assert "last exit timeout" in result.stdout
    pid = int((work / "child.pid").read_text())
    for _ in range(50):
        if not alive(pid):
            break
        time.sleep(0.1)
    assert not alive(pid)


def test_caps_are_clamped_and_iteration_cap_applies(tmp_path):
    result = run_loop(tmp_path, "--until", "false", "--every", "0.2", "--iters", "3", "--max", "99999")
    assert result.returncode == 3 and "hit the iteration cap" in result.stdout
    assert "max cut to 600s" in result.stdout
    code, text = loop.run_loop("false", every=0.2, max_s=1, iters=10 ** 6)
    assert code == 1 and "iters cut to 1000" in text


def test_caps_match_config():
    assert (loop.DEFAULT_MAX_S, loop.HARD_MAX_S) == (config.LOOP_MAX_S, config.LOOP_HARD_MAX_S)
    assert (loop.DEFAULT_ITERS, loop.HARD_ITERS) == (config.LOOP_ITERS, config.LOOP_HARD_ITERS)
    assert loop.OUTPUT_BYTES == config.LOOP_OUTPUT_BYTES and loop.SUMMARY_CHARS == config.LOOP_SUMMARY_CHARS
    assert loop.SUMMARY_CHARS < config.TOOL_RESULT_CHARS


def test_bad_arguments_exit_two(tmp_path):
    result = run_loop(tmp_path, "--every", "1")
    assert result.returncode == 2 and "usage" in result.stdout


# 3. output capping and summary length ------------------------------------------------------

def test_noisy_output_is_capped_and_summary_is_short(tmp_path):
    # ~2 MB of long lines per check; the check file is limited and only the tail reaches the summary.
    noisy = "yes 'invented noise line that is long enough to make plenty of bytes quickly' | head -c 2000000; false"
    result = run_loop(tmp_path, "--until", noisy, "--every", "0.2", "--iters", "2")
    assert result.returncode == 3
    assert len(result.stdout) <= config.LOOP_SUMMARY_CHARS + 1
    assert len(result.stdout) < config.TOOL_RESULT_CHARS
    assert not any(len(line) > 105 for line in result.stdout.splitlines()[1:])


def test_summary_keeps_only_the_last_lines_and_cuts_long_ones(tmp_path):
    cmd = "for i in 1 2 3 4 5 6 7 8; do echo line-$i; done; printf 'z%.0s' $(seq 1 400); echo; false"
    result = run_loop(tmp_path, "--until", cmd, "--iters", "1")
    body = result.stdout.splitlines()[1:]
    assert len(body) <= loop.TAIL_LINES and "line-1" not in result.stdout
    assert all(len(line) <= loop.LINE_CHARS for line in body)
    assert len(result.stdout) <= loop.SUMMARY_CHARS + 1


def test_output_file_limit_stops_a_runaway_writer(tmp_path):
    start = time.monotonic()
    result = run_loop(tmp_path, "--until", "yes invented | head -c 50000000 >/dev/stdout; true",
                      "--iters", "1", "--max", "20")
    assert time.monotonic() - start < 20
    assert result.returncode in (0, 3)
    assert len(result.stdout) <= loop.SUMMARY_CHARS + 1


# 4. sandbox scope ------------------------------------------------------------------------

@pytest.mark.skipif(not sandbox.usable(), reason="a sandbox cannot be applied inside another sandbox")
def test_loop_under_the_ideate_profile_cannot_write_outside_the_project(tmp_path):
    project = Path(os.path.realpath(tmp_path)) / "proj"
    work, agent = project / "work", project / ".ideate" / "agent"
    work.mkdir(parents=True)
    agent.mkdir(parents=True)
    (agent / "tmp").mkdir()
    runner_path = project / ".ideate" / "bin" / "ideate-loop"
    runner_path.parent.mkdir()
    runner_path.write_text(harness.loop_runner_source())
    runner_path.chmod(0o700)
    profile = project / ".ideate" / "p.sb"
    profile.write_text(sandbox.build_profile(project, [work, agent], 1))
    outside = Path(os.path.realpath(tmp_path)) / "outside.txt"
    script = (f'{runner_path} --until \'echo in > in.txt; echo out > {outside}; echo meta > '
              f'{project}/.ideate/meta.json; test -f in.txt\' --iters 1')
    env = {"PATH": "/usr/bin:/bin", "TMPDIR": str(agent / "tmp") + "/", "HOME": str(agent)}
    result = subprocess.run(sandbox.contain(["/bin/sh", "-c", script], profile), cwd=work, env=env,
                            capture_output=True, text=True, timeout=60)
    assert (work / "in.txt").exists(), result.stdout + result.stderr
    assert not outside.exists() and not (project / ".ideate" / "meta.json").exists()


def test_harness_installs_the_runner_where_the_agent_cannot_write(tmp_path):
    from service.ideate import store
    project = store.Store(tmp_path / "projects").create("invented thing", "general")
    install = harness.HarnessInstall("pi", Path(sys.executable), Path("cli.js"), "0", "env", [])
    spec = harness.PiAdapter().prepare(project, install, harness.EngineTarget("http://127.0.0.1:1/v1", 1),
                                       kind="general", resume=False)
    tool = project.private / "bin" / "ideate-loop"
    assert tool.is_file() and os.access(tool, os.X_OK)
    assert tool.read_text().startswith("#!/usr/bin/env python3\n")
    assert spec.env["PATH"].split(os.pathsep)[0] == str(project.private / "bin")
    # Outside the folders the sandbox lets the harness write.
    assert project.private / "bin" not in (project.work, project.agent)
    assert project.private / "bin" != project.agent and project.agent not in (project.private / "bin").parents


# 5. cancel kills the group ------------------------------------------------------------------

@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_cancel_kills_the_running_check_group(tmp_path, sig):
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "tmp").mkdir()
    cmd = HANG + " & echo $! > child.pid; wait"
    proc = subprocess.Popen([sys.executable, str(LOOP_PY), "--until", cmd, "--max", "300"], cwd=work,
                            env={"PATH": os.environ["PATH"], "TMPDIR": str(tmp_path / "tmp") + "/"},
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    for _ in range(100):
        if (work / "child.pid").exists() and (work / "child.pid").read_text().strip():
            break
        time.sleep(0.1)
    child = int((work / "child.pid").read_text())
    assert alive(child)
    # What runner._kill_group does to the harness's group: SIGTERM to the whole group.
    os.killpg(proc.pid, sig)
    out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 143 and "cancelled" in out
    for _ in range(50):
        if not alive(child):
            break
        time.sleep(0.1)
    assert not alive(child)


# 6. approvals -----------------------------------------------------------------------------

def test_a_loop_is_one_card_showing_the_exact_command(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    command = 'ideate-loop --until "python3 -m pytest -q" --every 2 --max 120'
    verdict = gate.judge("bash", {"command": command}, work)
    assert verdict.action == gate.CONFIRM and verdict.text == command
    assert gate.LOOP_NOTE in verdict.note
    # Even a loop of read-only checks is never run without a card.
    quiet = gate.judge("bash", {"command": 'ideate-loop --until "test -f done.txt"'}, work)
    assert quiet.action == gate.CONFIRM
    # Without the loop the same read-only check needs no card, so the loop did not loosen anything.
    assert gate.judge("bash", {"command": "ls"}, work).action == gate.ALLOW


def test_what_a_loop_runs_is_judged_by_the_same_rules(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    denied = gate.judge("bash", {"command": 'ideate-loop --until "rm -rf /"'}, work)
    assert denied.action == gate.DENY
    denied = gate.judge("bash", {"command": 'ideate-loop --do "open http://example.com" --until "true"'}, work)
    assert denied.action == gate.DENY
    net = gate.judge("bash", {"command": 'ideate-loop --until "curl -s https://example.com"'}, work)
    assert net.action == gate.CONFIRM and gate.NO_NETWORK_NOTE in net.note
    engine = gate.judge("bash", {"command": 'ideate-loop --until "nc -z 127.0.0.1 8000"'}, work, 8000)
    assert engine.action == gate.CONFIRM and gate.ENGINE_NOTE in engine.note
    # An unparsable loop still asks.
    assert gate.judge("bash", {"command": "ideate-loop --until 'unterminated"}, work).action == gate.CONFIRM
    assert gate.judge("bash", {"command": "/p/.ideate/bin/ideate-loop --until true"}, work).action == gate.CONFIRM


# 7. prompt budget ----------------------------------------------------------------------------

def approx_tokens(text: str) -> int:
    return -(-len(text) * 2 // 7)          # characters / 3.5, rounded up (a deliberately high count)


def test_the_prompt_line_for_loops_fits_its_budget():
    lines = [l for l in harness.SYSTEM_PROMPT.splitlines() if "ideate-loop" in l]
    assert len(lines) == 1
    assert approx_tokens(lines[0]) <= config.LOOP_PROMPT_TOKENS
    assert "--until" in lines[0] and "--max" in lines[0]
    for kind in harness.KIND_PROMPTS:
        total = approx_tokens(harness.system_prompt(kind))
        # System prompt + the four tool schemas (about 450 tokens measured) leave room for the work.
        assert total < 0.30 * config.PROMPT_BUDGET_TOKENS, (kind, total)


def test_tools_are_unchanged():
    assert config.TOOLS == ("read", "write", "edit", "bash")
