"""Stand-in for the ssh client in tests: `python fake_ssh.py <ssh options> -- <host> <command>`.

Logs every call to $WEBCODER_FAKE_SSH/calls.jsonl. The agent's own steps (`sh -c '<script>'`) run in a real
POSIX shell ($WEBCODER_FAKE_SH) with every absolute path in them moved under $WEBCODER_FAKE_SSH/servers/<host>,
so the tests exercise the real scripts; a few plain commands the tests type are answered here.
Hosts with special behaviour: unreachable, needs-password (connection errors), racy (someone creates the
file right after the agent checked it), flaky (the connection drops half way through an upload).
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ["WEBCODER_FAKE_SSH"])
QUOTED = re.compile(r"'(/[^']*)'")  # an absolute path, as remote_quote writes it


def say(text: str) -> None:  # a server's output is LF, whatever this machine uses
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")


def agent_step(host: str, script: str, server: Path) -> int:
    paths = [server / match[1].lstrip("/") for match in QUOTED.finditer(script)]
    for path in paths:  # the server's folders exist
        path.parent.mkdir(parents=True, exist_ok=True)
    local = QUOTED.sub(lambda match: "'" + (server / match[1].lstrip("/")).as_posix() + "'", script)
    data = sys.stdin.buffer.read()
    if host == "flaky":  # without a TTY, a dropped connection just ends stdin
        data = data[: len(data) // 2]
    done = subprocess.run([os.environ["WEBCODER_FAKE_SH"], "-c", local], input=data, capture_output=True)
    sys.stdout.buffer.write(done.stdout)
    sys.stderr.buffer.write(done.stderr)
    if host == "racy" and script.startswith("if [ -e "):  # someone else creates the file right after the check
        paths[0].write_bytes(b"theirs\n")
    return done.returncode


def main(argv: list[str]) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "calls.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(argv, ensure_ascii=False) + "\n")
    host, command = argv[argv.index("--") + 1], argv[argv.index("--") + 2]
    if host == "unreachable":
        sys.stderr.buffer.write(b"ssh: Could not resolve hostname unreachable: No such host is known.\n")
        return 255
    if host == "needs-password":
        sys.stderr.buffer.write(b"needs-password: Permission denied (publickey,password).\n")
        return 255
    words = shlex.split(command)
    if words[:2] == ["sh", "-c"] and len(words) == 3:
        return agent_step(host, words[2], STATE / "servers" / host)
    if words[:1] == ["echo"]:
        say(" ".join(words[1:]))
    elif words[:1] == ["hostname"]:
        say(host)
    elif words[:1] == ["spam"]:  # a command that prints a lot: `spam <bytes>`, ending in END
        left = int(words[1]) - 4
        while left > 0:
            sys.stdout.buffer.write(b"x" * min(left, 65536))
            left -= 65536
        sys.stdout.buffer.write(b"END\n")
    elif words[:1] == ["sleep"]:
        time.sleep(float(words[1]))
    elif words[:1] == ["fail"]:
        sys.stderr.buffer.write(b"boom\n")
        return int(words[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
