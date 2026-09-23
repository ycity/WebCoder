"""run_capped: a tool's output is capped while it arrives (ssh, adb and hdc all go through it)."""
import subprocess
import sys
import threading
import time

import pytest

from webcoder.agent.procs import OutputLost, Stopped, run_capped, run_quiet, running, stop_all

PRINT = ("import sys\nfor _ in range(64): sys.stdout.buffer.write(b'x' * 65536)\n"
         "sys.stdout.buffer.write(b'END'); sys.stderr.buffer.write(b'e' * 3000000 + b'DONE')")  # 4 MiB, 3 MB


def test_keeps_the_first_or_the_last_bytes():
    head = run_capped([sys.executable, "-c", PRINT], 60, 1000)
    assert head.returncode == 0 and head.stdout == b"x" * 1000 and head.stdout_cut
    assert head.stdout_size == 64 * 65536 + 3
    assert head.stderr.endswith(b"eDONE") and len(head.stderr) == 1024 * 1024 and head.stderr_cut
    tail = run_capped([sys.executable, "-c", PRINT], 60, 1000, tail=True)
    assert tail.stdout == b"x" * 997 + b"END" and tail.stdout_cut
    small = run_capped([sys.executable, "-c", "print('hi', end='')"], 60, 1000)
    assert small.stdout == b"hi" and not small.stdout_cut and not small.stderr_cut


def test_a_sink_gets_everything_up_to_the_limit(tmp_path):
    target = tmp_path / "out.bin"
    with open(target, "wb") as sink:
        ok = run_capped([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'y' * 300000)"], 60,
                        400000, sink=sink)
    assert not ok.stdout_cut and target.read_bytes() == b"y" * 300000
    with open(target, "wb") as sink:  # passing the limit stops the tool instead of filling the disk
        over = run_capped([sys.executable, "-c", "import sys, time\nwhile True: sys.stdout.buffer.write(b'z' * 65536)"],
                          60, 100000, sink=sink)
    assert over.stdout_cut and target.stat().st_size <= 100000


class FullDisk:
    def __init__(self):
        self.written = 0

    def write(self, data):
        if self.written >= 100000:
            raise OSError(28, "No space left on device")
        self.written += len(data)


def test_a_sink_that_cannot_be_written_stops_the_tool():
    sink = FullDisk()
    with pytest.raises(OutputLost) as err:  # never a result that looks complete
        run_capped([sys.executable, "-c", "import sys\nwhile True: sys.stdout.buffer.write(b'z' * 65536)"], 60,
                   10 ** 9, sink=sink)
    assert "No space left" in str(err.value) and running() == 0


def test_a_tool_run_without_pipes_can_be_stopped_too():
    """adb start-server runs without pipes (its daemon would hold them): revoking the device still stops it."""
    outcome = {}

    def start():
        try:
            outcome["exit"] = run_quiet([sys.executable, "-c", "import time; time.sleep(30)"], 60)
        except Stopped:
            outcome["stopped"] = True
    thread = threading.Thread(target=start)
    thread.start()
    deadline = time.monotonic() + 10
    while running() == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert stop_all() == 1
    thread.join(10)
    assert outcome == {"stopped": True} and running() == 0
    assert run_quiet([sys.executable, "-c", "raise SystemExit(3)"], 60) == 3


def test_a_timeout_kills_the_tool():
    with pytest.raises(subprocess.TimeoutExpired):
        run_capped([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"], 1, 1000)
