"""SQLite persistence plus a master key for secrets at rest."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from webcoder.common.crypto import b64d, b64e

SCHEMA_VERSION = 2

PROJECTS_TABLE = """CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, alias TEXT NOT NULL, alias_key TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE, root TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('ro', 'rw')), description TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE, UNIQUE (user_id, alias_key))"""

OAUTH_CODES_TABLE = """CREATE TABLE IF NOT EXISTS oauth_codes (
    hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, code_challenge TEXT NOT NULL,
    scopes TEXT NOT NULL, projects TEXT NOT NULL, resource TEXT NOT NULL, expires REAL NOT NULL,
    user_id TEXT NOT NULL)"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created REAL NOT NULL,
    role TEXT NOT NULL DEFAULT 'user', disabled INTEGER NOT NULL DEFAULT 0, last_login REAL);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, secret_enc TEXT, enabled INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL, paired REAL, last_seen REAL, info TEXT NOT NULL DEFAULT '{{}}',
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    expires REAL NOT NULL);
{PROJECTS_TABLE};
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('pat', 'oauth')), label TEXT NOT NULL,
    client_id TEXT, scopes TEXT NOT NULL, projects TEXT NOT NULL, resource TEXT,
    created REAL NOT NULL, expires REAL, revoked INTEGER NOT NULL DEFAULT 0, last_used REAL,
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('pat', 'access', 'refresh')), expires REAL NOT NULL,
    created REAL NOT NULL, used REAL);
CREATE TABLE IF NOT EXISTS oauth_clients (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL, created REAL NOT NULL, last_used REAL);
CREATE TABLE IF NOT EXISTS oauth_requests (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, state TEXT,
    code_challenge TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT NOT NULL, expires REAL NOT NULL);
{OAUTH_CODES_TABLE};
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ok', detail TEXT NOT NULL DEFAULT '{{}}',
    duration_ms INTEGER, user_id TEXT);
CREATE TABLE IF NOT EXISTS deleted_devices (
    id TEXT PRIMARY KEY, user_id TEXT, secret_enc TEXT NOT NULL, deleted REAL NOT NULL);
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('invite', 'reset')),
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE, note TEXT NOT NULL DEFAULT '', created_by TEXT,
    created REAL NOT NULL, expires REAL NOT NULL, used REAL);
CREATE TABLE IF NOT EXISTS browser_links (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL, created REAL NOT NULL, last_seen REAL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS browser_codes (
    code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id TEXT, expires REAL NOT NULL);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS tokens_grant ON tokens(grant_id);
CREATE INDEX IF NOT EXISTS devices_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS grants_user ON grants(user_id);
CREATE INDEX IF NOT EXISTS audit_user ON audit(user_id, id);
CREATE INDEX IF NOT EXISTS browser_links_user ON browser_links(user_id);
"""


def _owner_only(path: Path, mode: int) -> None:
    """Take away what group and others may do (POSIX; a Windows profile's ACLs already keep other accounts out)."""
    if os.name != "nt" and path.stat().st_mode & 0o077:
        os.chmod(path, mode)


class TransactionAborted(RuntimeError):
    """A step inside a joined (nested) transaction failed and the caller went on: nothing of it was saved."""


class Store:
    """Thread-safe wrapper around one SQLite connection (autocommit + explicit transactions)."""

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)  # accounts, paths, audit log: owner only
        _owner_only(self.dir, 0o700)  # also when it was already there
        self._private_database(self.dir / "hub.db")
        self.db = sqlite3.connect(self.dir / "hub.db", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._depth = 0  # open transaction() levels (only the thread holding the lock changes it)
        self._aborted = False
        # Database phases of long-running work (collaboration) run here, one at a time, off the event loop.
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="webcoder-store")
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.executescript(SCHEMA)
            self._migrate()
            self._add_last_login()
            self.db.executescript(INDEXES)
            with contextlib.suppress(sqlite3.IntegrityError):  # names differing only in case from before: kept
                self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_name_nocase ON users(lower(username))")
            self._migrate_collaboration()
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            self.db.execute("PRAGMA foreign_keys=ON")
        self._cipher = AESGCM(self._master_key())

    @staticmethod
    def _private_database(path: Path) -> None:
        """hub.db (and the -wal / -shm files, which SQLite creates with its mode) readable by the owner only,
        whatever the umask; a database from before this is tightened."""
        if os.name == "nt":  # a user profile's ACLs already keep other accounts out
            return
        if not path.exists():
            with contextlib.suppress(FileExistsError):
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        for name in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            with contextlib.suppress(FileNotFoundError):
                if name.stat().st_mode & 0o077:
                    os.chmod(name, 0o600)

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Upgrade a single-user (v1) database: everything becomes the first admin's."""
        if "role" in self._columns("users"):
            return
        first = self.db.execute("SELECT id FROM users ORDER BY created LIMIT 1").fetchone()
        owner = first[0] if first else None
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            self.db.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
            self.db.execute("UPDATE users SET role='admin'")  # v1 only had administrators
            for table in ("devices", "grants", "audit"):
                if "user_id" not in self._columns(table):
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
                self.db.execute(f"UPDATE {table} SET user_id=?", (owner,))
            # Project aliases become unique per user: rebuild the table with the new constraint.
            self.db.execute("ALTER TABLE projects RENAME TO projects_v1")
            self.db.execute(PROJECTS_TABLE)
            self.db.execute("INSERT INTO projects(id, alias, alias_key, device_id, root, mode, description, created, "
                            "user_id) SELECT id, alias, alias_key, device_id, root, mode, description, created, ? "
                            "FROM projects_v1", (owner,))
            self.db.execute("DROP TABLE projects_v1")
            self.db.execute("DROP TABLE IF EXISTS oauth_codes")  # short-lived; recreated with an owner column
            self.db.execute(OAUTH_CODES_TABLE)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _add_last_login(self) -> None:
        """Sessions are deleted at logout or expiry, so the last login is kept with the user; a database from
        before this starts from the sessions and the login entries of the audit log it still has."""
        if "last_login" in self._columns("users"):
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("ALTER TABLE users ADD COLUMN last_login REAL")
            self.db.execute("UPDATE users SET last_login = nullif(max("
                            "coalesce((SELECT max(created) FROM sessions s WHERE s.user_id = users.id), 0), "
                            "coalesce((SELECT max(ts) FROM audit a WHERE a.user_id = users.id "
                            "AND a.action = 'panel.login'), 0)), 0)")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_collaboration(self) -> None:
        """The collaboration center's tables (additive; created even while the feature is off, so turning it
        on later needs no separate upgrade step)."""
        from webcoder.hub.collaboration.schema import migrate

        self.db.execute("BEGIN IMMEDIATE")
        try:
            migrate(self.db)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _master_key(self) -> bytes:
        path = self.dir / "master.key"
        if path.exists():
            _owner_only(path, 0o600)  # e.g. restored from a backup with looser permissions
            key = b64d(path.read_text(encoding="ascii").strip())
            if len(key) != 32:
                raise RuntimeError(f"{path} is corrupt: expected a 32-byte key")
            return key
        key = os.urandom(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(b64e(key))
        return key

    def one(self, sql: str, params=()) -> dict | None:
        with self.lock:
            row = self.db.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def execute(self, sql: str, params=()) -> int:
        with self.lock:
            return self.db.execute(sql, params).rowcount

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = True):
        """BEGIN IMMEDIATE (or a deferred BEGIN for reads) ... COMMIT. Used inside an open transaction it joins
        it: only the outermost one commits, and a failure in any of them rolls the whole of it back (when the
        caller carried on regardless, the outermost raises TransactionAborted instead of committing)."""
        with self.lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self.db
                except BaseException:
                    self._aborted = True
                    raise
                finally:
                    self._depth -= 1
                return
            self.db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            self._depth, self._aborted = 1, False
            try:
                yield self.db
            except BaseException:
                self._depth = 0
                self.db.execute("ROLLBACK")
                raise
            self._depth = 0
            if self._aborted:
                self.db.execute("ROLLBACK")
                raise TransactionAborted("A step of this change failed, so none of it was saved")
            self.db.execute("COMMIT")

    async def run(self, function, /, *args, **kwargs):
        """Run one synchronous database phase (reads, a transaction) on the store's worker thread, so the event
        loop stays free. Phases run one after another; a phase already started finishes even if the awaiting
        task is cancelled, and no transaction is ever held open across an await."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._worker, functools.partial(function, *args, **kwargs))

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        return b64e(nonce + self._cipher.encrypt(nonce, plaintext.encode("utf-8"), b"webcoder-hub"))

    def decrypt(self, value: str) -> str:
        raw = b64d(value)
        return self._cipher.decrypt(raw[:12], raw[12:], b"webcoder-hub").decode("utf-8")

    def close(self) -> None:
        self._worker.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            self.db.close()
