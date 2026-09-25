"""Long-lived hub state shared by all routers."""
from __future__ import annotations

import collections
import json
import logging
import sqlite3
import threading
import time

from webcoder.hub.agents import AgentRegistry
from webcoder.hub.auth import ApiError, Auth
from webcoder.hub.clients import RedirectPolicy
from webcoder.hub.config import Settings
from webcoder.hub.oauth import OAuthService
from webcoder.hub.store import Store

log = logging.getLogger("webcoder.hub")


class RateLimiter:
    """In-memory sliding-window limiter for unauthenticated endpoints."""

    IDLE = 3600.0  # longer than any window: keys (client addresses) idle this long are forgotten

    def __init__(self):
        self._hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()
        self._checks = 0

    def check(self, key: str, limit: int, window: float) -> None:
        now = time.monotonic()
        with self._lock:
            self._checks += 1
            if self._checks % 1000 == 0:  # a flood from many addresses must not grow this forever
                for stale in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.IDLE]:
                    del self._hits[stale]
            hits = self._hits[key]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                raise ApiError(429, "RATE_LIMITED", "请求过于频繁，请稍后再试")
            hits.append(now)


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.auth = Auth(self.store, settings)
        self.redirects = RedirectPolicy(settings.clients, settings.extra_redirect_uris)
        self.agents = AgentRegistry(self)
        self.oauth = OAuthService(self)
        self.limiter = RateLimiter()
        from webcoder.hub.browser import BrowserRelay
        self.browser = BrowserRelay(self)  # webcoder: tasks from the command line, typed into ChatGPT by the extension
        self._audit_writes = 0
        from webcoder.hub.collaboration.lifecycle import install
        install(self)  # self.collaboration: rooms, dot / Work chats, tasks, MCP Events (off unless switched on)

    def audit(self, actor: str, action: str, target: str = "", status: str = "ok", detail=None,
              duration_ms: int | None = None, user_id: str | None = None) -> None:
        try:
            self.store.execute("INSERT INTO audit(ts, actor, action, target, status, detail, duration_ms, user_id) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (time.time(), actor, action, target or "", status,
                                json.dumps(detail or {}, ensure_ascii=False), duration_ms, user_id))
            self._audit_writes += 1
            if self._audit_writes % 500 == 0:  # keep the log bounded
                self.store.execute("DELETE FROM audit WHERE id <= (SELECT max(id) FROM audit) - 20000")
        except sqlite3.Error:
            log.exception("audit write failed")

    def close(self) -> None:
        self.store.close()
