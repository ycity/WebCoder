"""File operations executed by the agent inside its allowed roots.

The hub sends (root, relative path) pairs. The root must lie inside one of this
device's ``allowed_roots`` and every resolved target (after following symlinks)
must stay inside that root. Writes are atomic, guarded by the caller's expected
SHA-256 (optimistic locking), and keep a pre-image backup outside the project.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import difflib
import errno
import fnmatch
import hashlib
import inspect
import itertools
import json
import os
import platform
import re
import shutil
import socket
import stat as stat_mod
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from webcoder import VERSION
from webcoder.common.errors import WebCoderError

try:  # per-match timeout and GIL release: stdlib `re` can backtrack for hours and freeze the whole agent
    import regex as regex_engine
except ImportError:  # pragma: no cover - installed with the agent package
    regex_engine = None

IS_WINDOWS = os.name == "nt"
BOM = b"\xef\xbb\xbf"
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
BACKUP_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# Heavy or tool-owned folders: summarized in listings, skipped by search.
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".idea", ".vs", ".gradle", ".next", ".nuxt", ".turbo", ".cache",
})
# Secrets are never read or written through WebCoder. Patterns are matched
# case-insensitively against the file name, or the relative path if they contain "/".
DEFAULT_DENY = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    ".ssh/*", ".aws/credentials", ".docker/config.json",
    "master.key", "hub.db*", "hub-*.db",  # a WebCoder hub's data (every user's accounts) on the same computer
)
DENY_EXCEPTIONS = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults"})
ENV_PATTERNS = frozenset({".env", ".env.*"})  # the only rules DENY_EXCEPTIONS lift
MiB = 1024 * 1024
SCAN_ENTRIES = 200_000  # folder entries one find_files or search reads at most


@dataclass
class Limits:
    max_text_bytes: int = 5 * MiB          # largest file read_file/edit_file will decode
    max_return_chars: int = 200_000        # characters of file content returned per call
    max_line_chars: int = 2_000            # longer lines are shortened in read_file output
    max_write_bytes: int = 16 * MiB
    max_transfer_bytes: int = 16 * MiB     # base64 reads (cross-device copy)
    max_hash_bytes: int = 512 * MiB        # stat computes sha256 up to this size
    search_file_bytes: int = 2 * MiB
    search_seconds: float = 20.0
    regex_line_seconds: float = 5.0        # one regex match taking longer than this is pathological
    list_scan_cap: int = 20_000


@dataclass(frozen=True)
class Target:
    root: str      # real path of the project root
    rel: str       # normalized relative path using "/" ("" means the root)
    lexical: str   # root joined with rel, symlinks not resolved
    path: str      # fully resolved real path

    @property
    def display(self) -> str:
        return self.rel or "."


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(MiB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_eol(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "crlf"
    return "lf" if lf else "none"


def decode_text(data: bytes) -> tuple[str, str]:
    body, encoding = (data[3:], "utf-8-sig") if data.startswith(BOM) else (data, "utf-8")
    if b"\x00" in body[:8192]:
        raise WebCoderError("BINARY_FILE", "File looks binary; read it with encoding='base64' or use stat")
    try:
        return body.decode("utf-8"), encoding
    except UnicodeDecodeError as exc:
        raise WebCoderError("NOT_UTF8", "File is not valid UTF-8 text; read it with encoding='base64'",
                            byte_offset=exc.start) from None


def is_within(child: str, parent: str) -> bool:
    child, parent = os.path.normcase(child), os.path.normcase(parent)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives on Windows
        return False


def _int(value, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise WebCoderError("INVALID_ARGUMENT", f"Expected an integer, got {value!r}")
    return max(low, min(high, int(value)))


class PathGlob:
    """A path glob with the usual meaning: * ? [...] stay within one folder, a `**` part is zero or more folders
    (at the end, "dir/**": one or more, what is inside dir), and ** within a name is a plain * (as in .gitignore).
    Matched part by part (dynamic programming over pattern parts x path parts), never as one regular expression:
    a pattern such as **/**/**/.../x would backtrack combinatorially there and hold the agent for minutes."""

    _ANY = None

    def __init__(self, pattern: str):
        parts = pattern.lstrip("/").removeprefix("./").split("/")
        self.parts: list[re.Pattern | None] = []
        for part in parts:
            if part == "**":
                if not self.parts or self.parts[-1] is not self._ANY:  # **/** is one **
                    self.parts.append(self._ANY)
            else:  # fnmatch's translation cannot backtrack badly on runs of *
                self.parts.append(re.compile(fnmatch.translate(part)))
        self.inside = parts[-1] == "**"

    def match(self, path: str) -> bool:
        names = path.split("/")
        reach = [True] + [False] * len(names)  # reach[j]: the parts so far can end just before names[j]
        for index, part in enumerate(self.parts):
            ahead = [False] * (len(names) + 1)
            if part is self._ANY:
                seen, last = False, index == len(self.parts) - 1 and self.inside
                for j in range(len(names) + 1):
                    ahead[j] = seen if last else (seen or reach[j])  # a trailing ** takes at least one name
                    seen = seen or reach[j]
            else:
                for j, name in enumerate(names):
                    if reach[j] and part.match(name):
                        ahead[j + 1] = True
            reach = ahead
            if not any(reach):
                return False
        return reach[len(names)]


def path_glob(pattern: str) -> PathGlob:
    return PathGlob(pattern)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


def _unlink_quietly(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


# link() errors meaning "no hard links on this filesystem" (FAT, some network and FUSE mounts), not "exists".
_NO_HARD_LINKS = {errno.EPERM, errno.ENOSYS, errno.EXDEV, errno.EMLINK, getattr(errno, "ENOTSUP", errno.EPERM),
                  getattr(errno, "EOPNOTSUPP", errno.EPERM)}
_RENAME_REPLACES = not IS_WINDOWS  # POSIX rename() silently replaces the destination; Windows refuses


def publish_new(source: str, target: str) -> None:
    """Give the file `source` the name `target` without ever replacing what is there: FileExistsError if
    `target` exists, even if another program created it a moment ago. `source` is gone afterwards.
    os.replace would silently overwrite such a file; the in-process path locks cannot stop other programs."""
    if not _RENAME_REPLACES:
        os.rename(source, target)
        return
    try:
        os.link(source, target)  # atomic: EEXIST instead of replacing
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
        _reserve_and_replace(source, target)
        return
    os.unlink(source)


class _Budget:
    """How many folder entries a walk may still read, in all; `cut` once it had to leave some unread."""

    def __init__(self, entries: int):
        self.left, self.cut = entries, False


def _walk(top: str, budget: _Budget):
    """os.walk (top-down: prune by editing the folder list; links and junctions to folders are listed, not
    entered), in name order, reading no more entries than the budget allows: a folder with millions of entries is
    never read (or sorted) whole, which os.walk does before a caller can count anything."""
    stack = [top]
    while stack:
        path = stack.pop()
        if budget.left <= 0:
            budget.cut = True
            return
        try:
            with os.scandir(path) as entries:
                chunk = list(itertools.islice(entries, budget.left + 1))
        except OSError:
            continue
        if len(chunk) > budget.left:
            chunk, budget.cut = chunk[:budget.left], True
        budget.left -= len(chunk)
        folders, files, links = [], [], set()
        for entry in chunk:
            try:
                is_folder = entry.is_dir()
                if is_folder and (entry.is_symlink() or is_link_or_junction(entry.path)):
                    links.add(entry.name)
            except OSError:
                is_folder = False
            (folders if is_folder else files).append(entry.name)
        folders.sort()
        files.sort()
        yield path, folders, files
        stack.extend(os.path.join(path, name) for name in reversed(folders) if name not in links)


def _refuse_junctions_inside(folder: str) -> None:
    """Copying a folder to another filesystem keeps symbolic links as links, but copies a Windows junction (or a
    mounted volume) as a plain folder with everything it leads to: files from outside the project would become
    project files. Such a folder is not moved across; renaming within one filesystem moves the junction itself."""
    budget = _Budget(10_000_000)  # the move's own check (list_scan_cap) came first
    for dirpath, dirnames, _files in _walk(folder, budget):
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if is_link_or_junction(full) and not os.path.islink(full):
                raise WebCoderError("FOLDER_HAS_JUNCTIONS", "This folder holds a junction or mounted volume "
                                    f"({os.path.relpath(full, folder)}); moved to another filesystem it would be "
                                    "copied as a plain folder with all it leads to. Move what is around it, or remove "
                                    "it first")


def _tree_state(path: str) -> dict:
    """Every entry under a folder with its type, size, time and identity: to notice changes while it is copied."""
    state = {}
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
                state[os.path.relpath(full, path)] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
            except OSError:
                state[os.path.relpath(full, path)] = None
    return state


def _source_changed(copied: bool = False) -> WebCoderError:
    if copied:
        return WebCoderError("SOURCE_CHANGED", "The source changed while it was being moved to the other filesystem, "
                             "after the copy was put in place: both were kept (the copy may be the older version); "
                             "compare them before deleting either")
    return WebCoderError("SOURCE_CHANGED", "The source changed while it was being copied to the other filesystem, so "
                         "nothing was moved; read it again and retry")


def move_across_filesystems(source: str, destination: str, is_dir: bool,
                            seen: os.stat_result | None = None) -> None:
    """A move that rename() cannot do (another filesystem): copy next to the destination, on its filesystem, put
    the copy in place without replacing anything (FileExistsError), then remove the original. Not when the
    original changed meanwhile (for a file: since `seen`, the version that was checked): the copy is dropped
    before it is in place; once in place it may be in use already, so then both are kept."""
    parent = os.path.dirname(destination)
    if is_dir:
        _refuse_junctions_inside(source)
        before = _tree_state(source)
        temp = tempfile.mkdtemp(prefix=".webcoder-", dir=parent)
        try:
            shutil.copytree(source, temp, symlinks=True, dirs_exist_ok=True)
            if _tree_state(source) != before:
                raise _source_changed()
            if _RENAME_REPLACES:  # POSIX rename() replaces an empty folder: take the name first (FileExistsError)
                os.mkdir(destination)
                try:
                    os.rename(temp, destination)  # over our own empty folder
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.rmdir(destination)
                    raise
            else:
                os.rename(temp, destination)  # Windows never replaces anything
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        if _tree_state(source) != before:
            raise _source_changed(copied=True)
        shutil.rmtree(source)
        return
    seen = seen or os.stat(source)
    fd, temp = tempfile.mkstemp(prefix=".webcoder-", suffix=".tmp", dir=parent)
    os.close(fd)
    try:
        shutil.copy2(source, temp)
        if not _same_version(source, seen):
            raise _source_changed()
        publish_new(temp, destination)
    except BaseException:
        _unlink_quietly(temp)
        raise
    if not _same_version(source, seen):
        raise _source_changed(copied=True)
    os.unlink(source)


def _reserve_and_replace(source: str, target: str) -> None:
    """publish_new without hard links: reserve `target` with an exclusive create (O_EXCL), then rename the
    finished file over that empty placeholder in one step. Nobody else's file is replaced and the content
    appears whole; only a crash between the two steps could leave an empty file."""
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        os.replace(source, target)
    except BaseException:
        _unlink_quietly(target)
        raise


class Sandbox:
    """Maps (root, relative path) to real paths and enforces the boundaries."""

    def __init__(self, allowed_roots, *, deny=DEFAULT_DENY, read_only: bool = False, protected_dirs=()):
        self.allowed_roots = [os.path.realpath(os.path.expanduser(str(root))) for root in allowed_roots]
        self.deny = tuple(pattern.lower() for pattern in deny)
        self.read_only = read_only
        self.protected_dirs = [os.path.realpath(str(path)) for path in protected_dirs]

    def root(self, root: str) -> str:
        if not isinstance(root, str) or not root.strip():
            raise WebCoderError("INVALID_ROOT", "Project root is missing")
        expanded = os.path.expanduser(root.strip())
        if not os.path.isabs(expanded):
            raise WebCoderError("INVALID_ROOT", "Project root must be an absolute path", root=root)
        real = os.path.realpath(expanded)
        if not any(is_within(real, allowed) for allowed in self.allowed_roots):
            raise WebCoderError("ROOT_NOT_ALLOWED", "Project root is outside this device's allowed_roots "
                                "(add it with: webcoder-agent roots add <folder>)", root=root)
        if self.is_protected(real):
            raise WebCoderError("ROOT_NOT_ALLOWED", "The agent's own data folder cannot be a project", root=root)
        if not os.path.isdir(real):
            raise WebCoderError("ROOT_NOT_FOUND", "Project root does not exist or is not a folder", root=root)
        return real

    def resolve(self, root: str, rel, *, write: bool = False) -> Target:
        base = self.root(root)
        parts = self._parts(rel)
        rel_clean = "/".join(parts)
        lexical = os.path.join(base, *parts) if parts else base
        real = os.path.realpath(lexical)
        if not is_within(real, base):
            raise WebCoderError("PATH_ESCAPE", "Path resolves outside the project root (symlink or junction)",
                                path=rel_clean)
        real_rel = os.path.relpath(real, base).replace("\\", "/")
        real_rel = "" if real_rel == "." else real_rel
        candidates = {rel_clean, real_rel}
        if any(_git_data(candidate.lower()) for candidate in candidates):
            raise WebCoderError("PATH_DENIED", "git's own folder (.git) is off limits to file tools: it can hold "
                                "credentials. Use the git tools instead.", path=rel_clean)
        if any(self.is_denied(candidate, base) for candidate in candidates):
            raise WebCoderError("PATH_DENIED", "This file is blocked by the agent's deny list (secrets and keys)",
                                path=rel_clean)
        if self.is_protected(real):  # reads too: its config.json holds the device secret
            raise WebCoderError("PATH_DENIED", "The agent's own data folder (credentials, backups) is off limits",
                                path=rel_clean)
        if write:
            if self.read_only:
                raise WebCoderError("AGENT_READ_ONLY", "The agent on this device is configured read-only")
            # The whole path, not just the part inside the project: a project rooted at a repo's .git is no way
            # to plant hooks.
            if any(part.lower() == ".git" for path in (real, lexical) for part in re.split(r"[\\/]", path)):
                raise WebCoderError("PATH_DENIED", "Writing inside .git is not allowed", path=rel_clean)
        return Target(root=base, rel=rel_clean, lexical=lexical, path=real)

    def is_protected(self, real: str) -> bool:
        return any(is_within(real, protected) for protected in self.protected_dirs)

    def walkable(self, path: str, root: str) -> bool:
        """May a folder walk (listing, search) enter `path`? Walks read files without resolve(), so they
        never follow links: a junction or symlinked folder could lead out of the project, into the agent's
        folder, or onto a denied path under a harmless name. Its target, if inside, is walked as itself."""
        if self.is_link(path):
            return False
        real = os.path.realpath(path)
        return is_within(real, root) and not self.is_protected(real)

    @staticmethod
    def is_link(path: str) -> bool:
        """A symlink or a Windows junction: its real location differs from where it appears."""
        return os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path))

    @staticmethod
    def _parts(rel) -> list[str]:
        if rel is None:
            rel = "."
        if not isinstance(rel, str):
            raise WebCoderError("INVALID_PATH", "Path must be a string")
        if "\x00" in rel:
            raise WebCoderError("INVALID_PATH", "Path contains a NUL character")
        text = rel.replace("\\", "/")  # not stripped: " a.txt " is another file than "a.txt" on Linux / macOS
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            raise WebCoderError("INVALID_PATH", "Use a path relative to the project root, not an absolute path",
                                path=rel)
        parts = []
        for part in text.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                raise WebCoderError("INVALID_PATH", "'..' is not allowed; paths stay inside the project", path=rel)
            if IS_WINDOWS:
                if ":" in part:
                    raise WebCoderError("INVALID_PATH", "':' is not allowed in file names", path=rel)
                if part.endswith((" ", ".")):
                    raise WebCoderError("INVALID_PATH", "File names cannot end with a dot or space on Windows",
                                        path=rel)
                if part.split(".")[0].upper() in WINDOWS_RESERVED:
                    raise WebCoderError("INVALID_PATH", f"'{part}' is a reserved device name on Windows", path=rel)
            parts.append(part)
        return parts

    def is_denied(self, rel: str, root: str | None = None) -> bool:
        """Is `rel` (inside the project at real path `root`) a secret the deny list keeps out? Judged on the whole
        path too: an allowed folder or project rooted at ~/.ssh, ~/.aws or inside a .git gets no way round it."""
        if not rel:
            return False
        return self._denied(rel) or bool(root) and self._denied(f"{root}/{rel}".replace("\\", "/"))

    def _denied(self, rel: str) -> bool:
        low = rel.lower()
        if _git_data(low):
            return True
        name = low.rsplit("/", 1)[-1]
        exempt = name in DENY_EXCEPTIONS  # sample env files: exempt from the .env rules only
        for pattern in self.deny:
            if exempt and pattern in ENV_PATTERNS:
                continue
            if "/" in pattern:
                if fnmatch.fnmatchcase(low, pattern) or fnmatch.fnmatchcase(low, "*/" + pattern):
                    return True
            elif fnmatch.fnmatchcase(name, pattern):
                return True
        return False


def is_link_or_junction(path: str) -> bool:
    """Is `path` itself a symlink, or a Windows junction (which os.path.islink does not report)? Not: is it inside
    a linked folder."""
    if os.path.islink(path):
        return True
    if hasattr(os.path, "isjunction"):  # Python 3.12+
        return os.path.isjunction(path)
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0) == 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return False


def _renames_itself(source: str, target: str, lexical: str) -> bool:
    """Is the destination (real path `target`, `lexical` as asked) the source entry itself, spelled another way: a
    case-only rename where case does not count (Windows, macOS)? Judged by the file, not by os.path.normcase (which
    folds case on Windows only). Not a link to the source, nor another name (hard link) of the same file where case
    counts: renaming onto that would do nothing at all."""
    if source.casefold() != target.casefold() or is_link_or_junction(lexical):
        return False
    try:
        if not os.path.samestat(os.lstat(source), os.lstat(target)):
            return False
        name = os.path.basename(target)
        return name == os.path.basename(source) or name not in os.listdir(os.path.dirname(target))
    except OSError:
        return False


def _same_version(path: str, seen: os.stat_result) -> bool:
    """Is `path` still the file `seen` described (not changed, not replaced by a new file)?"""
    try:
        now = os.stat(path)
    except OSError:
        return False
    return (now.st_mtime_ns, now.st_size, now.st_ino) == (seen.st_mtime_ns, seen.st_size, seen.st_ino)


def _git_data(low: str) -> bool:
    """git's own data, whatever the deny list says: its config can hold credentials (a token in a remote URL, a
    CI checkout's auth header), and rebase / am state holds patches of secret files. The git tools are the way
    in; they mask credentials and leave secrets out."""
    parts = low.split("/")
    return ".git" in parts or len(parts) > 1 and parts[-2].endswith(".git") and parts[-1] in (
        "config", "config.worktree")  # a bare repository's


def make_private_dir(path, *, ours: bool = False) -> None:
    """Owner-only folder on POSIX (Windows folders inherit the private ACL of the user profile).
    An existing folder is only tightened when it is certainly the agent's own (`ours`)."""
    created = not os.path.isdir(path)
    os.makedirs(path, exist_ok=True)
    if not IS_WINDOWS and (created or ours):
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


class BackupStore:
    """Pre-image copies of changed files, kept outside project trees and readable by the owner only."""

    def __init__(self, directory, keep_per_file: int = 20, max_total_bytes: int = 512 * MiB):
        self.directory = Path(directory)
        make_private_dir(self.directory, ours=True)
        self.keep_per_file = max(1, keep_per_file)
        self.max_total_bytes = max_total_bytes
        self._lock = threading.Lock()

    def save(self, target: Target, data: bytes, reason: str) -> str:
        backup_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        try:  # so restoring a deleted private file does not come back readable by everyone
            mode = stat_mod.S_IMODE(os.stat(target.path).st_mode)
        except OSError:
            mode = None
        meta = {"id": backup_id, "root": target.root, "path": target.rel, "size": len(data),
                "sha256": sha256_bytes(data), "time": time.time(), "reason": reason, "mode": mode}
        with self._lock:
            _write_private(self.directory / f"{backup_id}.bin", data)
            _write_private(self.directory / f"{backup_id}.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
            self._prune(meta)
        return backup_id

    def save_file(self, target: Target, reason: str, keep: str | None = None) -> tuple[str, str]:
        """Back up the file at target.path by streaming it (for files too big to hold in memory); making room
        never drops the backup `keep` (the one being restored). Returns (backup_id, sha256 of what was copied)."""
        backup_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        info = os.stat(target.path)
        digest, size = hashlib.sha256(), 0
        destination = self.directory / f"{backup_id}.bin"
        with self._lock:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
            try:
                with os.fdopen(fd, "wb") as out, open(target.path, "rb") as source:
                    for chunk in iter(lambda: source.read(MiB), b""):
                        size += len(chunk)
                        digest.update(chunk)
                        out.write(chunk)
            except BaseException:
                with contextlib.suppress(OSError):
                    destination.unlink()
                raise
            meta = {"id": backup_id, "root": target.root, "path": target.rel, "size": size,
                    "sha256": digest.hexdigest(), "time": time.time(), "reason": reason,
                    "mode": stat_mod.S_IMODE(info.st_mode)}
            _write_private(self.directory / f"{backup_id}.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
            self._prune(meta, keep)
        return backup_id, meta["sha256"]

    def discard(self, backup_id: str) -> None:
        with self._lock:
            self._delete(backup_id)

    def list(self, root: str, rel: str | None = None, limit: int = 50) -> list[dict]:
        return [meta for meta in self._all() if self._matches(meta, root, rel)][:limit]

    def load(self, root: str, backup_id) -> tuple[dict, Path]:
        """(metadata, path of the verified backup copy); checked as a stream, so any size is fine."""
        if not isinstance(backup_id, str) or not BACKUP_ID_RE.match(backup_id):
            raise WebCoderError("INVALID_ARGUMENT", "backup_id is not a valid backup identifier")
        path = self.directory / f"{backup_id}.bin"
        try:
            meta = json.loads((self.directory / f"{backup_id}.json").read_text(encoding="utf-8"))
            actual = sha256_file(str(path))
        except (OSError, ValueError):
            raise WebCoderError("BACKUP_NOT_FOUND", "Backup does not exist (it may have been pruned)",
                                backup_id=backup_id) from None
        if not self._matches(meta, root):
            raise WebCoderError("BACKUP_NOT_FOUND", "Backup does not belong to this project", backup_id=backup_id)
        if actual != meta.get("sha256"):
            raise WebCoderError("BACKUP_CORRUPT", "Backup content does not match its recorded sha256",
                                backup_id=backup_id)
        return meta, path

    def _all(self) -> list[dict]:
        items = []
        for meta_path in self.directory.glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and BACKUP_ID_RE.match(str(meta.get("id", ""))):
                items.append(meta)
        items.sort(key=lambda meta: meta.get("time", 0), reverse=True)
        return items

    @staticmethod
    def _matches(meta: dict, root: str, rel: str | None = None) -> bool:
        if os.path.normcase(str(meta.get("root", ""))) != os.path.normcase(root):
            return False
        return rel is None or os.path.normcase(str(meta.get("path", ""))) == os.path.normcase(rel)

    def _delete(self, backup_id: str) -> None:
        for suffix in (".bin", ".json"):
            with contextlib.suppress(FileNotFoundError):
                (self.directory / f"{backup_id}{suffix}").unlink()

    def _prune(self, newest: dict, keep: str | None = None) -> None:
        items = self._all()
        same_file = [meta for meta in items if self._matches(meta, newest["root"], newest["path"])]
        dropped = {meta["id"] for meta in same_file[self.keep_per_file:]} - {keep}
        for backup_id in dropped:
            self._delete(backup_id)
        remaining = [meta for meta in items if meta["id"] not in dropped]
        total = sum(int(meta.get("size", 0)) for meta in remaining)
        for meta in reversed(remaining):  # oldest first
            if total <= self.max_total_bytes:
                break
            if meta["id"] not in (newest["id"], keep):
                self._delete(meta["id"])
                total -= int(meta.get("size", 0))


class PathLocks:
    """Per-path locks so concurrent writes to one file cannot interleave. A path's lock is dropped once nobody
    holds or waits for it: a long-running agent touches ever new paths (screenshots, new files ...)."""

    def __init__(self):
        self._locks: dict[str, list] = {}  # key -> [lock, holders and waiters]
        self._guard = threading.Lock()

    @contextlib.contextmanager
    def hold(self, *paths: str):
        keys = sorted({os.path.normcase(path) for path in paths})
        with self._guard:
            entries = [self._locks.setdefault(key, [threading.Lock(), 0]) for key in keys]
            for entry in entries:
                entry[1] += 1
        try:
            with contextlib.ExitStack() as stack:
                for entry in entries:
                    stack.enter_context(entry[0])
                yield
        finally:
            with self._guard:
                for key, entry in zip(keys, entries, strict=True):
                    entry[1] -= 1
                    if entry[1] == 0:
                        del self._locks[key]


OPERATIONS = {
    "fs.list": "list_dir",
    "fs.read": "read_file",
    "fs.read_many": "read_many",
    "fs.search": "search",
    "fs.find": "find_files",
    "fs.stat": "stat",
    "fs.write": "write_file",
    "fs.edit": "edit_file",
    "fs.delete": "delete_path",
    "fs.move": "move_path",
    "fs.mkdir": "make_dir",
    "fs.backups": "list_backups",
    "fs.restore": "restore_backup",
    "agent.info": "info",
    "agent.browse": "browse",
    "agent.check_root": "check_root",
}


class FileOps:
    """All operations the hub may request. Methods run in worker threads."""

    def __init__(self, sandbox: Sandbox, backups: BackupStore, limits: Limits | None = None):
        self.sb = sandbox
        self.backups = backups
        self.limits = limits or Limits()
        self.mobile = None  # MobileOps, attached by AgentConfig.build_fileops
        self.ssh = None  # SshOps, attached by AgentConfig.build_fileops
        self.git = None  # GitOps, attached by AgentConfig.build_fileops
        self.skills = None  # SkillOps, attached by AgentConfig.build_fileops
        self.cli = None  # CommandOps, attached by AgentConfig.build_fileops
        self._locks = PathLocks()
        if IS_WINDOWS:
            self._new_file_mode = None
        else:
            mask = os.umask(0)
            os.umask(mask)
            self._new_file_mode = 0o666 & ~mask

    # ------------------------------------------------------------------ dispatch
    def dispatch(self, op: str, args: dict) -> dict:
        if not isinstance(args, dict):
            raise WebCoderError("INVALID_ARGUMENT", "Operation arguments must be an object")
        if op.startswith("mobile.") and self.mobile is not None and op in self.mobile.OPERATIONS:
            call = lambda: self.mobile.dispatch(op, args)  # noqa: E731 - same error translation as file ops
        elif op.startswith("ssh.") and self.ssh is not None and op in self.ssh.OPERATIONS:
            call = lambda: self.ssh.dispatch(op, args)  # noqa: E731
        elif op.startswith("git.") and self.git is not None and op in self.git.OPERATIONS:
            call = lambda: self.git.dispatch(op, args)  # noqa: E731
        elif op.startswith("skills.") and self.skills is not None and op in self.skills.OPERATIONS:
            call = lambda: self.skills.dispatch(op, args)  # noqa: E731
        elif op.startswith("cli.") and self.cli is not None and op in self.cli.OPERATIONS:
            call = lambda: self.cli.dispatch(op, args)  # noqa: E731
        else:
            name = OPERATIONS.get(op)
            if name is None:
                raise WebCoderError("UNKNOWN_OPERATION", f"This agent does not support {op!r}; upgrade the agent")
            method = getattr(self, name)
            try:
                inspect.signature(method).bind(**args)
            except TypeError as exc:
                raise WebCoderError("INVALID_ARGUMENT", f"Invalid arguments for {op}: {exc}") from None
            call = lambda: method(**args)  # noqa: E731
        try:
            return call()
        except WebCoderError:
            raise
        except PermissionError as exc:
            raise WebCoderError("PERMISSION_DENIED",
                                f"The operating system denied access: {exc.strerror or exc}") from None
        except FileNotFoundError as exc:
            raise WebCoderError("NOT_FOUND", exc.strerror or "Not found") from None
        except OSError as exc:
            raise WebCoderError("IO_ERROR", f"{type(exc).__name__}: {exc.strerror or exc}") from None

    # ------------------------------------------------------------------ helpers
    def _stat_file(self, t: Target) -> os.stat_result:
        try:
            st = os.stat(t.path)
        except (FileNotFoundError, NotADirectoryError):
            raise WebCoderError("NOT_FOUND", "File does not exist", path=t.display) from None
        if stat_mod.S_ISDIR(st.st_mode):
            raise WebCoderError("IS_A_DIRECTORY", "Path is a folder; use list_dir", path=t.display)
        if not stat_mod.S_ISREG(st.st_mode):
            raise WebCoderError("NOT_A_REGULAR_FILE", "Path is not a regular file", path=t.display)
        return st

    def _read(self, t: Target, limit: int) -> tuple[bytes, os.stat_result]:
        st = self._stat_file(t)
        if st.st_size > limit:
            raise WebCoderError("FILE_TOO_LARGE", f"File is {st.st_size} bytes; this operation is limited to {limit}",
                                path=t.display, size=st.st_size, limit=limit)
        with open(t.path, "rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise WebCoderError("FILE_TOO_LARGE", f"File grew beyond {limit} bytes while reading", path=t.display)
        return data, st

    def _max_existing(self) -> int:
        return max(self.limits.max_write_bytes, self.limits.max_transfer_bytes, self.limits.max_text_bytes)

    @staticmethod
    def _check_sha(t: Target, current: str, expected) -> None:
        if expected in (None, ""):
            raise WebCoderError("SHA_REQUIRED", "This file already exists: pass expected_sha256 (from read_file or "
                                "stat) to change it", path=t.display, current_sha256=current)
        if not isinstance(expected, str) or not SHA_RE.match(expected):
            raise WebCoderError("INVALID_ARGUMENT", "expected_sha256 must be 64 hexadecimal characters")
        if expected.lower() != current:
            raise WebCoderError("SHA_MISMATCH", "The file changed since you read it: read it again, then retry",
                                path=t.display, current_sha256=current)

    @staticmethod
    def _publish(tmp: str, t: Target, replace: bool) -> None:
        """Move a finished temp file into place. A new file (replace=False) never replaces one that another
        program created meanwhile."""
        if replace:
            os.replace(tmp, t.path)
            return
        try:
            publish_new(tmp, t.path)
        except FileExistsError:
            raise WebCoderError("DESTINATION_EXISTS", "Another program created this file a moment ago; it was left "
                                "alone (read it, then pass expected_sha256 to change it)", path=t.display) from None

    @staticmethod
    def _still(t: Target, seen: os.stat_result | None) -> None:
        """Right before replacing a file, check it is still the version that was read and checked (other
        programs are not stopped by the path locks): otherwise its newer content would be lost."""
        if seen is None:
            return
        now = os.stat(t.path)
        if (now.st_mtime_ns, now.st_size, now.st_ino) != (seen.st_mtime_ns, seen.st_size, seen.st_ino):
            raise WebCoderError("SHA_MISMATCH", "Another program changed the file while it was being written; nothing "
                                "was replaced. Read it again, then retry", path=t.display)

    def _atomic_write(self, t: Target, data: bytes, mode: int | None, replace: bool = True,
                      seen: os.stat_result | None = None) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".webcoder-", suffix=".tmp", dir=os.path.dirname(t.path))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if not IS_WINDOWS:
                os.chmod(tmp, stat_mod.S_IMODE(mode) if mode is not None else self._new_file_mode)
            self._still(t, seen)
            self._publish(tmp, t, replace)
        except PermissionError:
            _unlink_quietly(tmp)
            raise WebCoderError("FILE_LOCKED", "Could not replace the file: it is read-only or locked by another "
                                "program", path=t.display) from None
        except BaseException:
            _unlink_quietly(tmp)
            raise

    @staticmethod
    def _encode_text(text: str, eol: str, old: bytes | None) -> bytes:
        if eol == "keep":
            if old is not None and detect_eol(old.decode("utf-8", errors="replace")) == "crlf" and "\r\n" not in text:
                text = text.replace("\n", "\r\n")
        elif eol == "crlf":
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        elif eol == "lf":
            text = text.replace("\r\n", "\n")
        data = text.encode("utf-8")
        if old is not None and old.startswith(BOM) and not data.startswith(BOM):
            data = BOM + data
        return data

    @staticmethod
    def _diff(path: str, before: str, after: str, limit: int = 120) -> str:
        lines = list(difflib.unified_diff(before.replace("\r\n", "\n").splitlines(),
                                          after.replace("\r\n", "\n").splitlines(),
                                          fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=2))
        if len(lines) > limit:
            lines = lines[:limit] + [f"… diff truncated ({len(lines) - limit} more lines)"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ reads
    def read_file(self, root, path, offset=1, limit=2000, encoding="text", max_chars=None) -> dict:
        t = self.sb.resolve(root, path)
        if encoding == "base64":
            data, _ = self._read(t, self.limits.max_transfer_bytes)
            return {"path": t.display, "encoding": "base64", "size": len(data), "sha256": sha256_bytes(data),
                    "data": base64.b64encode(data).decode("ascii")}
        if encoding != "text":
            raise WebCoderError("INVALID_ARGUMENT", "encoding must be 'text' or 'base64'")
        offset = _int(offset, 1, 1, 10**9)
        limit = _int(limit, 2000, 1, 5000)
        budget = _int(max_chars, self.limits.max_return_chars, 1000, self.limits.max_return_chars)
        data, _ = self._read(t, self.limits.max_text_bytes)
        text, text_encoding = decode_text(data)
        lines = text.replace("\r\n", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        rendered, used, end, shortened = [], 0, offset - 1, 0
        for index in range(offset - 1, min(total, offset - 1 + limit)):
            line = lines[index]
            if len(line) > self.limits.max_line_chars:
                line = line[: self.limits.max_line_chars] + " …[line shortened in this view]"
                shortened += 1
            row = f"{index + 1:>6}\t{line}"
            if rendered and used + len(row) + 1 > budget:
                break
            rendered.append(row)
            used += len(row) + 1
            end = index + 1
        return {"path": t.display, "sha256": sha256_bytes(data), "size": len(data), "encoding": text_encoding,
                "eol": detect_eol(text), "total_lines": total, "start_line": offset if end >= offset else 0,
                "end_line": end if end >= offset else 0, "next_offset": end + 1 if end < total else None,
                "shortened_lines": shortened, "content": "\n".join(rendered)}

    def read_many(self, root, paths, limit_per_file=400) -> dict:
        if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
            raise WebCoderError("INVALID_ARGUMENT", "paths must contain 1 to 20 file paths")
        limit = _int(limit_per_file, 400, 1, 2000)
        budget = self.limits.max_return_chars
        files = []
        for rel in paths:
            if budget < 1000:
                files.append({"path": rel, "error": {"code": "SKIPPED",
                                                     "message": "Output budget used up; read this file separately"}})
                continue
            try:
                item = self.read_file(root, rel, 1, limit, "text", budget)
                budget -= len(item["content"])
                files.append(item)
            except WebCoderError as exc:
                files.append({"path": rel, "error": exc.to_dict()})
        return {"files": files}

    def stat(self, root, path) -> dict:
        t = self.sb.resolve(root, path)
        try:
            st = os.stat(t.path)
        except (FileNotFoundError, NotADirectoryError):
            return {"path": t.display, "exists": False}
        kind = "dir" if stat_mod.S_ISDIR(st.st_mode) else "file" if stat_mod.S_ISREG(st.st_mode) else "other"
        info = {"path": t.display, "exists": True, "type": kind, "size": st.st_size, "mtime": st.st_mtime,
                "symlink": is_link_or_junction(t.lexical)}
        if kind == "file" and st.st_size <= self.limits.max_hash_bytes:
            info["sha256"] = sha256_file(t.path)
        return info

    def list_dir(self, root, path=".", depth=1, offset=0, limit=200, show_hidden=True, include_ignored=False) -> dict:
        t = self.sb.resolve(root, path)
        if not os.path.isdir(t.path):
            raise WebCoderError("NOT_A_DIRECTORY" if os.path.exists(t.path) else "NOT_FOUND",
                                "Path is not an existing folder", path=t.display)
        depth = _int(depth, 1, 1, 5)
        offset = _int(offset, 0, 0, 10**9)
        limit = _int(limit, 200, 1, 500)
        entries: list[dict] = []
        state = {"scanned": 0, "capped": False}

        def walk(directory: str, prefix: str, level: int) -> None:
            try:
                with os.scandir(directory) as iterator:  # never more than the scan cap in memory, or sorted
                    room = max(self.limits.list_scan_cap - state["scanned"], 0)
                    items = sorted(itertools.islice(iterator, room + 1), key=lambda entry: entry.name.lower())
            except OSError as exc:
                entries.append({"path": prefix or ".", "type": "error", "error": exc.strerror or str(exc)})
                return
            for entry in items:
                if state["scanned"] >= self.limits.list_scan_cap:
                    state["capped"] = True
                    return
                state["scanned"] += 1
                if not show_hidden and entry.name.startswith("."):
                    continue
                rel = f"{prefix}/{entry.name}" if prefix else entry.name
                try:
                    is_link = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_link:
                    entries.append({"path": rel, "type": "symlink"})
                elif is_dir:
                    if entry.name in IGNORED_DIRS and not include_ignored:
                        entries.append({"path": rel, "type": "dir", "ignored": True})
                        continue
                    if not self.sb.walkable(entry.path, t.root):  # a junction, or the agent's own folder
                        entries.append({"path": rel, "type": "symlink"} if self.sb.is_link(entry.path)
                                       else {"path": rel, "type": "dir", "blocked": True})
                        continue
                    entries.append({"path": rel, "type": "dir"})
                    if level < depth:
                        walk(entry.path, rel, level + 1)
                else:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    item = {"path": rel, "type": "file", "size": st.st_size, "mtime": int(st.st_mtime)}
                    real_rel = os.path.relpath(entry.path, t.root).replace("\\", "/")  # listed via a link: as itself
                    if self.sb.is_denied(rel, t.root) or self.sb.is_denied(real_rel, t.root):
                        item["blocked"] = True
                    entries.append(item)

        walk(t.path, t.rel, 1)
        page = entries[offset: offset + limit]
        following = offset + len(page)
        return {"path": t.display, "depth": depth, "total": len(entries), "offset": offset, "entries": page,
                "next_offset": following if following < len(entries) else None, "scan_capped": state["capped"]}

    def _iter_files(self, t: Target, budget: _Budget):
        """Files under a target. Search reads them without resolve(), so the walk itself keeps to the same
        boundaries: no folders that really lie outside the project, none in the agent's own folder."""
        if os.path.isfile(t.path):
            yield t.rel, t.path
            return
        for dirpath, dirnames, filenames in _walk(t.path, budget):
            dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS
                           and self.sb.walkable(os.path.join(dirpath, name), t.root)]
            rel_dir = os.path.relpath(dirpath, t.root).replace("\\", "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            for name in filenames:
                yield (f"{rel_dir}/{name}" if rel_dir else name), os.path.join(dirpath, name)

    def find_files(self, root, pattern, path=".", max_results=200, include_ignored=False) -> dict:
        """Files and folders whose name (or, for patterns with '/', relative path) matches a glob."""
        if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 500:
            raise WebCoderError("INVALID_ARGUMENT", "pattern must be a non-empty glob of at most 500 characters")
        t = self.sb.resolve(root, path)
        if not os.path.isdir(t.path):
            raise WebCoderError("NOT_A_DIRECTORY" if os.path.exists(t.path) else "NOT_FOUND",
                                "Path is not an existing folder", path=t.display)
        max_results = _int(max_results, 200, 1, 1000)
        globs = [item.strip().lower().replace("\\", "/") for item in pattern.split(",") if item.strip()]
        names = [glob for glob in globs if "/" not in glob]  # a bare pattern matches the name at any depth
        paths = [path_glob(glob) for glob in globs if "/" in glob]

        def matches(rel: str) -> bool:
            low = rel.lower()
            name = low.rsplit("/", 1)[-1]
            return (any(fnmatch.fnmatchcase(name, glob) for glob in names)
                    or any(regex.match(low) for regex in paths))

        deadline = time.monotonic() + self.limits.search_seconds
        found: list[dict] = []
        scanned, truncated = 0, None
        walked = _Budget(SCAN_ENTRIES)
        for dirpath, dirnames, filenames in _walk(t.path, walked):
            rel_dir = os.path.relpath(dirpath, t.root).replace("\\", "/")
            prefix = "" if rel_dir == "." else rel_dir + "/"
            walk_into, candidates = [], []
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if not self.sb.walkable(full, t.root):
                    continue  # links and the agent's own folder: not listed, not entered
                candidates.append((name, "dir", full))
                if include_ignored or name not in IGNORED_DIRS:
                    walk_into.append(name)
            dirnames[:] = walk_into
            candidates += [(name, "file", os.path.join(dirpath, name)) for name in filenames]
            for name, kind, full in candidates:
                scanned += 1
                rel = prefix + name
                if not matches(rel):
                    continue
                item = {"path": rel, "type": kind}
                if kind == "file":
                    try:
                        st = os.lstat(full)
                    except OSError:
                        continue
                    if stat_mod.S_ISLNK(st.st_mode):
                        continue
                    item.update(size=st.st_size, mtime=int(st.st_mtime))
                    if self.sb.is_denied(rel, t.root):
                        item["blocked"] = True
                found.append(item)
                if len(found) >= max_results:
                    truncated = "max_results"
                    break
            if truncated:
                break
            if time.monotonic() > deadline:
                truncated = "time_budget"
                break
        if truncated is None and walked.cut:
            truncated = "scan_cap"
        return {"pattern": pattern, "path": t.display, "matches": found, "scanned": scanned, "truncated": truncated}

    def search(self, root, query, path=".", regex=False, case_sensitive=False, glob=None, max_results=100,
               context=0) -> dict:
        if not isinstance(query, str) or not query or len(query) > 1000:
            raise WebCoderError("INVALID_ARGUMENT", "query must be a non-empty string of at most 1000 characters")
        t = self.sb.resolve(root, path)
        if not os.path.exists(t.path):
            raise WebCoderError("NOT_FOUND", "Search path does not exist", path=t.display)
        max_results = _int(max_results, 100, 1, 500)
        context = _int(context, 0, 0, 5)
        if regex:  # a literal pattern cannot backtrack, so only regex searches need the timeout-capable engine
            if regex_engine is None:
                raise WebCoderError("REGEX_UNAVAILABLE", "Regular-expression search needs the 'regex' package on "
                                    "this device; re-run the install command to upgrade the agent")
            try:
                pattern = regex_engine.compile(query, 0 if case_sensitive else regex_engine.IGNORECASE)
            except regex_engine.error as exc:
                raise WebCoderError("INVALID_REGEX", f"Invalid regular expression: {exc}") from None
        else:
            pattern = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
        globs = [item.strip().lower().replace("\\", "/") for item in str(glob or "").split(",") if item.strip()]
        deadline = time.monotonic() + self.limits.search_seconds
        matches: list[dict] = []
        scanned = files_matched = 0
        truncated = None
        walked = _Budget(SCAN_ENTRIES)
        for rel, real in self._iter_files(t, walked):
            if time.monotonic() > deadline:
                truncated = "time_budget"
                break
            low = rel.lower()
            name = low.rsplit("/", 1)[-1]
            if globs and not any(fnmatch.fnmatchcase(low, g) or fnmatch.fnmatchcase(low, "*/" + g) if "/" in g
                                 else fnmatch.fnmatchcase(name, g) for g in globs):
                continue
            if self.sb.is_denied(rel, t.root):
                continue
            try:
                if os.path.islink(real) or os.path.getsize(real) > self.limits.search_file_bytes:
                    continue
                with open(real, "rb") as handle:
                    data = handle.read(self.limits.search_file_bytes + 1)
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            scanned += 1
            lines = data.decode("utf-8", errors="replace").splitlines()
            hit = False
            for index, line in enumerate(lines):
                if len(line) > 20_000:
                    continue
                if regex:
                    budget = min(self.limits.regex_line_seconds, deadline - time.monotonic())
                    if budget <= 0:
                        truncated = "time_budget"
                        break
                    try:  # concurrent=True releases the GIL, so the agent keeps serving while this runs
                        found = pattern.search(line, timeout=budget, concurrent=True)
                    except TimeoutError:
                        truncated = "regex_timeout"
                        break
                else:
                    found = pattern.search(line)
                if not found:
                    continue
                hit = True
                item = {"path": rel, "line": index + 1, "text": _shorten(line.strip(), 400)}
                if context:
                    item["before"] = [[n + 1, _shorten(lines[n], 400)] for n in range(max(0, index - context), index)]
                    item["after"] = [[n + 1, _shorten(lines[n], 400)]
                                     for n in range(index + 1, min(len(lines), index + 1 + context))]
                matches.append(item)
                if len(matches) >= max_results:
                    truncated = "max_results"
                    break
            files_matched += hit
            if truncated:
                break
        if truncated is None and walked.cut:
            truncated = "scan_cap"
        return {"query": query, "path": t.display, "matches": matches, "files_scanned": scanned,
                "files_matched": files_matched, "truncated": truncated}

    # ------------------------------------------------------------------ writes
    def write_file(self, root, path, content, encoding="text", expected_sha256=None, create_parents=True,
                   eol="keep") -> dict:
        t = self.sb.resolve(root, path, write=True)
        if not t.rel:
            raise WebCoderError("IS_A_DIRECTORY", "Path is the project root", path=".")
        if not isinstance(content, str):
            raise WebCoderError("INVALID_ARGUMENT", "content must be a string")
        if encoding not in ("text", "base64"):
            raise WebCoderError("INVALID_ARGUMENT", "encoding must be 'text' or 'base64'")
        if eol not in ("keep", "lf", "crlf"):
            raise WebCoderError("INVALID_ARGUMENT", "eol must be 'keep', 'lf' or 'crlf'")
        payload = b""
        if encoding == "base64":
            try:
                payload = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                raise WebCoderError("INVALID_ARGUMENT", "content is not valid base64") from None
        with self._locks.hold(t.path):
            old, mode, st = None, None, None
            if os.path.lexists(t.path):
                old, st = self._read(t, self._max_existing())
                self._check_sha(t, sha256_bytes(old), expected_sha256)
                mode = st.st_mode
            elif expected_sha256:
                raise WebCoderError("NOT_FOUND", "expected_sha256 was given but the file does not exist; omit it to "
                                    "create a new file", path=t.display)
            if encoding == "text":
                payload = self._encode_text(content, eol, old)
            if len(payload) > self.limits.max_write_bytes:
                raise WebCoderError("FILE_TOO_LARGE", f"Content is {len(payload)} bytes; the limit is "
                                    f"{self.limits.max_write_bytes}", path=t.display)
            if old is not None and old == payload:
                return {"path": t.display, "created": False, "unchanged": True, "size": len(payload),
                        "sha256": sha256_bytes(payload), "backup_id": None}
            parent = os.path.dirname(t.path)
            if not os.path.isdir(parent):
                if not create_parents:
                    raise WebCoderError("PARENT_NOT_FOUND", "Parent folder does not exist; set create_parents=true",
                                        path=t.display)
                os.makedirs(parent, exist_ok=True)
            backup_id = self.backups.save(t, old, "overwrite") if old is not None else None
            self._atomic_write(t, payload, mode, replace=old is not None, seen=st)
        return {"path": t.display, "created": old is None, "size": len(payload), "sha256": sha256_bytes(payload),
                "backup_id": backup_id}

    def edit_file(self, root, path, expected_sha256, edits) -> dict:
        t = self.sb.resolve(root, path, write=True)
        if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
            raise WebCoderError("INVALID_ARGUMENT", "edits must contain 1 to 50 replacements")
        with self._locks.hold(t.path):
            old, st = self._read(t, self.limits.max_text_bytes)
            current = sha256_bytes(old)
            self._check_sha(t, current, expected_sha256)
            text, text_encoding = decode_text(old)
            eol = detect_eol(text)
            work = text.replace("\r\n", "\n") if eol == "crlf" else text
            replacements = 0
            for index, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    raise WebCoderError("INVALID_ARGUMENT", f"edits[{index}] must be an object")
                old_s, new_s = edit.get("old_string"), edit.get("new_string")
                replace_all = bool(edit.get("replace_all", False))
                if not isinstance(old_s, str) or not old_s or not isinstance(new_s, str):
                    raise WebCoderError("INVALID_ARGUMENT", f"edits[{index}] needs a non-empty old_string and a "
                                        "new_string (use write_file to create or fully replace files)")
                if eol == "crlf":
                    old_s, new_s = old_s.replace("\r\n", "\n"), new_s.replace("\r\n", "\n")
                count = work.count(old_s)
                if count == 0 and eol == "mixed" and "\n" in old_s:
                    alt_old = old_s.replace("\r\n", "\n").replace("\n", "\r\n")
                    if work.count(alt_old):
                        old_s = alt_old
                        new_s = new_s.replace("\r\n", "\n").replace("\n", "\r\n")
                        count = work.count(alt_old)
                if count == 0:
                    raise WebCoderError("EDIT_NOT_FOUND", f"edits[{index}].old_string was not found. Copy the exact "
                                        "text from read_file (whitespace matters, no line-number prefixes)",
                                        index=index)
                if count > 1 and not replace_all:
                    raise WebCoderError("EDIT_AMBIGUOUS", f"edits[{index}].old_string matches {count} places; include "
                                        "more surrounding lines or set replace_all=true", index=index, matches=count)
                work = work.replace(old_s, new_s) if replace_all else work.replace(old_s, new_s, 1)
                replacements += count if replace_all else 1
            result = work.replace("\n", "\r\n") if eol == "crlf" else work
            if result == text:
                return {"path": t.display, "changed": False, "sha256": current, "replacements": replacements}
            data = result.encode("utf-8")
            if text_encoding == "utf-8-sig":
                data = BOM + data
            if len(data) > self.limits.max_write_bytes:
                raise WebCoderError("FILE_TOO_LARGE", "Edited file would exceed the write limit", path=t.display)
            backup_id = self.backups.save(t, old, "edit")
            self._atomic_write(t, data, st.st_mode, seen=st)
        return {"path": t.display, "changed": True, "sha256": sha256_bytes(data), "size": len(data),
                "replacements": replacements, "backup_id": backup_id, "diff": self._diff(t.display, text, result)}

    def delete_path(self, root, path, expected_sha256=None) -> dict:
        t = self.sb.resolve(root, path, write=True)
        if not t.rel:
            raise WebCoderError("INVALID_PATH", "Refusing to delete the project root")
        if is_link_or_junction(t.lexical):  # resolve() gave its target: that must not go instead
            raise WebCoderError("IS_SYMLINK", "Symlinks are not changed through WebCoder", path=t.display)
        with self._locks.hold(t.path):
            if not os.path.lexists(t.path):
                raise WebCoderError("NOT_FOUND", "Path does not exist", path=t.display)
            if os.path.isdir(t.path):
                try:
                    os.rmdir(t.path)
                except OSError:
                    raise WebCoderError("DIRECTORY_NOT_EMPTY", "Only empty folders can be deleted; delete the files "
                                        "inside first", path=t.display) from None
                return {"path": t.display, "deleted": "directory"}
            seen = self._stat_file(t)
            if seen.st_size > self.backups.max_total_bytes:
                raise WebCoderError("FILE_TOO_LARGE", f"File is {seen.st_size:,} bytes, too large to keep a backup of, "
                                    "so it is not deleted through WebCoder; delete it on the computer itself",
                                    path=t.display)
            # Hash and back up as streams: large files need not fit in memory.
            current = sha256_file(t.path)
            self._check_sha(t, current, expected_sha256)
            backup_id, copied = self.backups.save_file(t, "delete")
            if copied != current or not _same_version(t.path, seen):  # e.g. an editor saved a new file meanwhile
                self.backups.discard(backup_id)
                raise WebCoderError("SHA_MISMATCH", "The file changed while it was being deleted; read it again",
                                    path=t.display)
            os.remove(t.path)
        return {"path": t.display, "deleted": "file", "backup_id": backup_id}

    def move_path(self, root, path, new_path, expected_sha256=None) -> dict:
        src = self.sb.resolve(root, path, write=True)
        dst = self.sb.resolve(root, new_path, write=True)
        if not src.rel or not dst.rel:
            raise WebCoderError("INVALID_PATH", "The project root cannot be moved or replaced")
        if is_link_or_junction(src.lexical):
            raise WebCoderError("IS_SYMLINK", "Symlinks are not changed through WebCoder", path=src.display)
        with self._locks.hold(src.path, dst.path):
            if not os.path.lexists(src.path):
                raise WebCoderError("NOT_FOUND", "Source does not exist", path=src.display)
            # Only a case-only rename of the entry itself; a symlink pointing at the source is a different entry.
            same_entry = _renames_itself(src.path, dst.path, dst.lexical)
            if same_entry and src.rel == dst.rel:
                raise WebCoderError("INVALID_PATH", "Source and destination are the same")
            if os.path.lexists(dst.lexical) and not same_entry:
                raise WebCoderError("DESTINATION_EXISTS", "Destination already exists; choose another path",
                                    path=dst.display)
            is_dir = os.path.isdir(src.path)
            seen = None
            if is_dir:
                if is_within(dst.path, src.path) and not same_entry:
                    raise WebCoderError("INVALID_PATH", "A folder cannot be moved into itself")
                self._check_folder_move(src, dst)
            else:  # hashed as a stream: moving does not need the content in memory
                seen = self._stat_file(src)
                if seen.st_size > self.limits.max_hash_bytes:
                    raise WebCoderError("FILE_TOO_LARGE", f"File is {seen.st_size:,} bytes, too large to verify its "
                                        "sha256", path=src.display)
                self._check_sha(src, sha256_file(src.path), expected_sha256)
            destination = dst.lexical if same_entry else dst.path  # case-only rename keeps the new spelling
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            if seen is not None and not _same_version(src.path, seen):  # e.g. an editor saved it after the check
                raise WebCoderError("SHA_MISMATCH", "The file changed while it was being moved; read it again",
                                    path=src.display)
            try:
                if is_dir or same_entry:  # a folder can at most replace an empty folder
                    os.rename(src.path, destination)
                else:  # never a file that another program created after the check
                    publish_new(src.path, destination)
            except FileExistsError:
                raise WebCoderError("DESTINATION_EXISTS", "Destination already exists; choose another path",
                                    path=dst.display) from None
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                try:  # another filesystem (e.g. a drive mounted inside the project)
                    move_across_filesystems(src.path, destination, is_dir, seen)
                except FileExistsError:
                    raise WebCoderError("DESTINATION_EXISTS", "Destination already exists; choose another path",
                                        path=dst.display) from None
        return {"from": src.display, "to": dst.display, "type": "directory" if is_dir else "file"}

    def _check_folder_move(self, src: Target, dst: Target) -> None:
        """Renaming a folder must not lift protection from what is inside: the deny list matches paths
        (".aws/credentials", ".ssh/*"), and the agent's own folder is protected by location."""
        if any(is_within(protected, src.path) for protected in self.sb.protected_dirs):
            raise WebCoderError("PATH_DENIED", "This folder contains the agent's own data folder", path=src.display)
        budget = _Budget(self.limits.list_scan_cap)
        for dirpath, _dirnames, filenames in _walk(src.path, budget):
            rel_dir = os.path.relpath(dirpath, src.path).replace("\\", "/")
            prefix = "" if rel_dir == "." else rel_dir + "/"
            for name in filenames:
                old_rel, new_rel = f"{src.rel}/{prefix}{name}", f"{dst.rel}/{prefix}{name}"
                # both ways: out of protection exposes it, into it writes where writing is refused (.ssh/config)
                if self.sb.is_denied(old_rel, src.root) != self.sb.is_denied(new_rel, dst.root):
                    raise WebCoderError("PATH_DENIED", "Moving this folder would move a file into or out of what the "
                                        "deny list protects (secrets, keys, ssh settings)", path=old_rel)
            if budget.cut:
                break
        if budget.cut:
            raise WebCoderError("FOLDER_TOO_LARGE", "Folder is too large to check before moving; move smaller parts",
                                path=src.display)

    def make_dir(self, root, path) -> dict:
        t = self.sb.resolve(root, path, write=True)
        if os.path.isdir(t.path):
            return {"path": t.display, "created": False}
        if os.path.lexists(t.path):
            raise WebCoderError("NOT_A_DIRECTORY", "A file with this name already exists", path=t.display)
        os.makedirs(t.path, exist_ok=True)
        return {"path": t.display, "created": True}

    # ------------------------------------------------------------------ backups
    def list_backups(self, root, path=None, limit=50) -> dict:
        base = self.sb.root(root)
        rel = self.sb.resolve(root, path).rel if path not in (None, "", ".") else None
        items = self.backups.list(base, rel, _int(limit, 50, 1, 200))
        keys = ("id", "path", "size", "sha256", "time", "reason")
        return {"backups": [{key: meta.get(key) for key in keys} for meta in items]}

    def restore_backup(self, root, backup_id, expected_sha256=None) -> dict:
        base = self.sb.root(root)
        meta, source = self.backups.load(base, backup_id)
        t = self.sb.resolve(root, meta["path"], write=True)
        with self._locks.hold(t.path):  # streamed throughout: backups of large files need not fit in memory
            saved, mode, st = None, None, None
            if os.path.lexists(t.path):
                st = self._stat_file(t)
                current = sha256_file(t.path)
                self._check_sha(t, current, expected_sha256)
                if current == meta["sha256"]:
                    return {"path": t.display, "restored_from": backup_id, "sha256": meta["sha256"],
                            "unchanged": True, "backup_id": None}
                saved, copied = self.backups.save_file(t, "restore", keep=backup_id)  # its source stays
                if copied != current:
                    self.backups.discard(saved)
                    raise WebCoderError("SHA_MISMATCH", "The file changed while it was being replaced; read it again",
                                        path=t.display)
                mode = st.st_mode
            else:
                os.makedirs(os.path.dirname(t.path), exist_ok=True)
                # The deleted file's own mode; backups made before modes were recorded stay private.
                mode = meta["mode"] if isinstance(meta.get("mode"), int) else 0o600
            self._atomic_copy(t, source, mode, replace=saved is not None, seen=st)
        return {"path": t.display, "restored_from": backup_id, "sha256": meta["sha256"], "size": meta["size"],
                "backup_id": saved}

    def _atomic_copy(self, t: Target, source, mode: int | None, replace: bool = True,
                     seen: os.stat_result | None = None) -> None:
        """Like _atomic_write, from a file instead of bytes in memory."""
        fd, tmp = tempfile.mkstemp(prefix=".webcoder-", suffix=".tmp", dir=os.path.dirname(t.path))
        try:
            with os.fdopen(fd, "wb") as handle, open(source, "rb") as src:
                shutil.copyfileobj(src, handle, MiB)
                handle.flush()
                os.fsync(handle.fileno())
            if not IS_WINDOWS:
                os.chmod(tmp, stat_mod.S_IMODE(mode) if mode is not None else self._new_file_mode)
            self._still(t, seen)
            self._publish(tmp, t, replace)
        except PermissionError:
            _unlink_quietly(tmp)
            raise WebCoderError("FILE_LOCKED", "Could not replace the file: it is read-only or locked by another "
                                "program", path=t.display) from None
        except BaseException:
            _unlink_quietly(tmp)
            raise

    # ------------------------------------------------------------------ files for phones (mobile.py)
    def create_file(self, root, path, data: bytes, *, max_bytes: int | None = None) -> dict:
        """Save new content made on this computer (e.g. a screenshot) into a project; never replaces an existing
        file. `max_bytes` replaces the write limit, as for files pulled from a phone."""
        t = self.sb.resolve(root, path, write=True)
        if len(data) > (self.limits.max_write_bytes if max_bytes is None else max_bytes):
            raise WebCoderError("FILE_TOO_LARGE", f"{len(data):,} bytes is over the write limit", path=t.display)
        with self._locks.hold(t.path):
            if os.path.lexists(t.lexical):
                raise WebCoderError("DESTINATION_EXISTS", "File already exists; choose another path", path=t.display)
            os.makedirs(os.path.dirname(t.path), exist_ok=True)
            self._atomic_write(t, data, None, replace=False)
        return {"path": t.display, "size": len(data), "sha256": sha256_bytes(data)}

    def local_file(self, root, path, max_bytes: int) -> tuple[str, int, str]:
        """(real path, size, display path) of a project file handed to adb / hdc (push, install)."""
        t = self.sb.resolve(root, path)
        st = self._stat_file(t)
        if st.st_size > max_bytes:
            raise WebCoderError("FILE_TOO_LARGE", f"{st.st_size:,} bytes is too large", path=t.display)
        return t.path, st.st_size, t.display

    @contextlib.contextmanager
    def incoming_file(self, root, path, max_bytes: int):
        """A temp file next to the destination for a tool to fill (e.g. adb pull), moved into place when
        the block succeeds. Never replaces an existing file; the result is set on the yielded object."""
        t = self.sb.resolve(root, path, write=True)
        if os.path.lexists(t.lexical):
            raise WebCoderError("DESTINATION_EXISTS", "File already exists; choose another path", path=t.display)
        os.makedirs(os.path.dirname(t.path), exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".webcoder-", suffix=".tmp", dir=os.path.dirname(t.path))
        os.close(fd)
        incoming = SimpleNamespace(temp=temp, result=None)
        try:
            yield incoming
            size = os.path.getsize(temp)
            if size > max_bytes:
                raise WebCoderError("FILE_TOO_LARGE", f"{size:,} bytes is too large", path=t.display)
            with self._locks.hold(t.path):
                if os.path.lexists(t.lexical):
                    raise WebCoderError("DESTINATION_EXISTS", "File already exists; choose another path",
                                        path=t.display)
                if not IS_WINDOWS:
                    os.chmod(temp, self._new_file_mode)
                self._publish(temp, t, replace=False)
            incoming.result = {"path": t.display, "size": size, "sha256": sha256_file(t.path)}
        finally:
            _unlink_quietly(temp)

    # ------------------------------------------------------------------ agent info (panel helpers)
    def info(self) -> dict:
        return {"agent_version": VERSION, "platform": platform.system(), "platform_release": platform.release(),
                "hostname": socket.gethostname(), "python": platform.python_version(),
                "allowed_roots": list(self.sb.allowed_roots), "read_only": self.sb.read_only,
                "mobile": self.mobile.status() if self.mobile is not None else {"enabled": False},
                "ssh": self.ssh.status() if self.ssh is not None else {"hosts": []},
                "git": self.git.status_info() if self.git is not None else {"client": False},
                "skills": self.skills.status() if self.skills is not None else {"enabled": False, "dirs": []},
                "commands": self.cli.status() if self.cli is not None else {"programs": []}}

    def check_root(self, root) -> dict:
        return {"root": self.sb.root(root)}

    def browse(self, path=None) -> dict:
        roots = self.sb.allowed_roots
        if not path:
            return {"path": None, "parent": None, "truncated": False,
                    "dirs": [{"name": root, "path": root, "exists": os.path.isdir(root)} for root in roots]}
        if not isinstance(path, str) or not os.path.isabs(os.path.expanduser(path)):
            raise WebCoderError("INVALID_PATH", "Browse needs an absolute folder path")
        real = os.path.realpath(os.path.expanduser(path))
        if not any(is_within(real, root) for root in roots):
            raise WebCoderError("ROOT_NOT_ALLOWED", "Folder is outside this device's allowed_roots")
        if not os.path.isdir(real):
            raise WebCoderError("NOT_FOUND", "Folder does not exist")
        dirs = []
        with os.scandir(real) as iterator:
            for entry in iterator:
                with contextlib.suppress(OSError):
                    if entry.is_dir(follow_symlinks=False) and entry.name not in IGNORED_DIRS:
                        dirs.append({"name": entry.name, "path": os.path.join(real, entry.name)})
        dirs.sort(key=lambda item: item["name"].lower())
        parent = os.path.dirname(real)
        in_scope = parent != real and any(is_within(parent, root) for root in roots)
        return {"path": real, "parent": parent if in_scope else None, "dirs": dirs[:500], "truncated": len(dirs) > 500}
