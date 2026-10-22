"""Owned fixture cleanup stays bounded and never masks signal failures."""
import importlib.util
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from unittest.mock import Mock, call, patch

import pytest

SUPPORT = Path(__file__).resolve().parents[1] / "build-support"
sys.path.insert(0, str(SUPPORT))
spec = importlib.util.spec_from_file_location("fixture_cleanup_gate", SUPPORT / "native_peer_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def fixture_mock(communications):
    process = Mock(pid=43210, returncode=0)
    process.communicate.side_effect = communications
    return process


def timeout():
    return subprocess.TimeoutExpired(["synthetic-fixture"], 1)


def test_timeout_never_sends_a_second_kill():
    process = fixture_mock([timeout(), timeout(), ("", "")])
    # CI rejected the redundant KILL after the first one had already succeeded.
    with patch.object(gate.subprocess, "Popen", return_value=process) as launch, patch.object(
        gate.os, "killpg", side_effect=[None, None, PermissionError("redundant kill")]
    ) as kill:
        result = gate.fixture_process(["synthetic-fixture"], timeout=3)
    assert result.returncode == 124
    assert result.stderr == "fixture process timeout"
    launch.assert_called_once_with(["synthetic-fixture"], start_new_session=True)
    assert kill.call_args_list == [call(43210, signal.SIGTERM), call(43210, signal.SIGKILL)]
    assert process.communicate.call_args_list == [call(timeout=3), call(timeout=2), call(timeout=5)]
    process.wait.assert_called_once_with(timeout=5)


def test_success_still_kills_descriptor_detached_descendants():
    process = fixture_mock([("answer", "diagnostic"), ("", "")])
    with patch.object(gate.subprocess, "Popen", return_value=process), patch.object(gate.os, "killpg") as kill:
        result = gate.fixture_process(["synthetic-fixture"], timeout=3)
    assert (result.returncode, result.stdout, result.stderr) == (0, "answer", "diagnostic")
    assert kill.call_args_list == [call(43210, signal.SIGTERM), call(43210, signal.SIGKILL)]
    process.wait.assert_called_once_with(timeout=5)


@pytest.mark.parametrize("denied_signal", [signal.SIGTERM, signal.SIGKILL])
def test_first_signal_permission_failure_propagates(denied_signal):
    process = fixture_mock([timeout(), timeout()])
    def signal_group(group, kind):
        if kind == denied_signal:
            raise PermissionError("signal denied")
    with patch.object(gate.subprocess, "Popen", return_value=process), patch.object(
        gate.os, "killpg", side_effect=signal_group
    ) as kill, pytest.raises(PermissionError, match="signal denied"):
        gate.fixture_process(["synthetic-fixture"], timeout=3)
    assert kill.call_args_list.count(call(43210, denied_signal)) == 1


def test_empty_process_group_is_tolerated():
    process = fixture_mock([("answer", ""), ("", "")])
    with patch.object(gate.subprocess, "Popen", return_value=process), patch.object(
        gate.os, "killpg", side_effect=ProcessLookupError
    ):
        assert gate.fixture_process(["synthetic-fixture"], timeout=3).returncode == 0
    process.wait.assert_called_once_with(timeout=5)


def test_post_kill_drain_timeout_propagates_without_another_kill():
    process = fixture_mock([timeout(), timeout(), timeout()])
    with patch.object(gate.subprocess, "Popen", return_value=process), patch.object(
        gate.os, "killpg"
    ) as kill, pytest.raises(subprocess.TimeoutExpired):
        gate.fixture_process(["synthetic-fixture"], timeout=3)
    assert kill.call_args_list == [call(43210, signal.SIGTERM), call(43210, signal.SIGKILL)]
    assert process.communicate.call_args_list[-1] == call(timeout=5)


def test_unexpected_grace_drain_error_still_kills_and_propagates():
    process = fixture_mock([timeout(), OSError("drain failed")])
    with patch.object(gate.subprocess, "Popen", return_value=process), patch.object(
        gate.os, "killpg"
    ) as kill, pytest.raises(OSError, match="drain failed"):
        gate.fixture_process(["synthetic-fixture"], timeout=3)
    assert kill.call_args_list == [call(43210, signal.SIGTERM), call(43210, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="owned POSIX process-group fixture")
def test_successful_leader_does_not_leave_a_detached_pipe_writer(tmp_path):
    reader, writer = os.pipe()
    ready = tmp_path / "ready"
    child = (
        "import os,signal,time,sys;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "os.write(int(sys.argv[1]),b'ready');Path(sys.argv[2]).touch();time.sleep(5)"
    )
    parent = (
        "import subprocess,sys,time;from pathlib import Path;"
        "subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],sys.argv[2],sys.argv[3]],"
        "pass_fds=(int(sys.argv[2]),),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "deadline=time.monotonic()+3\n"
        "while not Path(sys.argv[3]).exists():\n"
        " if time.monotonic()>deadline: raise RuntimeError('child not ready')\n"
        " time.sleep(.01)\n"
        "print('leader finished')"
    )
    try:
        result = gate.fixture_process(
            [sys.executable, "-I", "-c", parent, child, str(writer), str(ready)], timeout=4,
            pass_fds=(writer,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "leader finished"
        os.close(writer)
        writer = None
        assert select.select([reader], [], [], 1)[0]
        assert os.read(reader, 5) == b"ready"
        assert select.select([reader], [], [], 1)[0]
        assert os.read(reader, 1) == b""
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)
