"""Accounts, panel sessions, invite / reset links, bearer tokens (PAT + OAuth) and principals."""
from __future__ import annotations

import hmac
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass

from starlette.requests import Request

from webcoder.common.crypto import hash_password, new_token, sha256_hex, verify_password
from webcoder.hub.config import Settings
from webcoder.hub.store import Store

SESSION_COOKIE = "webcoder_session"
SCOPES = ("read", "write", "mobile", "ssh", "cli")  # mobile: phones; ssh: commands on servers; cli: local programs
ROLES = ("admin", "user")
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")


class ApiError(Exception):
    """Error returned to the panel / HTTP clients as {"error": {...}}."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class Principal:
    kind: str                           # "pat" or "oauth"
    grant_id: str
    label: str
    scopes: frozenset
    projects: tuple | None              # None = every project of the user
    user_id: str
    username: str
    client_id: str | None = None

    def can_access(self, project_id: str) -> bool:
        return self.projects is None or project_id in self.projects

    @property
    def actor(self) -> str:
        return f"{self.username}·{'令牌' if self.kind == 'pat' else 'OAuth'}:{self.label}"


def normalize_scopes(scopes) -> list[str]:
    chosen = {scope for scope in scopes if scope in SCOPES}
    if chosen & {"write", "mobile", "ssh", "cli"}:
        chosen.add("read")
    return [scope for scope in SCOPES if scope in chosen]


class Auth:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    # ------------------------------------------------------------- accounts
    def has_admin(self) -> bool:
        return self.store.one("SELECT id FROM users WHERE role='admin' LIMIT 1") is not None

    @staticmethod
    def _check_password(password: str) -> None:
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise ApiError(400, "WEAK_PASSWORD", "密码长度需为 10–256 个字符")

    def create_user(self, username: str, password: str, role: str = "user") -> str:
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise ApiError(400, "INVALID_USERNAME",
                           "用户名需为 2–32 位字母、数字、点、下划线或连字符，且以字母或数字开头")
        if role not in ROLES:
            raise ApiError(400, "INVALID_ROLE", "未知角色")
        self._check_password(password)
        if self.store.one("SELECT id FROM users WHERE lower(username)=lower(?)", (username,)):
            raise ApiError(409, "USERNAME_TAKEN", f"用户名 {username} 已被使用")
        user_id = "u_" + uuid.uuid4().hex[:16]
        try:  # the database holds names unique whatever their case, as login looks them up
            self.store.execute("INSERT INTO users(id, username, password_hash, created, role) VALUES (?, ?, ?, ?, ?)",
                               (user_id, username, hash_password(password), time.time(), role))
        except sqlite3.IntegrityError:
            raise ApiError(409, "USERNAME_TAKEN", f"用户名 {username} 已被使用") from None
        return user_id

    def create_admin(self, username: str, password: str) -> str:
        if self.has_admin():
            raise ApiError(409, "ALREADY_INITIALIZED", "管理员已存在；如需重置请使用 reset-password")
        return self.create_user(username, password, role="admin")

    def _set_password(self, user_id: str, password: str) -> None:
        self._check_password(password)
        with self.store.transaction() as db:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            # a reset link from before would set it once more: the password it was for is gone
            db.execute("DELETE FROM links WHERE kind='reset' AND user_id=? AND used IS NULL", (user_id,))

    def set_password(self, username: str, password: str) -> None:
        user = self.store.one("SELECT id FROM users WHERE lower(username)=lower(?)", (username,))  # as login does
        if not user:
            raise ApiError(404, "NO_SUCH_USER", f"用户 {username} 不存在")
        self._set_password(user["id"], password)

    def change_password(self, user_id: str, current: str, new: str) -> None:
        user = self.store.one("SELECT password_hash FROM users WHERE id=?", (user_id,))
        if not user or not verify_password(current, user["password_hash"]):
            raise ApiError(403, "PASSWORD_INCORRECT", "当前密码不正确")
        self._set_password(user_id, new)

    # ------------------------------------------------------------- invite / reset links
    def create_link(self, kind: str, created_by: str, *, user_id: str | None = None, note: str = "",
                    days: float = 7) -> dict:
        token = new_token()
        link_id = "l_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self.store.transaction() as db:
            if kind == "reset":  # at most one live reset link per user
                db.execute("DELETE FROM links WHERE kind='reset' AND user_id=? AND used IS NULL", (user_id,))
            db.execute("INSERT INTO links(id, token_hash, kind, user_id, note, created_by, created, expires) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (link_id, sha256_hex(token), kind, user_id, note, created_by, now, now + days * 86400))
        path = "invite" if kind == "invite" else "reset"
        return {"id": link_id, "token": token, "url": f"{self.settings.public_url}/#/{path}/{token}",
                "expires": now + days * 86400}

    def peek_link(self, token: str, kind: str) -> dict:
        row = self.store.one("SELECT l.*, u.username FROM links l LEFT JOIN users u ON u.id = l.user_id "
                             "WHERE l.token_hash=? AND l.kind=?", (sha256_hex(token or ""), kind))
        if not row or row["used"] or row["expires"] <= time.time():
            raise ApiError(404, "LINK_INVALID", "链接无效、已使用或已过期，请联系管理员重新生成")
        return row

    def accept_invite(self, token: str, username: str, password: str) -> str:
        link = self.peek_link(token, "invite")
        user_id = self.create_user(username, password)
        if self.store.execute("UPDATE links SET used=?, user_id=? WHERE id=? AND used IS NULL",
                              (time.time(), user_id, link["id"])) != 1:
            self.store.execute("DELETE FROM users WHERE id=?", (user_id,))  # lost a race for the same link
            raise ApiError(404, "LINK_INVALID", "邀请链接已被使用")
        return user_id

    def use_reset_link(self, token: str, password: str) -> str:
        link = self.peek_link(token, "reset")
        self._check_password(password)
        user = self.store.one("SELECT disabled FROM users WHERE id=?", (link["user_id"],))
        if not user or user["disabled"]:  # it could not sign in: keep the link for when it is enabled again
            raise ApiError(403, "USER_DISABLED", "该账号已被管理员停用，请先让管理员启用，再用这个链接设置密码")
        if self.store.execute("UPDATE links SET used=? WHERE id=? AND used IS NULL", (time.time(), link["id"])) != 1:
            raise ApiError(404, "LINK_INVALID", "重置链接已被使用")
        self._set_password(link["user_id"], password)
        return link["user_id"]

    # ------------------------------------------------------------- panel sessions
    def login(self, ip: str, username: str, password: str) -> dict:
        now = time.time()
        self.store.execute("DELETE FROM login_attempts WHERE at<?", (now - 900,))
        failures = self.store.one("SELECT count(*) AS n FROM login_attempts WHERE ip=? AND at>?", (ip, now - 900))["n"]
        if failures >= 10:
            raise ApiError(429, "TOO_MANY_ATTEMPTS", "登录失败次数过多，请 15 分钟后再试")
        user = self.store.one("SELECT * FROM users WHERE lower(username)=lower(?)", (username,))
        if not user or not verify_password(password, user["password_hash"]):
            self.store.execute("INSERT INTO login_attempts(ip, at) VALUES (?, ?)", (ip, now))
            raise ApiError(401, "LOGIN_FAILED", "用户名或密码错误")
        if user["disabled"]:
            raise ApiError(403, "USER_DISABLED", "该账号已被管理员停用")
        return {**self.start_session(user["id"]), "user_id": user["id"], "username": user["username"],
                "role": user["role"]}

    def start_session(self, user_id: str) -> dict:
        now = time.time()
        cookie, csrf = new_token(), new_token(nbytes=24)
        self.store.execute("DELETE FROM sessions WHERE expires<?", (now,))
        self.store.execute("INSERT INTO sessions(id_hash, user_id, csrf, created, expires) VALUES (?, ?, ?, ?, ?)",
                           (sha256_hex(cookie), user_id, csrf, now, now + self.settings.session_ttl))
        self.store.execute("UPDATE users SET last_login=? WHERE id=?", (now, user_id))
        return {"cookie": cookie, "csrf": csrf}

    def session(self, request: Request) -> dict:
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie:
            raise ApiError(401, "LOGIN_REQUIRED", "请先登录")
        row = self.store.one("SELECT s.*, u.username, u.role, u.disabled FROM sessions s "
                             "JOIN users u ON u.id = s.user_id WHERE s.id_hash=? AND s.expires>?",
                             (sha256_hex(cookie), time.time()))
        if not row or row["disabled"]:
            raise ApiError(401, "LOGIN_REQUIRED", "登录已过期，请重新登录")
        return row

    def user(self, request: Request, write: bool = False) -> dict:
        """The signed-in panel user; write=True also checks the CSRF header."""
        row = self.session(request)
        if write and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf"]):
            raise ApiError(403, "CSRF_FAILED", "页面已过期，请刷新后重试")
        return row

    def admin(self, request: Request, write: bool = False) -> dict:
        row = self.user(request, write)
        if row["role"] != "admin":
            raise ApiError(403, "ADMIN_REQUIRED", "只有管理员可以执行此操作")
        return row

    def logout(self, request: Request) -> None:
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            self.store.execute("DELETE FROM sessions WHERE id_hash=?", (sha256_hex(cookie),))

    # ------------------------------------------------------------- bearer tokens
    def bearer(self, request: Request) -> tuple[Principal | None, str]:
        """Return (principal, state) where state is 'ok', 'missing' or 'invalid'."""
        header = request.headers.get("authorization", "")
        if not header:
            return None, "missing"
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return None, "invalid"
        return self.principal_for_token(value.strip())

    def principal_for_token(self, token: str) -> tuple[Principal | None, str]:
        row = self.store.one(
            "SELECT t.kind AS token_kind, t.expires AS token_expires, g.*, u.username, u.disabled "
            "FROM tokens t JOIN grants g ON g.id = t.grant_id JOIN users u ON u.id = g.user_id "
            "WHERE t.hash=? AND t.kind IN ('pat', 'access')", (sha256_hex(token),))
        now = time.time()
        if (not row or row["revoked"] or row["disabled"] or row["token_expires"] <= now
                or (row["expires"] and row["expires"] <= now)):
            return None, "invalid"
        # OAuth access tokens are audience-bound to this MCP endpoint (RFC 8707).
        if row["token_kind"] == "access" and row["resource"] not in (None, self.settings.mcp_url):
            return None, "invalid"
        if (row["last_used"] or 0) < now - 60:
            self.store.execute("UPDATE grants SET last_used=? WHERE id=?", (now, row["id"]))
        projects = json.loads(row["projects"])
        return Principal(kind=row["kind"], grant_id=row["id"], label=row["label"],
                         scopes=frozenset(json.loads(row["scopes"])),
                         projects=None if projects == "*" else tuple(projects), user_id=row["user_id"],
                         username=row["username"], client_id=row["client_id"]), "ok"

    def issue_pat(self, user_id: str, label: str, scopes, projects, days: int) -> dict:
        chosen = normalize_scopes(scopes)
        if "read" not in chosen:
            raise ApiError(400, "INVALID_SCOPE", "令牌至少需要 read 权限")
        now = time.time()
        grant_id, token = "g_" + uuid.uuid4().hex[:16], new_token("wc_pat_")
        expires = now + days * 86400
        with self.store.transaction() as db:
            db.execute("INSERT INTO grants(id, kind, label, scopes, projects, created, expires, user_id) "
                       "VALUES (?, 'pat', ?, ?, ?, ?, ?, ?)",
                       (grant_id, label, json.dumps(chosen), json.dumps(projects), now, expires, user_id))
            db.execute("INSERT INTO tokens(hash, grant_id, kind, expires, created) VALUES (?, ?, 'pat', ?, ?)",
                       (sha256_hex(token), grant_id, expires, now))
        return {"grant_id": grant_id, "token": token, "scopes": chosen, "expires": expires}

    def issue_oauth_tokens(self, grant_id: str) -> dict:
        grant = self.store.one("SELECT scopes FROM grants WHERE id=?", (grant_id,))
        now = time.time()
        access, refresh = new_token("wc_at_"), new_token("wc_rt_")
        with self.store.transaction() as db:
            db.execute("DELETE FROM tokens WHERE grant_id=? AND expires<?", (grant_id, now))
            db.execute("INSERT INTO tokens(hash, grant_id, kind, expires, created) VALUES (?, ?, 'access', ?, ?)",
                       (sha256_hex(access), grant_id, now + self.settings.access_token_ttl, now))
            db.execute("INSERT INTO tokens(hash, grant_id, kind, expires, created) VALUES (?, ?, 'refresh', ?, ?)",
                       (sha256_hex(refresh), grant_id, now + self.settings.refresh_token_ttl, now))
        return {"access_token": access, "token_type": "Bearer", "expires_in": self.settings.access_token_ttl,
                "refresh_token": refresh, "scope": " ".join(json.loads(grant["scopes"]))}

    def revoke_grant(self, grant_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE grants SET revoked=1 WHERE id=?", (grant_id,))
            db.execute("DELETE FROM tokens WHERE grant_id=?", (grant_id,))
