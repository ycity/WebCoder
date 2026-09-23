"""Run a tool (ssh, adb, hdc) without letting what it prints exhaust this computer's memory or disk.

Every tool that runs is tracked, so that when the hub revokes this device (disabled, deleted or re-paired in
the panel) the agent can stop them at once with stop_all() instead of letting them run to their timeout.
"""
from __future__ import annotations

import contextlib
import subprocess
import threading

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # the agent runs windowless (pythonw); so must its tools
CHUNK = 64 * 1024
ERR_LIMIT = 1024 * 1024
GRACE = 5.0  # after the tool exits: a helper it started may still hold the pipe open

_running: dict[subprocess.Popen, bool] = {}  # process -> stopped by stop_all()
_lock = threading.Lock()


class Stopped(Exception):
    """The tool was killed by stop_all(): the hub revoked this device."""


class OutputLost(Exception):
    """What the tool printed could not all be written to the sink (e.g. the disk is full); the tool was stopped."""


def stop_all() -> int:
    """Kill every tool still running; returns how many there were."""
    with _lock:
        victims = list(_running)
        for process in victims:
            _running[process] = True
    for process in victims:
        with contextlib.suppress(OSError):
            process.kill()
    return len(victims)


def running() -> int:
    with _lock:
        return len(_running)


class _Drain(threading.Thread):
    """Reads a pipe to its end, keeping at most `limit` bytes: the first ones, or the last ones with tail=True.
    With a `sink` (an open file) the bytes go there instead, and passing `limit` calls `over()` and stops."""

    def __init__(self, stream, limit: int, *, tail: bool = False, sink=None, over=None):
        super().__init__(daemon=True)
        self.stream, self.limit, self.tail, self.sink, self.over = stream, limit, tail, sink, over
        self.data = bytearray()
        self.size = 0
        self.error: Exception | None = None
        self.start()

    def run(self) -> None:
        with self.stream:
            while chunk := self.stream.read1(CHUNK):
                self.size += len(chunk)
                if self.sink is not None:
                    if self.size > self.limit:
                        self.over()
                        return
                    try:
                        self.sink.write(chunk)
                    except Exception as exc:  # noqa: BLE001 - the caller must not use a partly written file
                        self.error = exc
                        self.over()
                        return
                    continue
                self.data += chunk
                if not self.tail:
                    del self.data[self.limit:]  # keep reading, so the tool never blocks on a full pipe
                elif len(self.data) > 2 * self.limit:  # trim now and then, not on every chunk
                    del self.data[:-self.limit]

    @property
    def cut(self) -> bool:
        return self.size > self.limit

    def value(self) -> bytes:
        return bytes(self.data[-self.limit:] if self.tail else self.data)


def run_quiet(argv: list[str], timeout: float) -> int:
    """Run a tool whose output is not wanted, with no pipes at all (adb start-server leaves a daemon that would
    hold one open), still stoppable by stop_all(). Returns the exit code; raises like run_capped."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    with _lock:
        _running[process] = False
    try:
        returncode = process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        with _lock:
            stopped = _running.pop(process, False)
    if stopped:
        raise Stopped(argv[0])
    return returncode


def run_capped(argv: list[str], timeout: float, limit: int, *, tail: bool = False, stdin=subprocess.DEVNULL,
               sink=None, cwd=None, env=None) -> subprocess.CompletedProcess:
    """Like subprocess.run(capture_output=True), but stdout keeps at most `limit` bytes (the first ones, or the
    last with tail=True) and stderr its last ERR_LIMIT bytes. With `sink`, stdout is written to that file
    instead and the tool is stopped once it passes `limit` bytes. `.stdout_cut` / `.stderr_cut` tell whether
    anything was dropped, `.stdout_size` how much the tool printed. Raises what subprocess.run raises
    (FileNotFoundError, subprocess.TimeoutExpired; on a timeout the tool is killed first), Stopped, and
    OutputLost when the sink could not be written."""
    process = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=NO_WINDOW, cwd=cwd, env=env)
    with _lock:
        _running[process] = False
    out = _Drain(process.stdout, limit, tail=tail, sink=sink, over=process.kill)
    err = _Drain(process.stderr, ERR_LIMIT, tail=True)
    try:
        returncode = process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        with _lock:
            stopped = _running.pop(process, False)
        out.join(GRACE)
        err.join(GRACE)
    if stopped:
        raise Stopped(argv[0])
    if out.error is not None:
        raise OutputLost(f"{type(out.error).__name__}: {out.error}") from out.error
    result = subprocess.CompletedProcess(argv, returncode, out.value(), err.value())
    result.stdout_cut, result.stderr_cut, result.stdout_size = out.cut, err.cut, out.size
    return result
