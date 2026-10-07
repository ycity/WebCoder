"""Git in project folders, for the hub's git_* tools.

git.run runs any git command as given, the way the owner would at this computer: hooks, settings and every
option work as usual (git's options can run any program: -c alias.x=!cmd, rebase --exec ...).

Every other operation is a fixed git command built here from checked arguments, confined: hooks, fsmonitor,
external diff / textconv, pagers and editors are off, it only talks to remotes already configured in the
repository, files on the deny list stay out of diffs and are never staged, and commands that throw away
uncommitted work first save it under refs/webcoder/snapshots/. No git command prompts for passwords, and all
are stopped with the agent's other tools when the hub revokes the device.
"""
from __future__ import annotations

import fnmatch
import inspect
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time

from webcoder.agent.fileops import DENY_EXCEPTIONS, is_within
from webcoder.agent.procs import Stopped, run_capped
from webcoder.common.errors import WebCoderError

MAX_OUTPUT = 400_000
MAX_LISTING = 128 * 1024 * 1024  # file lists the safety checks read whole (a big repository's tree)
MAX_SETTINGS = 16 * 1024 * 1024  # all of git's settings, read whole before anything runs
LOCAL_TIMEOUT = 120
NETWORK_TIMEOUT = 300
MAX_STDIN = 1024 * 1024
REF_RE = re.compile(r"^(?![-.])(?!.*\.\.)[A-Za-z0-9._/~^@{}+\-]{1,200}$")  # a1b2c3d, main, v1.2, HEAD~2, stash@{1}
BRANCH_RE = re.compile(r"^(?![-./])(?!.*\.\.)(?!.*//)(?!.*@\{)(?!.*\.lock(?:/|$))(?!.*[./]$)[A-Za-z0-9._/\-]{1,200}$")
REMOTE_RE = re.compile(r"^(?![-.])[A-Za-z0-9._\-]{1,100}$")
_COMMIT_ID = re.compile(r"[0-9a-f]{4,64}")  # as git writes them into its own state files
_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.\-]*://)[^/\s@]+@", re.IGNORECASE)  # https://user:token@host
_URL_QUERY = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s?#'\"]*\?)[^\s#'\"]+", re.IGNORECASE)  # ...?token=x
# Settings whose value is a program git runs during the operations here (hooks, fsmonitor, pager, editor, askpass
# and external diff are switched off on every call instead).
_RUNS_PROGRAMS = re.compile(
    r"^(filter\..+\.(clean|smudge|process)|merge\..+\.driver|diff\..+\.(command|textconv)"
    r"|core\.(sshcommand|gitproxy|alternaterefscommand)|credential(\..+)?\.helper|gpg(\..+)?\.program"
    r"|remote\..+\.(uploadpack|receivepack))$", re.IGNORECASE)
_PROGRAM_PATH = re.compile(r"^gpg(\..+)?\.program$", re.IGNORECASE)  # run as it is, not through a shell


def locate_git(override=None) -> list[str] | None:
    if override:
        return [str(part) for part in override] if isinstance(override, list) else [str(override)]
    found = shutil.which("git")
    return [found] if found else None


def _ref(value, name: str = "ref") -> str:
    if not isinstance(value, str) or not REF_RE.match(value):
        raise WebCoderError("INVALID_ARGUMENT", f"{name} must be a commit, branch or tag such as HEAD~1, main, v1.2 "
                            "or a1b2c3d (no spaces, no leading '-', no '..')")
    return value


def _branch(value, name: str = "branch") -> str:
    if not isinstance(value, str) or not BRANCH_RE.match(value):
        raise WebCoderError("INVALID_ARGUMENT", f"{name} must be a branch name such as feature/login (letters, digits, "
                            "'.', '_', '-', '/'; no leading '-', no '..')")
    return value


def _count(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise WebCoderError("INVALID_ARGUMENT", f"{name} must be an integer between {low} and {high}")
    return value


def _text(value, name: str, limit: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise WebCoderError("INVALID_ARGUMENT", f"{name} must be non-empty text (at most {limit:,} characters)")
    return value


def _mask(text: str) -> str:
    return _CREDENTIALS.sub(r"\1***@", text)


def _mask_remotes(text: str) -> str:
    """Where remote URLs are printed: a token can also sit in a URL's query (signed URLs of remote helpers).
    Not for diffs, whose URLs are the project's own content."""
    return _URL_QUERY.sub(r"\1***", _mask(text))


def _flags(*pairs) -> list[str]:
    """The flags whose switch is exactly True."""
    return [flag for flag, on in pairs if on is True]


def _words(command: str) -> list[str]:
    """The words of a command that could name a file (./x, tools/y.sh, helper.sh, C:/z, the ./x of --opt=./x),
    read as the shell would and also as plain words (a Windows path's backslashes are not escapes to the one who
    wrote it). Options and URLs are left out."""
    words = []
    for posix in (True, False):  # the second keeps backslashes, and quoted words whole
        try:
            words += [word.strip("\"'") for word in shlex.split(command, posix=posix)]
        except ValueError:
            words += command.split()
    found = []
    for word in words:
        word = word.lstrip("!")  # "!command" credential helpers
        for part in (word, word.partition("=")[2]):
            if part and "://" not in part and not part.startswith("-"):
                found.append(part)
    return found


def env_globs() -> list[str]:
    """`.env.*` minus the sample files the deny list lets through (.env.example ...), as globs. A pathspec cannot
    subtract, so every other name is caught where it first differs from all the samples: `.env.[!dest]*`,
    `.env.e[!x]*` ... and names that go on past a sample (`.env.example?*`)."""
    samples = sorted(name[len(".env."):] for name in DENY_EXCEPTIONS)
    globs = []

    def walk(prefix: str) -> None:
        following = sorted({name[len(prefix)] for name in samples if name.startswith(prefix) and name != prefix})
        if prefix not in samples:
            globs.append(f".env.{prefix}")
        globs.append(f".env.{prefix}[!{''.join(following)}]*" if following else f".env.{prefix}?*")
        for char in following:
            walk(prefix + char)

    walk("")
    return globs


class GitOps:
    """The git.* operations the hub may request, in projects of this device."""

    OPERATIONS = {"git.status": "status", "git.diff": "diff", "git.log": "log", "git.show": "show",
                  "git.blame": "blame", "git.branches": "branches", "git.add": "add", "git.commit": "commit",
                  "git.restore": "restore", "git.reset": "reset", "git.branch": "branch", "git.stash": "stash",
                  "git.sync": "sync", "git.integrate": "integrate", "git.tag": "tag", "git.clean": "clean",
                  "git.init": "init", "git.run": "run"}
    READS = {"git.status", "git.diff", "git.log", "git.show", "git.blame", "git.branches"}

    def __init__(self, files, *, command=None, hooks_dir=None):
        self.files = files  # FileOps: the sandbox that says which folders are projects and what is denied
        self.command = command
        self.hooks_dir = str(hooks_dir) if hooks_dir else None

    def status_info(self) -> dict:
        return {"client": locate_git(self.command) is not None}

    def dispatch(self, op: str, args: dict) -> dict:
        method = getattr(self, self.OPERATIONS[op])
        try:
            inspect.signature(method).bind(**args)
        except TypeError as exc:
            raise WebCoderError("INVALID_ARGUMENT", f"Invalid arguments for {op}: {exc}") from None
        if op not in self.READS and self.files.sb.read_only:
            raise WebCoderError("AGENT_READ_ONLY", "The agent on this device is configured read-only")
        if op not in self.READS and op not in ("git.init", "git.run"):
            self._refuse_tracked_agent_files(args.get("root"))
        return method(**args)

    def _refuse_tracked_agent_files(self, root) -> None:
        """A repository that tracks the agent's own files (someone committed its folder) would have them rewritten
        by resets, switches, merges and stashes: nothing that changes it runs until they are out of git."""
        folders = self._agent_folders(root)
        if not folders:
            return
        real = self._repo(root)
        done = self._git(real, ["ls-files", "-z", "--", *(f":(literal,icase){folder}" for folder in folders)])
        if done.returncode != 0 or done.stdout.strip(b"\0"):
            raise WebCoderError("GIT_AGENT_FILES_TRACKED", "This repository tracks the agent's own files ("
                                + ", ".join(folders) + "), device secret included: the git tools change nothing here "
                                "until the owner takes them out of git (git rm -r --cached <folder>).")

    # ------------------------------------------------------------------ running git
    def _env(self) -> dict:
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "GCM_INTERACTIVE": "never",
                    "GIT_EDITOR": ":", "GIT_SEQUENCE_EDITOR": ":", "GIT_PAGER": "cat", "PAGER": "cat",
                    "GIT_OPTIONAL_LOCKS": "0", "GIT_MERGE_AUTOEDIT": "no"})
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_LITERAL_PATHSPECS", "GIT_CONFIG_PARAMETERS",
                     "GIT_EXTERNAL_DIFF"):
            env.pop(name, None)  # the repository is the project folder, and pathspec magic is ours
        return env

    def _confinement(self) -> list[str]:
        hooks = os.devnull  # a hooks folder with nothing in it: no hook ever runs
        if self.hooks_dir:
            try:
                os.makedirs(self.hooks_dir, exist_ok=True)
                hooks = self.hooks_dir
            except OSError:
                pass
        return ["-c", f"core.hooksPath={hooks}", "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
                "-c", "core.editor=:", "-c", "core.askPass=", "-c", "color.ui=false", "-c", "core.quotePath=false",
                "-c", "diff.external=", "-c", "protocol.ext.allow=never", "-c", "submodule.recurse=false"]

    def _git(self, cwd: str, args: list[str], timeout: float = LOCAL_TIMEOUT, limit: int = MAX_OUTPUT,
             extra_env: dict | None = None, stdin: bytes | None = None,
             confined: bool = True) -> subprocess.CompletedProcess:
        git = locate_git(self.command)
        if git is None:
            raise WebCoderError("GIT_MISSING", "git was not found on this computer (install Git and restart the agent)")
        argv = [*git, *(self._confinement() if confined else ()), *args]
        try:
            if stdin is None:
                return run_capped(argv, timeout, limit, cwd=cwd, env={**self._env(), **(extra_env or {})})
            with tempfile.TemporaryFile() as feed:  # a file, not a pipe: nothing to write while git reads
                feed.write(stdin)
                feed.seek(0)
                return run_capped(argv, timeout, limit, stdin=feed, cwd=cwd, env={**self._env(), **(extra_env or {})})
        except FileNotFoundError:
            raise WebCoderError("GIT_MISSING", f"{git[0]} was not found on this computer") from None
        except NotADirectoryError:
            raise WebCoderError("ROOT_NOT_FOUND", "The project folder does not exist") from None
        except subprocess.TimeoutExpired:
            raise WebCoderError("GIT_TIMEOUT", f"git {args[0]} did not finish within {timeout:.0f}s") from None
        except Stopped:
            raise WebCoderError("STOPPED", "Stopped: this device was disabled or deleted in the hub panel") from None

    def _repo(self, root) -> str:
        """The project folder, which must be the top of a git repository: a project inside a bigger repository
        would let git reach files outside the project. Checked on every call: a project's .git can be removed
        (making it part of an outer repository) while the agent runs."""
        real = self.files.sb.root(root)
        done = self._git(real, ["rev-parse", "--show-toplevel"])
        if done.returncode != 0:
            raise WebCoderError("GIT_NOT_A_REPOSITORY", "This project folder is not a git repository (git_init "
                                "creates one): " + done.stderr.decode("utf-8", "replace").strip()[-300:])
        top = os.path.realpath(done.stdout.decode("utf-8", "replace").strip())
        if os.path.normcase(top) != os.path.normcase(real):
            raise WebCoderError("GIT_NOT_REPOSITORY_ROOT", "The project folder is inside a larger git repository "
                                f"({top}); git tools only work on a project whose folder is the top of its repository")
        self._settings_are_safe(real)  # before any other git command here: even a listing can run a filter
        return real

    def _settings_are_safe(self, real: str) -> None:
        """git settings can run programs (filters, merge drivers, ssh, credential helpers, signing ...), and so
        they must not be the AI's to change: refused when a setting comes from a file the AI can change (an
        included .gitconfig in any allowed folder, not only this project: any program at all), or runs a program
        or script from there."""
        listed = self._git(real, ["config", "--list", "--show-origin", "-z"], limit=MAX_SETTINGS)
        if listed.returncode != 0 or listed.stdout_cut:
            raise WebCoderError("GIT_FAILED", "Could not read this repository's git settings, so nothing was run: "
                                + listed.stderr.decode("utf-8", "replace").strip()[-300:])
        fields = listed.stdout.decode("utf-8", "replace").split("\0")
        for origin, entry in zip(fields[0::2], fields[1::2], strict=False):  # the list ends with a NUL
            key, _, value = entry.partition("\n")
            if origin.startswith("file:") and self._in_project(real, origin[len("file:"):]):
                raise WebCoderError("GIT_PROJECT_SETTINGS", f"This repository reads git settings from "
                                    f"{origin[len('file:'):]}, a file in a folder the AI can change (this computer's "
                                    "allowed folders): it could make git run any program, so the git tools are off "
                                    "here. Remove that include, or use git yourself.")
            words = [value] if _PROGRAM_PATH.match(key) else _words(value)  # a path, not a command line
            if _RUNS_PROGRAMS.match(key) and any(self._project_file(real, word) for word in words):
                raise WebCoderError("GIT_PROJECT_SETTINGS", f"The git setting {key} runs something from a folder the "
                                    "AI can change (this computer's allowed folders), so the git tools are off here. "
                                    "Point it elsewhere, or use git yourself.")  # not its value: a token can sit in it
        env = self._env()  # the agent's environment can name programs too
        for name, whole in (("GIT_SSH", True), ("GIT_EXEC_PATH", True), ("GIT_SSH_COMMAND", False),
                            ("GIT_PROXY_COMMAND", False)):
            value = env.get(name)
            if value and any(self._project_file(real, word) for word in ([value] if whole else _words(value))):
                raise WebCoderError("GIT_PROJECT_SETTINGS", f"{name} in the agent's environment runs something from a "
                                    "folder the AI can change, so the git tools are off here. Point it elsewhere and "
                                    "restart the agent, or use git yourself.")

    def _project_file(self, real: str, word: str) -> bool:
        """Does a word of a command name something the AI can change: a path into the folders it may write, or a
        bare name of a file at the project's top (git runs these commands there, so `sh helper.sh` runs the
        project's helper.sh)? Not folders there: `git-lfs clean` must not be taken for a folder named clean."""
        if "/" in word or "\\" in word or word.startswith("."):
            return self._in_project(real, word)
        return os.path.isfile(os.path.join(real, word)) and self._in_project(real, word)

    def _in_project(self, real: str, path: str) -> bool:
        """Among the files the AI can change: in any folder this agent opens to the file tools (another project's
        files are as writable as this one's), not in a .git folder (which the file tools never touch) and not in
        the agent's own folder. `path` may be relative to the project."""
        full = os.path.realpath(os.path.join(real, path))
        if self.files.sb.is_protected(full):
            return False
        for root in self.files.sb.allowed_roots:
            if is_within(full, root):
                return ".git" not in os.path.relpath(full, root).replace("\\", "/").lower().split("/")
        return False

    def _run(self, root, args: list[str], *, timeout: float = LOCAL_TIMEOUT, snapshot: bool = False) -> dict:
        real = self._repo(root)
        saved = self._snapshot(real) if snapshot else None
        done = self._git(real, args, timeout)
        stdout = done.stdout.decode("utf-8", "replace")
        stderr = done.stderr.decode("utf-8", "replace")
        return {"command": "git " + " ".join(args), "exit_code": done.returncode, "stdout": _mask(stdout),
                "stderr": _mask(stderr), "truncated": done.stdout_cut or done.stderr_cut, "snapshot": saved}

    def _snapshot(self, real: str) -> str | None:
        """Save uncommitted changes to tracked files before a command that would throw them away, shaped like a
        stash (git_stash apply brings them back). Built by hand rather than by `git stash create`, to leave secret
        files out: a snapshot is a commit, and commits can be branched from, merged and pushed. Their own
        uncommitted changes are never thrown away (see _refuse_discarding_secrets)."""
        def step(args: list[str], env: dict | None = None) -> str:
            done = self._git(real, args, extra_env=env)
            if done.returncode != 0:  # e.g. conflicts not yet resolved, or no commit yet: keep the changes
                raise WebCoderError("GIT_FAILED", "Could not save the uncommitted changes first, so nothing was "
                                    "changed. During a merge, rebase or cherry-pick with conflicts, finish or abort "
                                    "it with git_integrate first. git said: "
                                    + done.stderr.decode("utf-8", "replace").strip()[-300:])
            return done.stdout.decode("utf-8", "replace").strip()

        head_tree = step(["rev-parse", "--verify", "HEAD^{tree}"])
        index = step(["rev-parse", "--git-path", "index"])
        who = {"GIT_AUTHOR_NAME": "WebCoder", "GIT_AUTHOR_EMAIL": "webcoder@localhost",
               "GIT_COMMITTER_NAME": "WebCoder", "GIT_COMMITTER_EMAIL": "webcoder@localhost"}
        with tempfile.TemporaryDirectory(prefix="webcoder-") as folder:
            temp = {"GIT_INDEX_FILE": os.path.join(folder, "index")}  # the real index is not touched
            if os.path.isfile(os.path.join(real, index)):
                shutil.copyfile(os.path.join(real, index), temp["GIT_INDEX_FILE"])
            else:
                step(["read-tree", "HEAD"], temp)
            step(["reset", "-q", "HEAD", "--", *self._secret_specs(real, exclude=False)], temp)  # staged secrets out
            index_tree = step(["write-tree"], temp)
            step(["add", "--update", "--", ".", *self._excluded(real)], temp)  # the working tree, secrets aside
            work_tree = step(["write-tree"], temp)
        if index_tree == head_tree and work_tree == head_tree:
            return None  # nothing to save
        on_index = step(["commit-tree", index_tree, "-p", "HEAD", "-m", "index: saved by webcoder"], who)
        commit = step(["commit-tree", work_tree, "-p", "HEAD", "-p", on_index, "-m",
                       "webcoder: saved before a command that discards changes"], who)
        name = f"refs/webcoder/snapshots/{time.strftime('%Y%m%d-%H%M%S')}-{commit[:8]}"
        kept = self._git(real, ["update-ref", "-m", "webcoder snapshot", name, commit])
        if kept.returncode != 0:
            raise WebCoderError("GIT_FAILED", "Could not save the uncommitted changes first, so nothing was changed: "
                                + kept.stderr.decode("utf-8", "replace").strip()[-300:])
        return name

    def _paths(self, root, paths, *, default: str | None = None) -> list[str]:
        """Pathspecs for project paths, taken literally (no pathspec magic from the caller)."""
        if paths is None:
            paths = [default] if default else []
        # never empty: the secret excludes alone would make a pathspec for the whole repository
        if not isinstance(paths, list) or not 1 <= len(paths) <= 200:
            raise WebCoderError("INVALID_ARGUMENT", "paths must be a list of 1 to 200 paths in the project"
                                + ("; leave it out for the whole project" if default else ""))
        return [":(literal)" + (self.files.sb.resolve(root, path).rel or ".") for path in paths]

    def _secrets(self) -> list[str]:
        """The deny list (secrets) as glob patterns for pathspecs, with the same exceptions as the files sandbox:
        sample env files are not secrets."""
        globs = []
        for pattern in self.files.sb.deny:
            if pattern == ".env.*":
                globs += env_globs()
            else:
                globs.append(pattern[:-1] + "**" if pattern.endswith("/*") else pattern)
        return globs

    def _agent_folders(self, root) -> list[str]:
        """The agent's own folders (device secret, backups) inside this repository, e.g. when it is the home folder:
        off limits to the file tools, so to git too."""
        real = self.files.sb.root(root)
        return [os.path.relpath(folder, real).replace("\\", "/") for folder in self.files.sb.protected_dirs
                if is_within(folder, real) and os.path.normcase(folder) != os.path.normcase(real)]

    def _top_secrets(self, root) -> list[str]:
        """Deny patterns with a folder in them ('.aws/credentials') whose folder is the project itself (~/.aws):
        they apply to its top level, as globs from there. (The file sandbox judges whole paths, so does git.)"""
        folders = [part.lower() for part in re.split(r"[\\/]", self.files.sb.root(root)) if part]
        globs = []
        for pattern in self.files.sb.deny:
            steps = pattern.split("/")
            if pattern.endswith("/*"):  # everything below such a folder, at any depth: the whole project here
                head = steps[:-1]
                windows = (folders[i:i + len(head)] for i in range(len(folders) - len(head) + 1))
                if any(all(fnmatch.fnmatchcase(folder, step) for folder, step in zip(window, head, strict=True))
                       for window in windows):
                    globs.append("**")
                continue
            for k in range(1, min(len(steps), len(folders) + 1)):  # the project is the folder the file is in
                if all(fnmatch.fnmatchcase(folder, step) for folder, step in zip(folders[-k:], steps[:k], strict=True)):
                    globs.append("/".join(steps[k:]))
        return globs

    def _secret_specs(self, root, *, exclude: bool) -> list[str]:
        """Pathspecs for the deny-listed files (secrets), anywhere in the repository; `exclude` to leave them out."""
        magic = "exclude,glob,icase" if exclude else "glob,icase"
        return ([f":({magic})**/{glob}" for glob in self._secrets()]
                + [f":({magic}){glob}" for glob in self._top_secrets(root)])

    def _excluded(self, root) -> list[str]:
        """Pathspecs that keep deny-listed files (secrets) and the agent's own folders out of diffs and out of the
        index."""
        return (self._secret_specs(root, exclude=True)
                + [f":(exclude,literal,icase){folder}" for folder in self._agent_folders(root)])

    def _refuse_staged_secrets(self, root) -> None:
        """A commit of the index would take secrets the owner staged: refuse. (Removing one is fine.)"""
        real = self._repo(root)
        done = self._git(real, ["diff", "--cached", "--name-only", "-z", "--no-renames", "--diff-filter=d", "--",
                                *self._secret_specs(root, exclude=False),
                                *(f":(literal,icase){folder}" for folder in self._agent_folders(root))])
        if done.returncode != 0:
            raise WebCoderError("GIT_FAILED", "Could not check the staged files, so nothing was committed: "
                                + done.stderr.decode("utf-8", "replace").strip()[-300:])
        staged = [name for name in done.stdout.decode("utf-8", "replace").split("\0") if name]
        if staged:
            raise WebCoderError("GIT_SECRETS_STAGED", "Secret files are staged, and the AI never commits them: "
                                f"{', '.join(staged[:10])}. Nothing was committed. The owner can unstage them "
                                "(git restore --staged <file>) or commit them by hand; or commit with paths, which "
                                "leaves staged secrets out.")

    def _refuse_overwriting_untracked(self, root, ref: str | None = None, paths: list[str] | None = None, *,
                                      names: list[str] | None = None) -> None:
        """Checking out, merging, resetting or restoring writes files over untracked ones in the way, and over
        ignored ones even when git otherwise protects untracked files (a local .env ...); no snapshot keeps them:
        refuse when any are in the way. The files that may be written: all of `ref` (its tree), or `names`."""
        real = self._repo(root)
        if names is None:
            tree = self._git(real, ["ls-tree", "-r", "-z", "--name-only", "--full-tree", ref, "--", *(paths or [])],
                             limit=MAX_LISTING)
            if tree.returncode != 0:
                return  # not a commit here: the command itself says so
            if tree.stdout_cut:
                raise WebCoderError("GIT_FAILED", "Could not list every file to check that no untracked file would be "
                                    "overwritten, so nothing was changed")
            names = tree.stdout.decode("utf-8", "replace").split("\0")
        index = self._git(real, ["ls-files", "-z"], limit=MAX_LISTING)
        # tracked files not as git left them: deleted (maybe a file now where a folder was) or changed (maybe a
        # folder now, with untracked files in it, where the file was)
        moved = self._git(real, ["ls-files", "-z", "--deleted", "--modified"], limit=MAX_LISTING)
        if any(done.stdout_cut or done.returncode != 0 for done in (index, moved)):
            raise WebCoderError("GIT_FAILED", "Could not list every file to check that no untracked file would be "
                                "overwritten, so nothing was changed")
        # names match as the filesystem does: README.md and readme.md are one file only where case does not count
        ignorecase = self._git(real, ["config", "--get", "--bool", "core.ignorecase"]).stdout.strip() == b"true"
        fold = str.casefold if ignorecase else str
        tracked = {fold(name) for name in index.stdout.decode("utf-8", "replace").split("\0")}
        changed = {fold(name) for name in moved.stdout.decode("utf-8", "replace").split("\0")}
        found = []
        for name in names:
            is_tracked = fold(name) in tracked
            if not name or is_tracked and fold(name) not in changed:
                continue  # where git expects it: nothing untracked in the way
            parts = name.split("/")
            for depth in range(1, len(parts) + 1):
                here, prefix = os.path.join(real, *parts[:depth]), "/".join(parts[:depth])
                if depth == len(parts):
                    if not is_tracked and os.path.lexists(here):  # an untracked file or folder where `ref` has one
                        found.append(name)
                    elif is_tracked and os.path.isdir(here) and not os.path.islink(here) and os.listdir(here):
                        found.append(name)  # a folder of untracked files where the tracked file was
                elif not os.path.lexists(here):
                    break
                elif fold(prefix) in tracked:  # git has a file here, where `ref` has a folder
                    if os.path.isdir(here) and not os.path.islink(here):
                        continue  # but it is a folder now: nothing in it is tracked
                    break
                elif os.path.islink(here) or not os.path.isdir(here):  # an untracked file where `ref` has a folder
                    found.append(prefix)
                    break
        if found:
            found = sorted(set(found))
            raise WebCoderError("GIT_UNTRACKED_IN_THE_WAY", "These untracked or ignored files would be overwritten, "
                                f"and no snapshot can keep them: {', '.join(found[:20])}"
                                + (f" and {len(found) - 20} more" if len(found) > 20 else "")
                                + ". Nothing was changed. Move, commit or delete them first.")

    def _touched(self, root, revisions: list[str], *, one_commit: bool = False) -> list[str]:
        """The paths what a merge or rebase brings in (`git diff HEAD...ref`) or a commit changes (for cherry-pick
        and revert) touch: the files they may write."""
        real = self._repo(root)
        command = ["diff-tree", "-r", "-m", "--root", "--no-commit-id"] if one_commit else ["diff"]
        done = self._git(real, [*command, "--name-only", "-z", "--no-renames", *revisions], limit=MAX_LISTING)
        if done.returncode != 0:
            return []  # not a commit here: the command itself says so
        if done.stdout_cut:
            raise WebCoderError("GIT_FAILED", "Could not list every file the change touches, so nothing was changed")
        return [name for name in done.stdout.decode("utf-8", "replace").split("\0") if name]

    def _replayed(self, root, onto: str, head: str = "HEAD") -> list[str]:
        """The paths the commits a rebase replays (in `head`, not in `onto`) touch, commit by commit: a file one of
        them adds and a later one deletes is written on the way, over an ignored file of that name."""
        real = self._repo(root)
        done = self._git(real, ["log", "-m", "--no-renames", "--name-only", "-z", "--format=", f"{onto}..{head}"],
                         limit=MAX_LISTING)
        if done.returncode != 0:
            return []  # not a commit here: the command itself says so
        if done.stdout_cut:
            raise WebCoderError("GIT_FAILED", "Could not list every file the rebase replays, so nothing was changed")
        return [name for name in done.stdout.decode("utf-8", "replace").split("\0") if name]

    def _still_to_apply(self, root, command: str) -> list[str]:
        """The paths what a stopped rebase, cherry-pick or revert has yet to apply may write. Its start checked
        them, but files may have appeared since (an ignored one git would write over): continue and skip check
        again. A rebase: every commit it replays (onto..orig-head); the others: the commits left on their list."""
        real = self._repo(root)
        found = self._git(real, ["rev-parse", "--absolute-git-dir"])
        git_dir = found.stdout.decode("utf-8", "replace").strip()
        if found.returncode != 0 or not git_dir:
            return []

        def state(*parts: str) -> str:
            try:
                with open(os.path.join(git_dir, *parts), encoding="utf-8", errors="replace") as handle:
                    return handle.read(1024 * 1024)
            except OSError:
                return ""

        if command == "rebase":
            for folder in ("rebase-merge", "rebase-apply"):
                onto, head = state(folder, "onto").strip(), state(folder, "orig-head").strip()
                if _COMMIT_ID.fullmatch(onto) and _COMMIT_ID.fullmatch(head):
                    return self._replayed(root, onto, head)
            return []
        commits = [words[1] for line in state("sequencer", "todo").splitlines()
                   if len(words := line.split()) > 1 and words[0] in ("pick", "p", "revert")
                   and _COMMIT_ID.fullmatch(words[1])]
        return [name for commit in commits[:1000] for name in self._touched(root, [commit], one_commit=True)]

    def _indexed(self, root, pathspecs: list[str]) -> list[str]:
        """The tracked files these pathspecs cover (what a restore from the index writes)."""
        real = self._repo(root)
        done = self._git(real, ["ls-files", "-z", "--", *pathspecs], limit=MAX_LISTING)
        if done.returncode != 0 or done.stdout_cut:
            raise WebCoderError("GIT_FAILED", "Could not list the files to restore, so nothing was changed")
        return [name for name in done.stdout.decode("utf-8", "replace").split("\0") if name]

    def _refuse_discarding_secrets(self, root) -> None:
        """Secret files are never rewritten through WebCoder: a hard reset, a forced switch or aborting a merge that
        would throw away uncommitted changes to one is refused, also when the merge brought them (they cannot be
        told apart from the owner's). (Moving to another commit changes them as any switch does.)"""
        real = self._repo(root)
        done = self._git(real, ["diff", "--name-only", "-z", "--no-renames", "HEAD", "--",
                                *self._secret_specs(root, exclude=False)])
        if done.returncode != 0:
            raise WebCoderError("GIT_FAILED", "Could not check the secret files for uncommitted changes, so nothing "
                                "was changed: " + done.stderr.decode("utf-8", "replace").strip()[-300:])
        changed = [name for name in done.stdout.decode("utf-8", "replace").split("\0") if name]
        if changed:
            raise WebCoderError("GIT_SECRETS_CHANGED", "Secret files have uncommitted changes that this would throw "
                                f"away: {', '.join(changed[:10])}. Nothing was changed; the owner can commit, stash or "
                                "undo them first (or, in the middle of a merge or rebase, finish or abort it on the "
                                "computer).")

    def _remote(self, real: str, name) -> str:
        """Only remotes already configured in the repository: never a URL or a path from the caller."""
        if not isinstance(name, str) or not REMOTE_RE.match(name):
            raise WebCoderError("INVALID_ARGUMENT", "remote must be the name of a configured remote, e.g. origin")
        listed = self._git(real, ["remote"]).stdout.decode("utf-8", "replace").split()
        if name not in listed:
            raise WebCoderError("GIT_UNKNOWN_REMOTE", f"No remote named {name!r} in this repository; configured: "
                                + (", ".join(listed) or "none"))
        return name

    # ------------------------------------------------------------------ reading
    def status(self, root) -> dict:
        return self._run(root, ["status", "--branch", "--short", "--untracked-files=normal"])

    def diff(self, root, staged=False, base=None, head=None, paths=None, stat=False, context=3) -> dict:
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", f"-U{_count(context, 'context', 0, 50)}"]
        if staged is True:
            args.append("--cached")
        if stat is True:
            args.append("--stat")
        if head is not None and base is None:
            raise WebCoderError("INVALID_ARGUMENT", "head needs base (the diff goes from base to head)")
        args += [_ref(ref, name) for ref, name in ((base, "base"), (head, "head")) if ref is not None]
        return self._run(root, [*args, "--", *self._paths(root, paths, default="."), *self._excluded(root)])

    def log(self, root, ref=None, base=None, max_count=30, paths=None, patch=False) -> dict:
        args = ["log", "--no-color", "--no-ext-diff", "--no-textconv", "--date=format:%Y-%m-%d %H:%M",
                f"--max-count={_count(max_count, 'max_count', 1, 500)}"]
        args += ["-p", "--stat"] if patch is True else ["--format=%h %ad %an%d%n    %s"]
        revision = _ref(ref) if ref is not None else "HEAD"
        if base is not None:
            revision = f"{_ref(base, 'base')}..{revision}"
        return self._run(root, [*args, revision, "--", *self._paths(root, paths, default="."), *self._excluded(root)])

    def show(self, root, ref="HEAD", path=None, stat_only=False) -> dict:
        revision = _ref(ref)
        if path is not None:  # the file as it was at that commit (deny list checked like any read)
            return self._run(root, ["show", "--no-textconv", f"{revision}:{self.files.sb.resolve(root, path).rel}"])
        # only commits: the excludes filter a commit's diff, but a blob (a secret's, by its id) would show whole
        if self._git(self._repo(root), ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"]).returncode:
            raise WebCoderError("INVALID_ARGUMENT", f"{revision} is not a commit: git_show shows commits (pass path to "
                                "see a file as it was in one)")
        args = ["show", "--no-color", "--no-ext-diff", "--no-textconv", "--date=iso", revision]
        if stat_only is True:
            args.append("--stat")
        return self._run(root, [*args, "--", ".", *self._excluded(root)])

    def blame(self, root, path, ref=None, start_line=None, end_line=None) -> dict:
        target = self.files.sb.resolve(root, path)
        args = ["blame", "--no-textconv", "--date=short"]
        if start_line is not None or end_line is not None:
            first = _count(start_line if start_line is not None else 1, "start_line", 1, 10_000_000)
            last = _count(end_line if end_line is not None else first + 199, "end_line", first, 10_000_000)
            args.append(f"-L{first},{last}")
        if ref is not None:
            args.append(_ref(ref))
        return self._run(root, [*args, "--", target.rel])  # a plain path here, not a pathspec (checked above)

    def branches(self, root) -> dict:
        """Branches (with upstream and ahead/behind), remotes, tags and stashes in one go."""
        parts, cut, failed = [], False, None
        for title, args in (("branches", ["branch", "--all", "-vv", "--no-color"]), ("remotes", ["remote", "-v"]),
                            ("tags (newest first)", ["tag", "--list", "--sort=-creatordate"]),
                            ("stashes", ["stash", "list"])):
            done = self._run(root, args)
            lines = _mask_remotes(done["stdout"]).splitlines()
            cut = cut or done["truncated"] or len(lines) > 300
            if done["exit_code"] != 0:  # never an empty list for one that could not be read
                failed = failed or done
                lines = [f"(could not list them: git exited {done['exit_code']})"]
            parts.append(f"== {title}\n" + ("\n".join(lines[:300]) or "(none)"))
        return {"command": "git branch -avv; git remote -v; git tag; git stash list",
                "exit_code": failed["exit_code"] if failed else 0, "stdout": "\n\n".join(parts),
                "stderr": _mask_remotes(failed["stderr"]) if failed else "", "truncated": cut, "snapshot": None}

    # ------------------------------------------------------------------ changing the repository
    def add(self, root, paths=None, every=False) -> dict:
        args = ["add", "--all"] if every is True else ["add"]
        return self._run(root, [*args, "--", *self._paths(root, paths, default="."), *self._excluded(root)])

    def commit(self, root, message=None, every=False, amend=False, allow_empty=False, paths=None) -> dict:
        args = ["commit", "--no-verify"]
        if message is not None:
            args += ["-m", _text(message, "message")]
        elif amend is True:
            args.append("--no-edit")
        else:
            raise WebCoderError("INVALID_ARGUMENT", "A commit needs a message")
        args += _flags(("--amend", amend), ("--allow-empty", allow_empty))
        if paths is not None:  # the named paths' current content, minus secrets; whatever else is staged stays out
            return self._run(root, [*args, "--", *self._paths(root, paths), *self._excluded(root)])
        self._refuse_staged_secrets(root)
        if every is True:  # like --all (every change to tracked files), but secrets are never staged
            staged = self._run(root, ["add", "--update", "--", ".", *self._excluded(root)])
            if staged["exit_code"] != 0:
                return staged
            done = self._run(root, args)
            return {**done, "command": f"{staged['command']} && {done['command']}"}
        return self._run(root, args)

    def restore(self, root, paths, staged=False, worktree=True, source=None) -> dict:
        if staged is not True and worktree is not True:
            raise WebCoderError("INVALID_ARGUMENT", "Choose staged (unstage), worktree (discard changes) or both")
        args = ["restore"] + [flag for flag, on in (("--staged", staged), ("--worktree", worktree)) if on is True]
        if source is not None:
            args.append(f"--source={_ref(source, 'source')}")
        if worktree is True:  # what it writes comes from source, from HEAD when unstaging too, else from the index
            targets = self._paths(root, paths)
            if source is not None or staged is True:
                self._refuse_overwriting_untracked(root, source if source is not None else "HEAD", targets)
            else:
                self._refuse_overwriting_untracked(root, names=self._indexed(root, targets))
        kept = self._excluded(root) if worktree is True else []  # secrets are never rewritten (unstaging is fine)
        return self._run(root, [*args, "--", *self._paths(root, paths), *kept], snapshot=worktree is True)

    def reset(self, root, mode="mixed", ref="HEAD", paths=None) -> dict:
        if mode not in ("soft", "mixed", "hard"):
            raise WebCoderError("INVALID_ARGUMENT", "mode must be soft, mixed or hard")
        if paths is not None:  # unstage these paths (the index takes them from ref)
            if mode != "mixed":
                raise WebCoderError("INVALID_ARGUMENT", "Resetting paths only works with mode=mixed")
            return self._run(root, ["reset", "-q", _ref(ref), "--", *self._paths(root, paths)])
        if mode == "hard":
            self._refuse_overwriting_untracked(root, _ref(ref))
            self._refuse_discarding_secrets(root)
        return self._run(root, ["reset", f"--{mode}", _ref(ref)], snapshot=mode == "hard")

    def branch(self, root, action, name=None, new_name=None, start=None, force=False, discard_changes=False) -> dict:
        if action == "switch":  # an existing branch (a remote one gets a local tracking branch)
            args = ["switch", _branch(name, "name")]
            self._refuse_overwriting_untracked(root, self._switch_target(root, name))  # ignored files too
            if discard_changes is True:
                args.append("--discard-changes")
                self._refuse_discarding_secrets(root)
            return self._run(root, args, snapshot=discard_changes is True)
        if action == "create":
            if start:  # from another commit: its files come in
                self._refuse_overwriting_untracked(root, _ref(start, "start"))
            return self._run(root, ["switch", "-c", _branch(name, "name"), *([_ref(start, "start")] if start else [])])
        if action == "detach":
            self._refuse_overwriting_untracked(root, _ref(name, "name"))
            return self._run(root, ["switch", "--detach", _ref(name, "name")])
        if action == "delete":
            return self._run(root, ["branch", "-D" if force is True else "-d", _branch(name, "name")])
        if action == "rename":
            return self._run(root, ["branch", "-m", _branch(name, "name"), _branch(new_name, "new_name")])
        raise WebCoderError("INVALID_ARGUMENT", "action must be switch, create, detach, delete or rename")

    def _switch_target(self, root, name: str) -> str:
        """What `git switch name` checks out: the branch, or else the one remote branch of that name it would
        track."""
        real = self._repo(root)
        candidates = [f"refs/heads/{name}"] + [f"refs/remotes/{remote}/{name}" for remote in self._git(
            real, ["remote"]).stdout.decode("utf-8", "replace").split()]
        found = [ref for ref in candidates
                 if self._git(real, ["rev-parse", "--verify", "--quiet", ref]).returncode == 0]
        return found[0] if found[:1] == candidates[:1] or len(found) == 1 else candidates[0]

    def stash(self, root, action, message=None, include_untracked=False, ref=None) -> dict:
        if action == "push":  # deny-listed files (untracked .env ...) stay where they are
            args = ["stash", "push"] + (["--include-untracked"] if include_untracked is True else [])
            args += ["-m", _text(message, "message", 500)] if message else []
            return self._run(root, [*args, "--", ".", *self._excluded(root)])
        if action in ("pop", "apply", "drop"):
            stash = _ref(ref if ref is not None else "stash@{0}")
            if action != "drop":
                self._refuse_stash_with_secrets(root, stash)
            return self._run(root, ["stash", action, stash])
        raise WebCoderError("INVALID_ARGUMENT", "action must be push, pop, apply or drop")

    def _refuse_stash_with_secrets(self, root, stash: str) -> None:
        """Applying a stash that someone made with secret files in it would write them back: refused."""
        real = self._repo(root)
        secrets_only = ["--", *self._secret_specs(root, exclude=False)]
        found = []
        for args in (["diff", "--name-only", "-z", "--no-renames", f"{stash}^1", stash, *secrets_only],
                     ["ls-tree", "-r", "-z", "--name-only", f"{stash}^3", *secrets_only]):  # its untracked part
            done = self._git(real, args)
            if done.returncode == 0:
                found += [name for name in done.stdout.decode("utf-8", "replace").split("\0") if name]
            elif args[0] == "diff":  # (a stash without untracked files has no ^3)
                raise WebCoderError("GIT_FAILED", f"Could not read {stash}, so nothing was applied: "
                                    + done.stderr.decode("utf-8", "replace").strip()[-300:])
        if found:
            raise WebCoderError("GIT_SECRETS_CHANGED", f"{stash} holds changes to secret files "
                                f"({', '.join(found[:10])}), which would be written back; nothing was applied. The "
                                "owner can apply it by hand.")

    def sync(self, root, action, remote=None, branch=None, to=None, prune=False, rebase=False, ff_only=False,
             set_upstream=False, force_with_lease=False, tags=False, delete=False) -> dict:
        real = self._repo(root)
        named = self._remote(real, remote) if remote is not None else None
        if action == "fetch":
            args = ["fetch", *(["--prune"] if prune is True else []), *([named] if named else ["--all"])]
        elif action == "pull":
            if branch is not None and named is None:
                raise WebCoderError("INVALID_ARGUMENT", "branch needs remote")
            result = self._pull(root, real, named, branch, rebase is True, ff_only is True)
            return {**result, "stdout": _mask_remotes(result["stdout"]), "stderr": _mask_remotes(result["stderr"])}
        elif action == "push":
            args = ["push", "--no-verify", *_flags(("--set-upstream", set_upstream), ("--tags", tags),
                                                   ("--force-with-lease", force_with_lease), ("--delete", delete))]
            if (branch is not None or delete is True) and named is None:
                raise WebCoderError("INVALID_ARGUMENT", "branch and delete need remote")
            if delete is True and branch is None:
                raise WebCoderError("INVALID_ARGUMENT", "delete needs the branch to delete on the remote")
            if any(str(name).lower().startswith(("refs/", "webcoder/")) for name in (branch, to) if name):
                raise WebCoderError("INVALID_ARGUMENT", "push takes branch names; refs/... (such as the agent's own "
                                    "snapshots) are not pushed")
            if delete is True:
                args += [named, _branch(branch)]
            else:  # always an explicit refspec: what is checked is exactly what goes (not push.default's pick)
                target, local, dest = self._push_target(real, named, branch, to)
                self._refuse_pushing_secrets(root, target, local, tags is True)
                args += [target, f"{local}:{dest}"]
        else:
            raise WebCoderError("INVALID_ARGUMENT", "action must be fetch, pull or push")
        result = self._run(root, args, timeout=NETWORK_TIMEOUT)
        return {**result, "stdout": _mask_remotes(result["stdout"]), "stderr": _mask_remotes(result["stderr"])}

    def _push_target(self, real: str, remote: str | None, branch: str | None, to: str | None) -> tuple[str, str, str]:
        """(remote, local branch, remote branch) a push goes to: as given, else the current branch to where git
        itself would push it (@{push}: its upstream, pushRemote ...)."""
        if branch is not None:
            return remote, _branch(branch), _branch(to, "to") if to is not None else _branch(branch)
        current = self._git(real, ["symbolic-ref", "--quiet", "--short", "HEAD"]).stdout.decode("utf-8", "replace")
        current = current.strip()
        if not current:
            raise WebCoderError("INVALID_ARGUMENT", "HEAD is not on a branch: pass remote and branch")
        if remote is not None:
            return remote, current, current
        push = self._git(real, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}"])
        where = push.stdout.decode("utf-8", "replace").strip()
        remotes = self._git(real, ["remote"]).stdout.decode("utf-8", "replace").split()
        for name in sorted(remotes, key=len, reverse=True):  # "origin/feature/x": the remote is a known name
            if push.returncode == 0 and where.startswith(name + "/"):
                return name, current, where[len(name) + 1:]
        raise WebCoderError("INVALID_ARGUMENT", f"Where {current} is pushed to is not set up: pass remote and branch")

    def _refuse_pushing_secrets(self, root, remote: str, local: str, tags: bool) -> None:
        """Secrets never leave through WebCoder: a push is refused when commits the target remote does not have yet
        change a secret file (another remote having them does not count). Checked at the push, the one way out,
        whatever made those commits (a cherry-pick of a stash with an uncommitted .env, a merge continued with one
        staged ...). The owner can push such commits by hand."""
        real = self._repo(root)
        # What the remote has is asked at its fetch address; pushes going elsewhere (pushurl, pushInsteadOf)
        # cannot count on that: then every commit pushed is checked.
        fetch_urls, push_urls = (self._git(real, ["remote", "get-url", "--all", *flag, remote]).stdout.split()
                                 for flag in ([], ["--push"]))
        known = self._remote_commits(real, remote) if sorted(fetch_urls) == sorted(push_urls) else []
        # -c: merges show what differs from every parent, so a stash's unstaged side counts, a clean merge's not
        done = self._git(real, ["log", "-c", "--no-renames", "--name-only", "-z", "--format=", local,
                                *(["--tags"] if tags else []), "--stdin", "--",
                                *self._secret_specs(root, exclude=False)], limit=MAX_LISTING,
                         stdin="".join(f"^{commit}\n" for commit in known).encode("ascii"))
        if done.returncode != 0 or done.stdout_cut:
            raise WebCoderError("GIT_FAILED", "Could not check what the push would send, so nothing was pushed: "
                                + done.stderr.decode("utf-8", "replace").strip()[-300:])
        changed = sorted({name for name in done.stdout.decode("utf-8", "replace").split("\0") if name.strip()})
        if changed:
            raise WebCoderError("GIT_SECRETS_PUSH", f"Commits this would push change secret files that {remote} does "
                                f"not have yet ({', '.join(changed[:10])}); secrets are never sent through WebCoder. "
                                "Nothing was pushed; the owner can push these commits by hand.")

    def _remote_commits(self, real: str, remote: str) -> list[str]:
        """The commits the remote's refs point at right now, of those this repository has. Asked, not taken from
        the remote-tracking refs: those show the last fetch, and a branch force-rewound or deleted there since
        (say, to take a leaked secret back) still shows its old commits here. What the remote has and this
        repository does not cannot be named, so what only those contain is checked as not there."""
        listed = self._git(real, ["ls-remote", "--", remote], timeout=NETWORK_TIMEOUT, limit=MAX_LISTING)
        if listed.returncode != 0 or listed.stdout_cut:
            raise WebCoderError("GIT_FAILED", f"Could not ask {remote} which commits it has, so nothing was pushed: "
                                + _mask_remotes(listed.stderr.decode("utf-8", "replace").strip()[-300:]))
        names = sorted({line.split("\t", 1)[0] for line in listed.stdout.decode("ascii", "replace").splitlines()
                        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", line.split("\t", 1)[0])})
        if not names:
            return []
        # never fetched from a promisor remote to answer: an object this repository lacks is just not there
        found = self._git(real, ["cat-file", "--batch-check=%(objectname) %(objecttype)"], limit=MAX_LISTING,
                          extra_env={"GIT_NO_LAZY_FETCH": "1"}, stdin="".join(f"{n}\n" for n in names).encode("ascii"))
        if found.returncode != 0 or found.stdout_cut:
            raise WebCoderError("GIT_FAILED", "Could not check which of the remote's commits are here, so nothing was "
                                "pushed: " + found.stderr.decode("utf-8", "replace").strip()[-300:])
        return [line.split(" ", 1)[0] for line in found.stdout.decode("ascii", "replace").splitlines()
                if line.endswith(" commit")]

    def _pull(self, root, real: str, remote: str | None, branch: str | None, rebase: bool, ff_only: bool) -> dict:
        """What `git pull` does, but merging (or rebasing onto) exactly the commit fetched and checked: a pull
        writes over ignored files in the way too, and would fetch again (maybe more) after the check."""
        # A branch is fetched straight into its remote-tracking ref: the remote's refspec may not cover it (a
        # single-branch clone ...), and then that ref would be missing or old while only FETCH_HEAD had it.
        wanted = [f"+refs/heads/{_branch(branch)}:refs/remotes/{remote}/{branch}"] if branch else []
        fetched = self._run(root, ["fetch", *([remote] if remote else []), *wanted], timeout=NETWORK_TIMEOUT)
        if fetched["exit_code"] != 0:
            return fetched
        if branch is not None:
            target, label = f"refs/remotes/{remote}/{branch}", f"{remote}/{branch}"
        else:
            upstream = self._git(real, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
            label = upstream.stdout.decode("utf-8", "replace").strip()
            if upstream.returncode != 0 or not label or remote and not label.startswith(remote + "/"):
                raise WebCoderError("GIT_FAILED", "This branch has no upstream" + (f" on {remote}" if remote else "")
                                    + " to pull from: pass remote and branch")
            target = "@{upstream}"
        resolved = self._git(real, ["rev-parse", "--verify", "--quiet", target + "^{commit}"])
        if resolved.returncode != 0:
            raise WebCoderError("GIT_FAILED", f"{label} was not fetched: pass remote and branch")
        commit = resolved.stdout.decode("ascii", "replace").strip()
        setting = "" if ff_only else self._pull_setting(real)  # asked for fast-forward only: never a rebase
        rebasing = (rebase and not ff_only) or setting in ("true", "yes", "on", "1", "merges", "m", "interactive", "i")
        self._refuse_overwriting_untracked(root, names=self._touched(root, [f"HEAD...{commit}"])
                                           + (self._replayed(root, commit) if rebasing else []))
        if rebasing:
            args = ["rebase", *(["--rebase-merges"] if not rebase and setting in ("merges", "m") else []), commit]
        else:
            ff = self._git(real, ["config", "--get", "pull.ff"]).stdout.decode("utf-8", "replace").strip().lower()
            args = ["merge", "--no-edit", *(["--ff-only"] if ff_only or ff == "only" else
                                            ["--no-ff"] if ff in ("false", "no", "off", "0") else []),
                    "-m", f"Merge remote-tracking branch '{label}'", commit]
        done = self._run(root, args)
        return {**done, "command": f"{fetched['command']} && {done['command']}",
                "stdout": "\n".join(part for part in (fetched["stdout"], done["stdout"]) if part),
                "stderr": "\n".join(part for part in (fetched["stderr"], done["stderr"]) if part)}

    def _pull_setting(self, real: str) -> str:
        """How the owner has pulls integrate: branch.<name>.rebase, else pull.rebase (merge when unset)."""
        current = self._git(real, ["symbolic-ref", "--quiet", "--short", "HEAD"]).stdout.decode("utf-8", "replace")
        for key in ([f"branch.{current.strip()}.rebase"] if current.strip() else []) + ["pull.rebase"]:
            found = self._git(real, ["config", "--get", key])
            if found.returncode == 0:
                return found.stdout.decode("utf-8", "replace").strip().lower()
        return ""

    def integrate(self, root, operation, step="start", ref=None, refs=None, no_ff=False, squash=False,
                  message=None) -> dict:
        commands = {"merge": "merge", "rebase": "rebase", "cherry_pick": "cherry-pick", "revert": "revert"}
        if operation not in commands:
            raise WebCoderError("INVALID_ARGUMENT", "operation must be merge, rebase, cherry_pick or revert")
        command = commands[operation]
        if step in ("abort", "continue", "skip"):
            if step == "skip" and operation == "merge":
                raise WebCoderError("INVALID_ARGUMENT", "A merge cannot skip; abort or continue it")
            if step == "continue":  # it commits the index, as git_commit would: not with secrets staged in it
                self._refuse_staged_secrets(root)
            else:  # both throw away what the stop left in the index and work tree (a rebase abort is a hard reset)
                self._refuse_discarding_secrets(root)
            if step != "abort" and operation != "merge":  # it goes on applying commits
                self._refuse_overwriting_untracked(root, names=self._still_to_apply(root, command))
            return self._run(root, [command, f"--{step}"])
        if step != "start":
            raise WebCoderError("INVALID_ARGUMENT", "step must be start, abort, continue or skip")
        # git overwrites ignored files that are in the way of what comes in (a local .env ...): not here
        if operation == "merge":
            args = ["merge", "--no-edit", *_flags(("--no-ff", no_ff), ("--squash", squash))]
            self._refuse_overwriting_untracked(root, names=self._touched(root, [f"HEAD...{_ref(ref)}"]))
            return self._run(root, args + (["-m", _text(message, "message")] if message else []) + [_ref(ref)])
        if operation == "rebase":  # never interactive: no todo list, no --exec
            self._refuse_overwriting_untracked(root, names=self._touched(root, [f"HEAD...{_ref(ref)}"])
                                               + self._replayed(root, _ref(ref)))
            return self._run(root, ["rebase", _ref(ref)])
        if operation == "revert":
            self._refuse_overwriting_untracked(root, names=self._touched(root, [_ref(ref)], one_commit=True))
            return self._run(root, ["revert", "--no-edit", _ref(ref)])
        picks = refs if refs is not None else ([ref] if ref is not None else None)
        if not isinstance(picks, list) or not 1 <= len(picks) <= 100:
            raise WebCoderError("INVALID_ARGUMENT", "cherry_pick needs ref or refs (1-100 commits)")
        picks = [_ref(item, "refs") for item in picks]
        self._refuse_overwriting_untracked(root, names=[name for pick in picks
                                                        for name in self._touched(root, [pick], one_commit=True)])
        return self._run(root, ["cherry-pick", *picks])

    def tag(self, root, action, name, ref=None, message=None) -> dict:
        tag = _branch(name, "name")
        if action == "create":
            args = ["tag", *(["-a", "-m", _text(message, "message")] if message else []), tag]
            return self._run(root, args + ([_ref(ref)] if ref is not None else []))
        if action == "delete":
            return self._run(root, ["tag", "-d", tag])
        raise WebCoderError("INVALID_ARGUMENT", "action must be create or delete")

    def clean(self, root, dry_run=True, directories=False, ignored=False, paths=None) -> dict:
        real = self._repo(root)
        scope = self._paths(root, paths, default=".")
        # With a pathspec (always here), git removes untracked folders as a whole even without -d, and does not
        # look at exclude pathspecs inside them: folders are kept out here, all of them without `directories`.
        folders, kept = self._untracked_folders(real, scope, ignored is True)
        args = ["clean", "-n" if dry_run is not False else "-f"]
        args += [flag for flag, on in (("-d", directories), ("-x", ignored)) if on is True]
        skipped = kept if directories is True else folders
        result = self._run(root, [*args, "--", *scope, *self._excluded(root),
                                  *(f":(exclude,literal){folder}" for folder in skipped)])
        if kept and directories is True:
            result["stdout"] += ("\n" if result["stdout"] else "") + "Kept, because they hold protected files: " \
                + ", ".join(kept)
        return result

    def _untracked_folders(self, real: str, scope: list[str], ignored: bool) -> tuple[list[str], list[str]]:
        """The untracked folders git clean would delete as a whole, and those of them that hold deny-listed files
        or the agent's own folder (git does not look at exclude pathspecs inside a folder it removes)."""
        args = ["ls-files", "-z", "--others", "--directory", *([] if ignored else ["--exclude-standard"]), "--", *scope]
        listed = self._git(real, args, limit=MAX_LISTING)
        if listed.stdout_cut or listed.returncode != 0:  # a folder missed here could go as a whole, secrets and all
            raise WebCoderError("GIT_FAILED", "Could not list every untracked folder to check it holds no protected "
                                "files, so nothing was cleaned; clean a smaller part with paths")
        folders, kept = [], []
        for entry in listed.stdout.decode("utf-8", "replace").split("\0"):
            if not entry.endswith("/"):
                continue
            folder = entry.rstrip("/")
            folders.append(folder)
            if any(is_within(agent, os.path.join(real, folder)) for agent in self.files.sb.protected_dirs):
                kept.append(folder)
                continue
            for dirpath, _dirnames, filenames in os.walk(os.path.join(real, folder)):
                inside = os.path.relpath(dirpath, real).replace("\\", "/")
                if any(self.files.sb.is_denied(f"{inside}/{name}", real) for name in filenames):
                    kept.append(folder)
                    break
        return folders, kept

    def init(self, root, initial_branch=None) -> dict:
        real = self.files.sb.root(root)
        args = ["init", *(["--initial-branch", _branch(initial_branch, "initial_branch")] if initial_branch else [])]
        done = self._git(real, args)
        return {"command": "git " + " ".join(args), "exit_code": done.returncode,
                "stdout": done.stdout.decode("utf-8", "replace"), "stderr": done.stderr.decode("utf-8", "replace"),
                "truncated": False, "snapshot": None}

    def run(self, root, args, stdin=None, timeout=LOCAL_TIMEOUT) -> dict:
        """Any git command in the project folder, unconfined; only prompts are off (nobody is here to answer)."""
        if (not isinstance(args, list) or not 1 <= len(args) <= 500
                or any(not isinstance(a, str) or "\x00" in a for a in args) or sum(len(a) + 3 for a in args) > 30_000):
            raise WebCoderError("INVALID_ARGUMENT", "args must be a list of 1-500 strings (at most 30,000 characters "
                                "in all), without git itself")
        if stdin is not None and (not isinstance(stdin, str) or len(stdin) > MAX_STDIN):
            raise WebCoderError("INVALID_ARGUMENT", f"stdin must be text of at most {MAX_STDIN:,} characters")
        timeout = _count(timeout, "timeout", 1, 600)
        real = self.files.sb.root(root)
        done = self._git(real, args, timeout, stdin=None if stdin is None else stdin.encode("utf-8"), confined=False)
        return {"command": shlex.join(["git", *args]), "exit_code": done.returncode,
                "stdout": _mask(done.stdout.decode("utf-8", "replace")),
                "stderr": _mask(done.stderr.decode("utf-8", "replace")),
                "truncated": done.stdout_cut or done.stderr_cut, "snapshot": None}
