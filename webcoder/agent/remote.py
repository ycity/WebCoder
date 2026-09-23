"""Run commands on the owner's servers over SSH, from the computer that hosts the projects.

Uses the system OpenSSH client with the owner's own ~/.ssh/config, keys and known_hosts, so server
credentials never reach the hub. Only hosts the owner listed here (``webcoder-agent ssh-hosts set``) can be
reached; the hub cannot change that list. Connections are non-interactive (no password prompts, no TTY,
no port forwarding, no LocalCommand). What the owner's ssh config does to reach a listed host (ProxyCommand,
ProxyJump, Match exec) runs as it does for the owner's own ssh: it is the owner's file, not the AI's.
"""
from __future__ import annotations

import inspect
import os
import re
import secrets
import shutil
import stat
import subprocess
import time

from webcoder.agent.procs import OutputLost, Stopped, run_capped
from webcoder.common.errors import WebCoderError

HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@:\[\]-]{0,252}$")  # never starts with "-": no option injection
MODE_RE = re.compile(r"^0?[0-7]{3}$")  # plain permissions: no setuid / setgid / sticky bits
MAX_OUTPUT = 200_000  # characters of stdout / stderr returned (the last ones)
MAX_CAPTURE = 1024 * 1024  # bytes of stdout kept in memory while a command runs (enough for MAX_OUTPUT)
MAX_FILE = 2 * 1024 * 1024 * 1024
_CONTINUATION = bytes(range(0x80, 0xC0))  # UTF-8 continuation bytes
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=4", "-o", "RequestTTY=no", "-o", "ClearAllForwardings=yes",
               "-o", "PermitLocalCommand=no", "-o", "RemoteCommand=none", "-T"]


def valid_host(value) -> str:
    if not isinstance(value, str) or not HOST_RE.match(value):
        raise WebCoderError("INVALID_ARGUMENT", "host must be an SSH host name or alias (letters, digits, . _ - @ :), "
                            "not starting with '-'")
    return value


def remote_quote(value: str) -> str:
    """Quote a path for the server's POSIX shell."""
    return "'" + value.replace("'", "'\\''") + "'"


def _sh(script: str) -> str:
    """The agent's own steps run under sh, whatever the account's login shell is (fish, csh...)."""
    return "sh -c " + remote_quote(script)


def exists_script(path: str) -> str:
    """Prints WEBCODER_EXISTS or WEBCODER_MISSING. A dangling symlink counts: writing to it would create its target."""
    quoted = remote_quote(path)
    return f"if [ -e {quoted} ] || [ -L {quoted} ]; then echo WEBCODER_EXISTS; else echo WEBCODER_MISSING; fi"


def upload_script(remote: str, stem: str, size: int, overwrite: bool, mode: int = 0o666, exact: bool = False) -> str:
    """Reads the file from stdin into a private temporary copy (`stem`.tmp, next to `remote`), then puts it
    in place. Exit code 0 on success; otherwise a WEBCODER_* marker on stderr says what happened.

    The target is only touched once the copy has exactly `size` bytes (WEBCODER_INCOMPLETE otherwise): without a
    TTY, a dropped connection just ends stdin, so `cat` would "succeed" with part of the file. The content
    then goes into the target with `cat`, which keeps an existing file's owner, group and mode (a rename would
    give a 0600 secrets file, or a config a service reads through its group, new ones).
    - A new target is created private (umask 077) and only then given its mode: `mode` exactly if `exact`,
      else `mode` limited by the server's umask (like scp). Nobody else can read it in between.
    - A new target is created empty and exclusively first (noclobber, O_EXCL): an existing file, or a dangling
      symlink, is refused even if it appeared a moment ago, and never touched. Only then is it filled; if that
      fails (disk full), the half-written file, which is ours, is removed (WEBCODER_REMOVED).
    - With `overwrite`, an existing file is first copied to a private backup (`stem`.bak); if that fails (disk
      full) nothing is touched (WEBCODER_NO_BACKUP). If writing the target fails, the backup is written back
      (WEBCODER_RESTORED); if even that fails, the backup stays on the server (WEBCODER_BACKUP_KEPT). With an
      `exact` mode, the file gets that mode too.
    The temporary copy is always removed."""
    target, copy, backup = remote_quote(remote), remote_quote(stem + ".tmp"), remote_quote(stem + ".bak")
    octal = f"{mode & 0o777:o}"
    final = octal if exact else f"$(printf %o $(( 0{octal} & ~0$u )))"
    staged = f"(umask 077 && cat > {copy}) && [ $(wc -c < {copy}) -eq {int(size)} ]"
    create = (f"if (set -C; umask 077 && : > {target}); then "
              f"if cat {copy} >| {target} && chmod {final} {target}; then rc=0; "
              f"else rm -f {target}; echo WEBCODER_REMOVED >&2; rc=1; fi; else rc=1; fi")
    if overwrite:
        restore = (f"if cat {backup} > {target}; then rm -f {backup}; echo WEBCODER_RESTORED >&2; rc=1; "
                   "else echo WEBCODER_BACKUP_KEPT >&2; rc=1; fi")
        # The backup goes only once everything worked, the mode too: a mode that cannot be set undoes the upload.
        written = (f"if chmod {octal} {target}; then rm -f {backup}; rc=0; else {restore}; fi" if exact
                   else f"rm -f {backup}; rc=0")
        place = (f"if [ ! -f {target} ]; then {create}; "
                 f"elif ! (umask 077 && cp {target} {backup}); then rm -f {backup}; echo WEBCODER_NO_BACKUP >&2; rc=1; "
                 f"elif cat {copy} > {target}; then {written}; "
                 f"else {restore}; fi")
    else:
        place = create
    return (("" if overwrite else "set -C; ") + "u=$(umask); "
            + f"if {staged}; then {place}; else echo WEBCODER_INCOMPLETE >&2; rc=1; fi; rm -f {copy}; exit $rc")


def _remote_path(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ch in value for ch in "\x00\r\n"):
        raise WebCoderError("INVALID_ARGUMENT", "remote_path must be a path on the server, e.g. /etc/nginx/nginx.conf")
    return "./" + value if value.startswith("-") else value  # never an option to cat, cp or chmod ("-" is stdin)


def locate_ssh(override=None) -> list[str] | None:
    if override:
        return [str(part) for part in override] if isinstance(override, list) else [str(override)]
    found = shutil.which("ssh")
    if found:
        return [found]
    windows = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
    return [windows] if os.name == "nt" and os.path.isfile(windows) else None


def _tail(data: bytes, cut: bool) -> tuple[str, bool]:
    """The last MAX_OUTPUT characters; `cut` says the bytes are already the end of a longer output."""
    text = (data.lstrip(_CONTINUATION) if cut else data).decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT:
        return "…" + text[-MAX_OUTPUT:], True
    return ("…" + text, True) if cut else (text, False)


class SshOps:
    """The ssh.* operations the hub may request (only for hosts the owner listed on this computer)."""

    OPERATIONS = {"ssh.hosts": "hosts", "ssh.exec": "exec_command", "ssh.upload": "upload", "ssh.download": "download"}

    def __init__(self, files, *, hosts=None, command=None):
        self.files = files  # FileOps: the project sandbox for uploads and downloads
        self.allowed = [host for host in (hosts or []) if isinstance(host, str) and HOST_RE.match(host)]
        self.command = command

    def status(self) -> dict:
        return {"hosts": list(self.allowed), "client": locate_ssh(self.command) is not None}

    def dispatch(self, op: str, args: dict) -> dict:
        if not self.allowed:
            raise WebCoderError("SSH_DISABLED", "No servers are allowed on this computer; its owner can list them in "
                                "the WebCoder panel (设备 → 修改设置) or run: webcoder-agent ssh-hosts set <host>...")
        # "The AI can look, not change": no server command can be told to be only a read, and transfers write.
        if op != "ssh.hosts" and self.files.sb.read_only:
            raise WebCoderError("AGENT_READ_ONLY", "The agent on this device is configured read-only, and a server "
                                "command or file transfer could change things, so none are run")
        method = getattr(self, self.OPERATIONS[op])
        try:
            inspect.signature(method).bind(**args)
        except TypeError as exc:
            raise WebCoderError("INVALID_ARGUMENT", f"Invalid arguments for {op}: {exc}") from None
        return method(**args)

    def _argv(self, host, remote_command: str) -> list[str]:
        host = valid_host(host)
        if host not in self.allowed:
            raise WebCoderError("SSH_HOST_NOT_ALLOWED", f"'{host}' is not among the servers allowed on this computer: "
                                + ", ".join(self.allowed))
        ssh = locate_ssh(self.command)
        if ssh is None:
            raise WebCoderError("SSH_CLIENT_MISSING", "No ssh client was found on this computer (install OpenSSH)")
        return [*ssh, *SSH_OPTIONS, "--", host, remote_command]

    def _run(self, argv: list[str], timeout: float, *, stdin=subprocess.DEVNULL, sink=None, limit=MAX_CAPTURE):
        try:  # output is capped while it arrives: `cat` of a huge log must not fill this computer's memory
            return run_capped(argv, timeout, limit, tail=True, stdin=stdin, sink=sink)
        except FileNotFoundError:
            raise WebCoderError("SSH_CLIENT_MISSING", f"{argv[0]} was not found on this computer") from None
        except subprocess.TimeoutExpired:
            raise WebCoderError("SSH_TIMEOUT", f"No result within {timeout:.0f}s; the command may still be running on "
                                "the server (use nohup/systemd for long jobs)") from None
        except Stopped:
            raise WebCoderError("STOPPED", "Stopped: this device was disabled or deleted in the hub panel") from None
        except OutputLost as exc:
            raise WebCoderError("WRITE_FAILED",
                                f"Could not save what the server sent on this computer: {exc}") from None

    @staticmethod
    def _connection_error(result, host: str) -> None:
        if result.returncode == 255:  # ssh's own failure (the remote command's exit codes are passed through)
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-600:]
            hint = (" Connect once by hand so the host key is known, and make sure a key (or ssh-agent) is set up: "
                    "passwords cannot be typed here." if "verification" in detail or "ermission" in detail else "")
            raise WebCoderError("SSH_CONNECTION_FAILED", f"Could not run the command on {host}: {detail}{hint}")

    def hosts(self) -> dict:
        return self.status()

    def exec_command(self, host, command, timeout=60) -> dict:
        if not isinstance(command, str) or not command.strip() or len(command) > 20_000 or "\x00" in command:
            raise WebCoderError("INVALID_ARGUMENT", "command must be a non-empty shell command")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600:
            raise WebCoderError("INVALID_ARGUMENT", "timeout must be 1-600 seconds")
        argv = self._argv(host, command)
        started = time.monotonic()
        result = self._run(argv, timeout)
        self._connection_error(result, host)
        stdout, cut_out = _tail(result.stdout, result.stdout_cut)
        stderr, cut_err = _tail(result.stderr, result.stderr_cut)
        return {"host": host, "exit_code": result.returncode, "stdout": stdout, "stderr": stderr,
                "truncated": cut_out or cut_err, "duration_ms": int((time.monotonic() - started) * 1000)}

    def _exists(self, host: str, path: str) -> bool:
        argv = self._argv(host, _sh(exists_script(path)))
        result = self._run(argv, 60)
        self._connection_error(result, host)
        out = result.stdout.decode("utf-8", errors="replace")
        if "WEBCODER_EXISTS" in out or "WEBCODER_MISSING" in out:
            return "WEBCODER_EXISTS" in out
        raise WebCoderError("SSH_COMMAND_FAILED", f"Could not check {path} on {host}: {out.strip()[-300:]}")

    def upload(self, host, root, path, remote_path, overwrite=False, mode=None) -> dict:
        remote = _remote_path(remote_path)
        if mode is not None and (not isinstance(mode, str) or not MODE_RE.match(mode)):
            raise WebCoderError("INVALID_ARGUMENT", "mode must be octal permissions such as 600, 644 or 755")
        local, size, display = self.files.local_file(root, path, MAX_FILE)
        if overwrite is not True and self._exists(host, remote):  # the server's file has no backup here
            raise WebCoderError("DESTINATION_EXISTS", f"{remote} already exists on {host}; choose another path, or "
                                "pass overwrite=true to replace it")
        # A new file's permissions: as asked; else the local file's (like scp; Windows has none to give).
        permissions = int(mode, 8) if mode else 0o666 if os.name == "nt" else stat.S_IMODE(os.stat(local).st_mode)
        stem = f"{remote}.webcoder-{secrets.token_hex(4)}"
        argv = self._argv(host, _sh(upload_script(remote, stem, size, overwrite is True, permissions, bool(mode))))
        with open(local, "rb") as source:
            result = self._run(argv, 600, stdin=source)
        self._connection_error(result, host)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-600:]
            markers = {"WEBCODER_INCOMPLETE": f"the transfer did not complete; {remote} was not changed",
                       "WEBCODER_REMOVED": f"writing the new file {remote} failed (disk full?); what was written of "
                                           "it was removed",
                       "WEBCODER_NO_BACKUP": f"{remote} could not be backed up first (disk full?), so it was not "
                                             "changed",
                       "WEBCODER_RESTORED": f"writing {remote} failed; its previous content was restored",
                       "WEBCODER_BACKUP_KEPT": f"writing {remote} failed, and so did restoring it: its previous "
                                               f"content is saved on the server as {stem}.bak"}
            for marker, outcome in markers.items():
                if marker in detail:
                    reason = detail.replace(marker, "").strip()
                    raise WebCoderError("SSH_COMMAND_FAILED", f"Uploading to {host}: {outcome}"
                                        + (f" ({reason})" if reason else ""))
            if overwrite is not True and "exist" in detail.lower():
                raise WebCoderError("DESTINATION_EXISTS", f"{remote} already exists on {host}; choose another path, "
                                    "or pass overwrite=true to replace it")
            raise WebCoderError("SSH_COMMAND_FAILED", f"Writing {remote} on {host} failed: {detail}")
        return {"host": host, "path": display, "remote_path": remote, "size": size, "overwrote": overwrite is True}

    def download(self, host, remote_path, root, path) -> dict:
        remote = _remote_path(remote_path)
        argv = self._argv(host, _sh(f"cat {remote_quote(remote)}"))
        with self.files.incoming_file(root, path, MAX_FILE) as incoming:
            with open(incoming.temp, "wb") as target:
                result = self._run(argv, 600, sink=target, limit=MAX_FILE)  # stopped once over the limit
            if result.stdout_cut:
                raise WebCoderError("FILE_TOO_LARGE", f"{remote} on {host} is larger than {MAX_FILE:,} bytes")
            self._connection_error(result, host)
            if result.returncode != 0:
                raise WebCoderError("SSH_COMMAND_FAILED", f"Reading {remote} on {host} failed: "
                                    f"{result.stderr.decode('utf-8', errors='replace').strip()[-600:]}")
            if os.path.getsize(incoming.temp) != result.stdout_size:  # all that arrived is in the file
                raise WebCoderError("WRITE_FAILED", f"Could not save all of {remote} on this computer")
        return {"host": host, "remote_path": remote, **incoming.result}
