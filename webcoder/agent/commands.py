"""Command-line programs the owner allowed on this computer (e.g. lark-cli, meegle), for the hub's cli_* tools.

Only programs listed here (``webcoder-agent commands set lark-cli meegle``) can run; the hub cannot add any.
Shells, interpreters, launchers and the well-known programs that run other commands or reach any file can never
be listed: allowing one would allow any command. No list names every such program, so the rest is the owner's
call: an allowed program can do whatever its own options do. A program runs with an argument list, never
through a shell. On Windows an npm command's .cmd wrapper is not run through
cmd.exe, whose parsing of arguments can be abused to run other commands; its JavaScript entry point is
started with node directly. Programs run with the owner's account and environment (their own logins),
without a console to prompt on, and are stopped with the agent's other tools when the hub revokes the device.
"""
from __future__ import annotations

import inspect
import locale
import os
import re
import shutil
import subprocess
import tempfile
import time

from webcoder.agent.fileops import is_within
from webcoder.agent.procs import Stopped, run_capped
from webcoder.common.errors import WebCoderError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
# Shells, interpreters and launchers: each of them runs any command it is given.
REFUSED = frozenset({
    "cmd", "command", "powershell", "powershell_ise", "pwsh", "bash", "sh", "dash", "zsh", "fish", "ksh", "csh",
    "tcsh", "nu", "wsl", "busybox", "python", "python3", "pythonw", "py", "pypy", "node", "nodejs", "deno", "bun",
    "ruby", "irb", "perl", "php", "java", "javaw", "jshell", "lua", "tclsh", "wish", "osascript", "wscript",
    "cscript", "mshta", "rundll32", "regsvr32", "msiexec", "installutil", "reg", "regedit", "schtasks", "sc", "at",
    "start", "explorer", "open", "xdg-open", "env", "sudo", "su", "doas", "runas", "gsudo", "xargs", "find",
    "nohup", "timeout", "watch", "npx", "npm", "pnpm", "yarn", "corepack", "pip", "pip3", "pipx", "uv", "uvx",
    "make", "docker", "podman", "vim", "vi", "nvim", "emacs", "nano", "less", "more", "man",
    # Programs whose options run other commands (git -c alias.x=!cmd, hg --config alias.x=!cmd, ssh -o
    # ProxyCommand=, tar --to-command, zip -TT, rsync -e, awk system(), GNU sed's e command ...) or reach any
    # local file (adb / hdc push and pull). git, ssh and phones have their own tools with fixed arguments.
    "git", "ssh", "scp", "sftp", "rsync", "adb", "hdc", "tar", "bsdtar", "zip", "awk", "gawk", "mawk", "nawk",
    "sed", "gsed", "perl5", "gh", "hub", "cargo", "go", "dotnet", "mvn", "gradle", "gradlew", "ant", "rake",
    "gem", "bundle", "composer", "rscript", "r", "julia", "swift", "kotlin", "scala", "groovy", "cmake", "ninja",
    "msbuild", "crontab", "launchctl", "systemctl", "service", "code", "cursor",
    "hg", "svn", "fossil", "bzr", "darcs",
    # File transfer: they send any local file (curl -d @file, wget --post-file) or write downloads anywhere.
    "curl", "wget", "wget2", "aria2c", "http", "https", "xh", "rclone", "ftp", "tftp", "lftp", "nc", "ncat",
    "netcat", "socat", "telnet", "certutil", "bitsadmin",
    # They run the command they are given (nice cmd, strace cmd, poetry run cmd, tmux new cmd ...).
    "nice", "ionice", "chroot", "stdbuf", "taskset", "setsid", "script", "unshare", "nsenter", "flock", "parallel",
    "time", "strace", "ltrace", "gdb", "lldb", "valgrind", "expect", "tmux", "screen", "caffeinate", "arch",
    "pkexec", "firejail", "proxychains", "proxychains4", "torsocks", "hyperfine", "entr", "nodemon", "pm2",
    "cross-env", "concurrently", "tsx", "ts-node", "wt", "conhost", "forfiles", "pcalua", "wmic", "cmstp", "msdt",
    "hh", "poetry", "pdm", "hatch", "pipenv", "rye", "conda", "mamba", "micromamba", "nix", "nix-shell",
    # Package managers, task and test runners: they install or run code, including code in the project.
    "winget", "choco", "scoop", "brew", "apt", "apt-get", "dnf", "yum", "pacman", "snap", "flatpak", "vcpkg",
    "conan", "just", "task", "invoke", "nox", "tox", "pytest", "jest", "vitest", "mocha", "bunx", "pnpx",
    "yarnpkg", "nx", "turbo", "lerna", "bazel", "sbt", "mix", "stack", "cabal", "terraform", "pulumi",
    "ansible", "ansible-playbook",
    # Database shells and editors with a shell escape (sqlite3 .shell, psql \!, mysql system, ed !cmd).
    "sqlite3", "psql", "mysql", "mariadb", "mongosh", "mongo", "ed", "ex", "view", "vimdiff", "micro"})
MAX_OUTPUT = 200_000  # characters of stdout / stderr returned
MAX_CAPTURE = 4 * 1024 * 1024  # bytes of stdout kept while a program runs
MAX_STDIN = 1024 * 1024
_NPM_SHIM = re.compile(r'"%dp0%\\(?P<script>[^"%\r\n]+\.[cm]?js)"\s+%\*')  # what npm writes into a .cmd wrapper


def decode(data: bytes) -> str:
    """Programs print UTF-8 (node, Go, most CLIs) or, on Windows, often the console's code page (e.g. GBK)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start >= len(data) - 3:  # only a character cut off at the end (the output was capped)
            return data.decode("utf-8", errors="replace")
        return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


def refused(name: str) -> bool:
    base = os.path.basename(name).lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        base = base.removesuffix(suffix)
    return base in REFUSED or re.sub(r"[\d.]+$", "", base) in REFUSED  # python3.12, node20 ...


def check_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise WebCoderError("INVALID_ARGUMENT", "A program is given by its command name, e.g. lark-cli (letters, "
                            "digits, . _ + -; no paths)")
    if refused(name):
        raise WebCoderError("CLI_REFUSED", f"{name} can run other commands or reach any file on this computer (a "
                            "shell, interpreter, launcher, package manager, file-transfer or version-control tool "
                            "...), so it cannot be allowed")
    return name


def npm_entry(wrapper: str) -> list[str] | None:
    """[node, script] for an npm-generated .cmd wrapper, so that it runs without cmd.exe."""
    try:
        with open(wrapper, encoding="utf-8", errors="replace") as handle:
            match = _NPM_SHIM.search(handle.read(16 * 1024))
    except OSError:
        return None
    if match is None:
        return None
    folder = os.path.dirname(os.path.abspath(wrapper))
    script = os.path.normpath(os.path.join(folder, match["script"]))
    if not os.path.isfile(script):
        return None
    bundled = os.path.join(folder, "node.exe")  # the wrapper's own choice: node.exe next to it, else PATH
    node = bundled if os.path.isfile(bundled) else shutil.which("node")
    return [node, script] if node else None


class CommandOps:
    """The cli.* operations the hub may request (only for programs the owner listed on this computer)."""

    OPERATIONS = {"cli.run": "run"}

    def __init__(self, files, *, programs=None, paths=None):
        self.files = files  # FileOps: projects a program may run in
        self.allowed = [name for name in (programs or []) if isinstance(name, str) and NAME_RE.match(name)
                        and not refused(name)]
        self.paths = dict(paths or {})  # name -> path (or argument list) when a program is not on PATH

    def _configured(self, name: str) -> list[str]:
        """The program as configured: its command_paths entry (a path or an argument list), else what PATH finds."""
        override = self.paths.get(name)
        if override:
            return [str(part) for part in override] if isinstance(override, list) else [str(override)]
        found = shutil.which(name)
        if found is None:
            raise WebCoderError("CLI_MISSING", f"{name} was not found on this computer's PATH")
        return [found]

    def resolve(self, name: str) -> list[str]:
        """What actually starts. On Windows a batch file (.cmd / .bat), found on PATH or named in command_paths
        alike, runs through cmd.exe, which parses the arguments again (& | ^ % ...): only npm wrappers are taken,
        their script started with node directly, and cmd itself is not started either."""
        argv = self._configured(name)
        if os.name == "nt":
            program = argv[0]
            if os.path.basename(program).lower() in ("cmd", "cmd.exe", "command.com"):
                raise WebCoderError("CLI_UNSUPPORTED", f"{name} would run through cmd.exe, which parses the arguments "
                                    "again (& | ^ % ...); set command_paths in the agent config to the real program")
            if os.path.splitext(program)[1].lower() in (".cmd", ".bat"):
                entry = npm_entry(program)
                if entry is None:
                    raise WebCoderError("CLI_UNSUPPORTED", f"{program} is a batch file; only npm command wrappers can "
                                        "run without cmd.exe (set command_paths in the agent config to the real "
                                        "program)")
                return entry + argv[1:]
        return argv

    def _not_in_projects(self, name: str, argv: list[str], cwd: str) -> None:
        """What runs must not lie in a folder the file tools can write to (the allowed folders): there write_file,
        then cli_run, would run any code as the owner. Checked: the program as configured (an npm wrapper names
        what runs, so it counts too), the node it picks (a node.exe next to it comes first) and its script, and
        every file a command_paths entry names, a relative one also as the program sees it (from `cwd`)."""
        candidates = []
        for parts in (self._configured(name), argv):
            for index, part in enumerate(parts):
                if index == 0 and not os.path.dirname(part):
                    candidates.append(shutil.which(part))  # a bare program name: found on PATH
                else:
                    candidates += [os.path.join(cwd, part), part]  # join keeps an absolute path as it is
        for path in candidates:
            if not path or not os.path.exists(path):
                continue
            real = os.path.realpath(path)
            if any(is_within(real, root) for root in self.files.sb.allowed_roots):
                raise WebCoderError("CLI_WRITABLE", f"{name} would run {real}, which is inside a folder the AI can "
                                    "change files in (this computer's allowed folders), so it could rewrite what runs. "
                                    "Install the program elsewhere, or allow narrower folders")

    def status(self) -> dict:
        items = []
        for name in self.allowed:
            try:
                self.resolve(name)
                items.append({"name": name, "found": True})
            except WebCoderError:
                items.append({"name": name, "found": False})
        return {"programs": items}

    def dispatch(self, op: str, args: dict) -> dict:
        if not self.allowed:
            raise WebCoderError("CLI_DISABLED", "No programs are allowed on this computer; its owner can list them in "
                                "the WebCoder panel (设备 → 修改设置) or run: webcoder-agent commands set <program>...")
        method = getattr(self, self.OPERATIONS[op])
        try:
            inspect.signature(method).bind(**args)
        except TypeError as exc:
            raise WebCoderError("INVALID_ARGUMENT", f"Invalid arguments for {op}: {exc}") from None
        return method(**args)

    def run(self, program, args=None, root=None, stdin=None, timeout=120) -> dict:
        if self.files.sb.read_only:  # "the AI can look, not change": a program could change anything
            raise WebCoderError("AGENT_READ_ONLY", "The agent on this device is configured read-only, and a program "
                                "could change files on it, so none are run")
        name = check_name(program)
        if name not in self.allowed:
            raise WebCoderError("CLI_NOT_ALLOWED", f"{name} is not among the programs allowed on this computer: "
                                + ", ".join(self.allowed))
        args = [] if args is None else args
        if (not isinstance(args, list) or len(args) > 500 or any(not isinstance(a, str) or "\x00" in a for a in args)
                or sum(len(a) + 3 for a in args) > 30_000):
            raise WebCoderError("INVALID_ARGUMENT", "args must be a list of strings (at most 30,000 characters in all)")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600:
            raise WebCoderError("INVALID_ARGUMENT", "timeout must be 1-600 seconds")
        if stdin is not None and (not isinstance(stdin, str) or len(stdin) > MAX_STDIN):
            raise WebCoderError("INVALID_ARGUMENT", f"stdin must be text of at most {MAX_STDIN:,} characters")
        cwd = self.files.sb.root(root) if root is not None else tempfile.gettempdir()
        program_argv = self.resolve(name)
        self._not_in_projects(name, program_argv, cwd)
        argv = [*program_argv, *args]
        env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}
        started = time.monotonic()
        with tempfile.TemporaryFile() as feed:
            if stdin is not None:
                feed.write(stdin.encode("utf-8"))
                feed.seek(0)
            try:
                done = run_capped(argv, timeout, MAX_CAPTURE, stdin=feed if stdin is not None else subprocess.DEVNULL,
                                  cwd=cwd, env=env)
            except FileNotFoundError:
                raise WebCoderError("CLI_MISSING", f"{argv[0]} was not found on this computer") from None
            except subprocess.TimeoutExpired:
                raise WebCoderError("CLI_TIMEOUT", f"{name} did not finish within {timeout}s") from None
            except Stopped:
                raise WebCoderError("STOPPED",
                                    "Stopped: this device was disabled or deleted in the hub panel") from None
        stdout, stderr = decode(done.stdout), decode(done.stderr)
        cut = done.stdout_cut or done.stderr_cut or len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT
        return {"program": name, "exit_code": done.returncode, "stdout": stdout[:MAX_OUTPUT],
                "stderr": stderr[-MAX_OUTPUT:], "truncated": cut,
                "duration_ms": int((time.monotonic() - started) * 1000)}
