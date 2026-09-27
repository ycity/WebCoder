"""WebCoder: the `webcoder` command line and its browser extension, relayed by the hub.

`webcoder "task"` runs in a folder on a paired device. It signs every request with that device's secret
(common.channel.request_proof), so it needs no token of its own:

    CLI --POST /api/cli/project--> hub --agent.check_root--> agent      the folder becomes a project
    CLI --POST /api/cli/jobs-----> hub ==/browser/ws=======> extension  types the task into chatgpt.com
    CLI <--POST /api/cli/jobs/{id}/events (long poll)-- hub <== extension: the reply as it streams
                                                        hub <-- /mcp: ChatGPT's tool calls on the project

The extension links to the owner's account once, with a code from `webcoder link`; from then on it holds a
token. Both travel as a WebSocket subprotocol, so the hub turns strangers away before it accepts a connection
and reads nothing from them. Tasks live in memory only: they last minutes, and a restarted hub has none.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from webcoder import VERSION
from webcoder.common.channel import NONCE_BYTES, SIGNATURE_HEADER, request_proof
from webcoder.common.crypto import b64d, new_token, normalize_pairing_code, pairing_code, sha256_hex
from webcoder.common.errors import WebCoderError
from webcoder.hub.auth import ApiError
from webcoder.hub.doorbell import add_doorbell_routes
from webcoder.hub.oauth import client_ip

log = logging.getLogger("webcoder.hub.browser")

PROTOCOL = "webcoder.v1"          # the subprotocol the hub accepts; the credential comes as a second one
CODE_TTL = 900                    # seconds a link code from `webcoder link` stays valid
SIGNATURE_WINDOW = 300            # seconds a signed CLI request is good for (clocks differ)
JOB_KEEP = 600                    # finished tasks kept this long, so the CLI can read their last events
MAX_ACTIVE_JOBS = 4               # unfinished tasks per account
MAX_EVENTS = 5000                 # events kept per task (beyond it only the final ones are)
MAX_TEXT = 400_000                # characters of reply text in one event
MAX_FIELD = 4000                  # characters of any other event field
MAX_MESSAGE = 1024 * 1024         # characters of one message from an extension
RECONNECT_GRACE = 45.0            # seconds a task waits for its browser to come back after a disconnect
CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN, CLOSE_REPLACED = 4401, 4403, 4409
EXTENSION_ORIGINS = ("chrome-extension://", "moz-extension://", "safari-web-extension://")
CHATGPT = "https://chatgpt.com/"
CONVERSATION_RE = re.compile(r"^https://chatgpt\.com/[A-Za-z0-9/_\-]{1,300}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# What an extension may report, and the text fields each kind carries.
EVENT_FIELDS = {
    "status": ("text",),
    "delta": ("text",),            # more of the reply
    "text": ("text",),             # the whole reply so far (it changed other than by growing)
    "conversation": ("url", "title"),
    "model": ("text",),
    "approval": ("tool", "text", "decision"),
    "warning": ("text",),
    "done": ("text", "url"),
    "error": ("code", "text"),
}
FINAL = ("done", "error", "cancelled")


# --------------------------------------------------------------------------- request bodies
class ProjectBody(BaseModel):
    root: str = Field(min_length=1, max_length=1024)
    alias: str | None = Field(default=None, pattern=ALIAS_RE.pattern)


class JobBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=200_000)
    conversation: str | None = Field(default=None, max_length=320)
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,100}$")
    app: str | None = Field(default=None, min_length=1, max_length=80)  # the ChatGPT app to @-mention
    auto_approve: bool = True
    timeout: int = Field(default=3600, ge=30, le=6 * 3600)
    browser: str | None = Field(default=None, max_length=80)  # a linked browser's id or name


class EventsBody(BaseModel):
    after: int = Field(default=0, ge=0)
    wait: float = Field(default=25.0, ge=0, le=50)


class RevokeBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)


class EmptyBody(BaseModel):
    pass


# --------------------------------------------------------------------------- state
@dataclass
class Job:
    id: str
    user_id: str
    device_id: str
    project: dict
    link_id: str
    request: dict
    device_name: str = ""
    created: float = field(default_factory=time.time)
    events: list = field(default_factory=list)
    seq: int = 0
    state: str = "running"
    finished: float | None = None
    conversation: str | None = None
    truncated: bool = False
    waiter: asyncio.Event = field(default_factory=asyncio.Event)
    watchdog: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self.finished is None


class BrowserConnection:
    def __init__(self, link: dict, websocket: WebSocket):
        self.link_id = link["id"]
        self.user_id = link["user_id"]
        self.name = link["name"]
        self.websocket = websocket
        self.connected_at = time.time()
        self.version = ""
        self._lock = asyncio.Lock()

    async def send(self, message: dict) -> None:
        async with self._lock:
            await self.websocket.send_text(json.dumps(message, ensure_ascii=False))

    async def close(self, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.websocket.close(code=code, reason=reason)


class _Rejected(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.close_code, self.reason = code, reason


def _clip(value, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return text if len(text) <= limit else text[:limit]


def clean_event(raw) -> dict | None:
    """An event from an extension, reduced to the fields its kind has (strings, within limits); None if the
    kind is unknown."""
    if not isinstance(raw, dict) or raw.get("type") not in EVENT_FIELDS:
        return None
    event = {"type": raw["type"]}
    for name in EVENT_FIELDS[raw["type"]]:
        if isinstance(raw.get(name), str):
            event[name] = _clip(raw[name], MAX_TEXT if name == "text" else MAX_FIELD)
    for name in ("url",):
        if name in event and not CONVERSATION_RE.match(event[name]):
            del event[name]  # only a chatgpt.com address is ever shown or remembered
    return event


def alias_for(folder: str) -> str:
    """A project alias from a folder name: the characters aliases allow, or 'project'."""
    name = re.split(r"[\\/]", folder.rstrip("\\/"))[-1] if folder.rstrip("\\/") else ""
    alias = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")[:56]
    return alias if alias and ALIAS_RE.match(alias) else "project"


# --------------------------------------------------------------------------- the relay
class BrowserRelay:
    def __init__(self, runtime):
        self.rt = runtime
        self.connections: dict[str, BrowserConnection] = {}
        self.jobs: dict[str, Job] = {}
        self._used: dict[bytes, float] = {}  # request proofs already taken, until they expire
        self._tasks: set[asyncio.Task] = set()
        self.drops: dict[str, int] = {}  # link id -> how many times it disconnected (see _lost)

    # ------------------------------------------------------------------ device signatures (the CLI)
    def verify(self, request: Request, body: bytes) -> dict:
        """The device a signed CLI request comes from (enabled, its owner enabled), or ApiError."""
        def refuse(status: int, code: str, message: str):
            self.rt.limiter.check("cli-refused:" + client_ip(request), 30, 600)  # 429 once guessing goes on
            raise ApiError(status, code, message)

        try:
            device_id, stamp, nonce, proof = request.headers.get(SIGNATURE_HEADER, "").split(" ")
            stamp, nonce, proof = int(stamp), b64d(nonce), b64d(proof)
        except ValueError:
            refuse(401, "SIGNATURE_REQUIRED", "This endpoint is for the webcoder command line: it signs each "
                                              "request with the device secret of a paired agent")
        now = time.time()
        if abs(now - stamp) > SIGNATURE_WINDOW:
            refuse(401, "SIGNATURE_EXPIRED", "The signature is too old or from the future: check that this "
                                             "computer's clock is right")
        if len(nonce) != NONCE_BYTES or len(proof) != 32:
            refuse(401, "BAD_SIGNATURE", "Malformed signature")
        device = self.rt.store.one("SELECT d.*, u.username, u.disabled AS owner_disabled FROM devices d "
                                   "LEFT JOIN users u ON u.id = d.user_id WHERE d.id=?", (device_id,))
        if not device or not device["secret_enc"]:
            refuse(401, "UNKNOWN_DEVICE", "Unknown or unpaired device: pair this computer's agent again")
        secret = b64d(self.rt.store.decrypt(device["secret_enc"]))
        raw_path = request.scope.get("raw_path") or request.url.path.encode()
        query = request.scope.get("query_string") or b""
        target = raw_path.decode("latin-1") + ("?" + query.decode("latin-1") if query else "")
        if not hmac.compare_digest(proof, request_proof(secret, device_id, stamp, nonce, request.method, target,
                                                        body)):
            refuse(401, "BAD_SIGNATURE", "The signature does not match this device: pair the agent again")
        self._used = {taken: until for taken, until in self._used.items() if until > now}
        if proof in self._used:
            refuse(401, "REPLAYED", "This signed request was already used")
        self._used[proof] = now + 2 * SIGNATURE_WINDOW
        if not device["enabled"]:
            raise ApiError(403, "DEVICE_DISABLED", "This device is disabled in the hub panel")
        if device["owner_disabled"] != 0:
            raise ApiError(403, "USER_DISABLED", "The account that owns this device is disabled")
        return device

    # ------------------------------------------------------------------ links
    def new_code(self, device: dict) -> dict:
        code = pairing_code()
        expires = time.time() + CODE_TTL
        self.rt.store.execute("DELETE FROM browser_codes WHERE expires<?", (time.time(),))
        self.rt.store.execute("INSERT INTO browser_codes(code_hash, user_id, device_id, expires) VALUES (?, ?, ?, ?)",
                              (sha256_hex(code), device["user_id"], device["id"], expires))
        return {"code": code, "expires": expires}

    def links(self, user_id: str) -> list[dict]:
        rows = self.rt.store.all("SELECT id, name, created, last_seen FROM browser_links WHERE user_id=? AND "
                                 "revoked=0 ORDER BY created", (user_id,))
        for row in rows:
            connection = self.connections.get(row["id"])
            row["online"] = connection is not None
            row["version"] = connection.version if connection else None
        return rows

    async def _end_link(self, link_id: str, close_code: int, reason: str, event: dict) -> None:
        """The link goes away (revoked, its account disabled): its tasks end, and the browser is told to stop each
        of them before the connection closes."""
        connection = self.connections.pop(link_id, None)
        for job in [job for job in self.jobs.values() if job.link_id == link_id and job.active]:
            self.finish(job, event)
            if connection is not None:
                with contextlib.suppress(Exception):
                    await connection.send({"t": "cancel", "job": job.id})
        if connection is not None:
            await connection.close(close_code, reason)

    async def revoke(self, user_id: str, link_id: str) -> None:
        if self.rt.store.execute("UPDATE browser_links SET revoked=1 WHERE id=? AND user_id=? AND revoked=0",
                                 (link_id, user_id)) != 1:
            raise ApiError(404, "NOT_FOUND", "No such linked browser")
        await self._end_link(link_id, CLOSE_UNAUTHORIZED, "revoked",
                             {"type": "error", "code": "BROWSER_LOST", "text": "The browser's link was revoked"})

    async def drop_user(self, user_id: str) -> None:
        """The account was disabled or deleted: its browsers go, and its tasks end."""
        event = {"type": "error", "code": "USER_DISABLED", "text": "The account was disabled"}
        for link_id, connection in list(self.connections.items()):
            if connection.user_id == user_id:
                await self._end_link(link_id, CLOSE_FORBIDDEN, "user disabled", event)
        for job in list(self.jobs.values()):
            if job.user_id == user_id and job.active:
                self.finish(job, event)

    # ------------------------------------------------------------------ the extension's socket
    def _credential(self, websocket: WebSocket) -> tuple[str, str]:
        offered = list(websocket.scope.get("subprotocols") or [])
        if PROTOCOL not in offered:
            raise _Rejected(CLOSE_UNAUTHORIZED, "missing subprotocol")
        for item in offered:
            kind, _, value = item.partition(".")
            if kind in ("token", "pair") and value:
                return kind, value
        raise _Rejected(CLOSE_UNAUTHORIZED, "no credential")

    def _authenticate(self, websocket: WebSocket) -> tuple[dict, str | None]:
        origin = websocket.headers.get("origin", "")
        if origin and not origin.startswith(EXTENSION_ORIGINS):  # a web page has no business here
            raise _Rejected(CLOSE_FORBIDDEN, "origin")
        kind, value = self._credential(websocket)
        ip = client_ip(websocket)
        if kind == "token":
            link = self.rt.store.one("SELECT l.*, u.disabled FROM browser_links l JOIN users u ON u.id = l.user_id "
                                     "WHERE l.token_hash=?", (sha256_hex(value),))
            if not link or link["revoked"]:
                self.rt.limiter.check("browser-refused:" + ip, 30, 600)
                raise _Rejected(CLOSE_UNAUTHORIZED, "unknown token")
            if link["disabled"]:
                raise _Rejected(CLOSE_FORBIDDEN, "user disabled")
            return link, None
        self.rt.limiter.check("browser-pair:" + ip, 10, 600)
        code = normalize_pairing_code(value)
        now = time.time()
        with self.rt.store.transaction() as db:
            row = db.execute("SELECT c.*, u.disabled FROM browser_codes c JOIN users u ON u.id = c.user_id "
                             "WHERE c.code_hash=? AND c.expires>?", (sha256_hex(code), now)).fetchone()
            if not row or row["disabled"]:
                raise _Rejected(CLOSE_UNAUTHORIZED, "invalid code")
            db.execute("DELETE FROM browser_codes WHERE code_hash=?", (row["code_hash"],))  # once only
            token, link_id = new_token("wcb_"), "bl_" + uuid.uuid4().hex[:12]
            db.execute("INSERT INTO browser_links(id, user_id, name, token_hash, created) VALUES (?, ?, ?, ?, ?)",
                       (link_id, row["user_id"], "Browser", sha256_hex(token), now))
        link = self.rt.store.one("SELECT * FROM browser_links WHERE id=?", (link_id,))
        self.rt.audit("浏览器扩展", "browser.linked", link_id, user_id=row["user_id"])
        return link, token

    async def serve(self, websocket: WebSocket) -> None:
        try:
            link, token = self._authenticate(websocket)
        except _Rejected as exc:
            log.info("browser connection refused: %s", exc.reason)
            with contextlib.suppress(Exception):
                if exc.close_code == CLOSE_UNAUTHORIZED and PROTOCOL in (websocket.scope.get("subprotocols") or []):
                    # Accepted only to say why (a browser sees no reason for a refused upgrade), then closed at
                    # once: nothing it sends is read.
                    await websocket.accept(subprotocol=PROTOCOL)
                    await websocket.close(code=CLOSE_UNAUTHORIZED, reason=exc.reason)
                else:  # refused before the upgrade
                    await websocket.close(code=exc.close_code)
            return
        except ApiError as exc:  # rate limited
            log.info("browser connection refused: %s", exc.code)
            with contextlib.suppress(Exception):
                await websocket.close(code=1013)
            return
        await websocket.accept(subprotocol=PROTOCOL)
        connection = BrowserConnection(link, websocket)
        if token is not None:  # a new link: hand over its token, then the extension reconnects with it
            with contextlib.suppress(Exception):
                await connection.send({"t": "paired", "token": token, "link_id": link["id"]})
                await websocket.close(code=1000)
            return
        previous = self.connections.get(link["id"])
        self.connections[link["id"]] = connection
        if previous is not None:
            await previous.close(CLOSE_REPLACED, "replaced by a newer connection")
        self.rt.store.execute("UPDATE browser_links SET last_seen=? WHERE id=?", (time.time(), link["id"]))
        user = self.rt.store.one("SELECT username FROM users WHERE id=?", (link["user_id"],))
        try:
            await connection.send({"t": "welcome", "link_id": link["id"], "name": link["name"],
                                   "user": user["username"] if user else "", "hub_version": VERSION})
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is None or len(text) > MAX_MESSAGE:
                    await connection.close(1009 if text else 1003, "message")
                    break
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    await self._on_message(connection, payload)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if self.connections.get(link["id"]) is connection:
                del self.connections[link["id"]]
                self.rt.store.execute("UPDATE browser_links SET last_seen=? WHERE id=?", (time.time(), link["id"]))
                self._lost(link["id"])

    async def _on_message(self, connection: BrowserConnection, payload: dict) -> None:
        kind = payload.get("t")
        if kind == "ping":
            await connection.send({"t": "pong"})
        elif kind == "hello":
            connection.version = _clip(payload.get("version"), 40)
            name = _clip(payload.get("name"), 80).strip()
            if name and name != connection.name:
                connection.name = name
                self.rt.store.execute("UPDATE browser_links SET name=? WHERE id=?", (name, connection.link_id))
            running = {item for item in payload.get("running") or [] if isinstance(item, str)}
            for job in list(self.jobs.values()):  # tasks it no longer knows (its service worker restarted)
                if job.link_id == connection.link_id and job.active and job.id not in running:
                    self.finish(job, {"type": "error", "code": "BROWSER_RESTARTED",
                                      "text": "The browser extension restarted and lost this task"})
            for job_id in list(running)[:100]:  # ones it still runs that ended here meanwhile (cancelled while it
                job = self.jobs.get(job_id)     # was away, timed out, or unknown after a hub restart): stop them
                if job is None or not job.active:
                    await connection.send({"t": "cancel", "job": job_id})
        elif kind == "event":
            job_id = str(payload.get("job"))
            raw = payload.get("event")
            job = self.jobs.get(job_id)
            event = clean_event(raw)
            if job is not None and event is not None and job.link_id == connection.link_id and job.active:
                if event["type"] == "conversation" and event.get("url"):
                    job.conversation = event["url"]
                if event["type"] in FINAL:
                    if event["type"] == "done" and event.get("url"):
                        job.conversation = event["url"]
                    self.finish(job, event)
                else:
                    self.add(job, event)
            if isinstance(raw, dict) and raw.get("type") in FINAL:  # the browser keeps it until this arrives
                await connection.send({"t": "ack", "job": job_id})

    def _lost(self, link_id: str) -> None:
        """The browser went away: its tasks wait a while for it to come back and say it still runs them. Each
        disconnect counts: a timer from an earlier one that ended in a reconnect does nothing."""
        self.drops[link_id] = generation = self.drops.get(link_id, 0) + 1
        jobs = [job for job in self.jobs.values() if job.link_id == link_id and job.active]
        for job in jobs:
            self.add(job, {"type": "warning", "text": "The browser extension disconnected; waiting for it to "
                                                      "reconnect"})

        async def later():
            await asyncio.sleep(RECONNECT_GRACE)
            if link_id not in self.connections and self.drops.get(link_id) == generation:
                self._orphan(link_id, "The browser extension disconnected")

        if jobs:
            self._spawn(later())

    def _orphan(self, link_id: str, why: str) -> None:
        for job in list(self.jobs.values()):
            if job.link_id == link_id and job.active:
                self.finish(job, {"type": "error", "code": "BROWSER_LOST", "text": why})

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ tasks
    def add(self, job: Job, event: dict) -> None:
        if len(job.events) >= MAX_EVENTS and event["type"] not in FINAL:
            if event["type"] in ("delta", "text", "status"):
                job.truncated = True  # the final event carries the whole reply anyway
                return
        job.seq += 1
        job.events.append({"seq": job.seq, "ts": round(time.time(), 3), **event})
        job.waiter.set()
        job.waiter = asyncio.Event()

    def finish(self, job: Job, event: dict) -> None:
        if not job.active:
            return
        job.state = {"done": "done", "cancelled": "cancelled"}.get(event["type"], "error")
        job.finished = time.time()
        self.add(job, event)
        if job.watchdog is not None and job.watchdog is not asyncio.current_task():
            job.watchdog.cancel()
        self.rt.audit(f"设备:{job.device_name or job.device_id}·CLI", "webcoder.task", job.project["alias"],
                      "ok" if job.state == "done" else event.get("code") or job.state.upper(),
                      duration_ms=int((job.finished - job.created) * 1000), user_id=job.user_id)

    def sweep(self) -> None:
        now = time.time()
        for job_id, job in list(self.jobs.items()):
            if job.finished is not None and job.finished < now - JOB_KEEP:
                del self.jobs[job_id]

    def pick_browser(self, user_id: str, wanted: str | None) -> BrowserConnection:
        mine = sorted((c for c in self.connections.values() if c.user_id == user_id),
                      key=lambda c: c.connected_at, reverse=True)
        if wanted:
            key = wanted.strip().lower()
            mine = [c for c in mine if c.link_id == wanted.strip() or c.name.lower() == key]
        if not mine:
            raise ApiError(409, "BROWSER_OFFLINE", "No WebCoder browser extension is online for this account"
                           + (f" under the name {wanted!r}" if wanted else "")
                           + ". Open the browser that has the extension; to link one, run: webcoder link")
        return mine[0]

    async def submit(self, device: dict, body: JobBody) -> Job:
        self.sweep()
        project = self.rt.store.one("SELECT * FROM projects WHERE id=? AND device_id=?",
                                    (body.project_id, device["id"]))
        if not project:
            raise ApiError(404, "PROJECT_NOT_FOUND", "No such project on this device")
        if body.conversation and not CONVERSATION_RE.match(body.conversation):
            raise ApiError(400, "INVALID_CONVERSATION", "conversation must be a https://chatgpt.com/... address")
        active = [job for job in self.jobs.values() if job.user_id == device["user_id"] and job.active]
        if len(active) >= MAX_ACTIVE_JOBS:
            raise ApiError(429, "TOO_MANY_TASKS", f"{len(active)} tasks are still running for this account; "
                                                  "wait for one to finish")
        if body.conversation and any(job.conversation == body.conversation or
                                     job.request.get("conversation") == body.conversation for job in active):
            raise ApiError(409, "CONVERSATION_BUSY", "A task is still running in this ChatGPT conversation")
        connection = self.pick_browser(device["user_id"], body.browser)
        job = Job(id="job_" + uuid.uuid4().hex[:16], user_id=device["user_id"], device_id=device["id"],
                  project={"id": project["id"], "alias": project["alias"], "root": project["root"]},
                  link_id=connection.link_id, request=body.model_dump(), device_name=device["name"],
                  conversation=body.conversation)
        self.jobs[job.id] = job
        self.add(job, {"type": "browser", "name": connection.name, "id": connection.link_id})
        try:
            await connection.send({"t": "job", "job": {
                "id": job.id, "prompt": body.prompt, "conversation": body.conversation, "model": body.model,
                "app": body.app, "auto_approve": body.auto_approve, "timeout": body.timeout,
                "project": project["alias"]}})
        except Exception:  # noqa: BLE001 - the socket was closing
            self.finish(job, {"type": "error", "code": "BROWSER_LOST", "text": "The browser extension disconnected"})
            raise ApiError(409, "BROWSER_OFFLINE", "The browser extension disconnected; try again") from None

        async def watchdog():
            await asyncio.sleep(body.timeout + 60)
            await self.cancel(job, {"type": "error", "code": "TIMEOUT", "text": f"No result within {body.timeout} s"})

        job.watchdog = self._spawn(watchdog())
        return job

    def job_for(self, device: dict, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None or job.user_id != device["user_id"]:
            raise ApiError(404, "NOT_FOUND", "No such task (finished tasks are kept for 10 minutes)")
        return job

    async def wait_events(self, job: Job, after: int, wait: float) -> list[dict]:
        deadline = time.monotonic() + wait
        while True:
            new = [event for event in job.events if event["seq"] > after]
            remaining = deadline - time.monotonic()
            if new or not job.active or remaining <= 0:
                return new
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(job.waiter.wait(), remaining)

    async def cancel(self, job: Job, event: dict | None = None) -> None:
        """End a task and tell its browser to stop it (now, or when it reconnects and still reports it)."""
        if not job.active:
            return
        self.finish(job, event or {"type": "cancelled"})
        connection = self.connections.get(job.link_id)
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.send({"t": "cancel", "job": job.id})

    def tool_called(self, principal, name: str, target: str, status: str, duration_ms: int) -> None:
        """One MCP tool call (from tools.execute): shown in the running tasks of the same account whose
        project it touched."""
        if not self.jobs or not target:
            return
        lowered = target.lower()
        for job in self.jobs.values():
            if job.user_id != principal.user_id or not job.active:
                continue
            alias = job.project["alias"].lower() + ":"
            if lowered.startswith(alias) or f" {alias}" in lowered or f">{alias}" in lowered:
                self.add(job, {"type": "tool", "name": name, "target": target[:MAX_FIELD], "status": status,
                               "ms": duration_ms})


# --------------------------------------------------------------------------- the CLI's endpoints
def make_cli_router(rt) -> APIRouter:
    router = APIRouter(prefix="/api/cli")
    relay: BrowserRelay = rt.browser
    store = rt.store

    async def signed(request: Request, model):
        body = await request.body()
        device = relay.verify(request, body)
        try:
            parsed = model.model_validate_json(body or b"{}")
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}" for item in exc.errors())
            raise ApiError(422, "INVALID_REQUEST", problems or "Invalid request") from None
        return device, parsed

    def audit(device: dict, action: str, target: str = "", **kwargs) -> None:
        rt.audit(f"设备:{device['name']}·CLI", action, target, user_id=device["user_id"], **kwargs)

    def project_view(row: dict, created: bool) -> dict:
        warnings = []
        if row["mode"] != "rw":
            warnings.append("This project is read-only in the hub panel: ChatGPT can read files but not change "
                            "them")
        grants = store.all("SELECT kind, label, projects FROM grants WHERE user_id=? AND revoked=0 AND "
                           "(expires IS NULL OR expires>?)", (row["user_id"], time.time()))
        reach = [g for g in grants if json.loads(g["projects"]) == "*" or row["id"] in json.loads(g["projects"])]
        if grants and not reach:
            warnings.append("None of your ChatGPT authorizations / tokens include this project (they are limited "
                            "to other projects): authorize the app again in ChatGPT and include it")
        elif not grants:
            warnings.append("No ChatGPT authorization exists yet: add the WebCoder app in ChatGPT first")
        return {"id": row["id"], "alias": row["alias"], "root": row["root"], "mode": row["mode"],
                "created": created, "warnings": warnings}

    @router.post("/status")
    async def status(request: Request):
        device, _ = await signed(request, EmptyBody)
        projects = store.all("SELECT id, alias, root, mode FROM projects WHERE device_id=? ORDER BY alias_key",
                             (device["id"],))
        return {"hub_version": VERSION, "user": device["username"],
                "device": {"id": device["id"], "name": device["name"], "online": rt.agents.is_online(device["id"])},
                "browsers": relay.links(device["user_id"]), "projects": projects}

    @router.post("/project")
    async def ensure_project(request: Request):
        device, body = await signed(request, ProjectBody)
        try:
            checked = await rt.agents.call(device["id"], "agent.check_root", {"root": body.root}, 15.0)
        except WebCoderError as exc:
            status_code = 409 if exc.code in ("DEVICE_OFFLINE", "AGENT_TIMEOUT", "AGENT_DISCONNECTED") else 400
            raise ApiError(status_code, exc.code, exc.message) from None
        root = checked["root"]
        windows = json.loads(device["info"] or "{}").get("platform") == "Windows"
        same = (lambda a, b: a.casefold() == b.casefold()) if windows else (lambda a, b: a == b)
        for row in store.all("SELECT * FROM projects WHERE device_id=? ORDER BY created", (device["id"],)):
            if same(row["root"], root):
                return project_view(row, False)
        base = body.alias or alias_for(root)
        taken = {row["alias_key"] for row in store.all("SELECT alias_key FROM projects WHERE user_id=?",
                                                       (device["user_id"],))}
        alias = base
        for number in range(2, 1000):
            if alias.lower() not in taken:
                break
            if body.alias:
                raise ApiError(409, "ALIAS_TAKEN", f"The project name {body.alias} is already used")
            alias = f"{base[:58]}-{number}"
        project_id = "prj_" + uuid.uuid4().hex[:12]
        store.execute("INSERT INTO projects(id, alias, alias_key, device_id, root, mode, description, created, "
                      "user_id) VALUES (?, ?, ?, ?, ?, 'rw', ?, ?, ?)",
                      (project_id, alias, alias.lower(), device["id"], root, "webcoder", time.time(),
                       device["user_id"]))
        audit(device, "project.created", f"{alias} → {device['name']}:{root}")
        return project_view(store.one("SELECT * FROM projects WHERE id=?", (project_id,)), True)

    @router.post("/browser-code")
    async def browser_code(request: Request):
        device, _ = await signed(request, EmptyBody)
        audit(device, "browser.code")
        return relay.new_code(device)

    @router.post("/browsers/revoke")
    async def revoke_browser(request: Request):
        device, body = await signed(request, RevokeBody)
        await relay.revoke(device["user_id"], body.id)
        audit(device, "browser.revoked", body.id)
        return {"ok": True}

    @router.post("/jobs")
    async def submit(request: Request):
        device, body = await signed(request, JobBody)
        job = await relay.submit(device, body)
        audit(device, "webcoder.submitted", job.project["alias"])
        return {"job_id": job.id, "browser": relay.connections[job.link_id].name
                if job.link_id in relay.connections else None, "project": job.project}

    @router.post("/jobs/{job_id}/events")
    async def events(job_id: str, request: Request):
        device, body = await signed(request, EventsBody)
        job = relay.job_for(device, job_id)
        new = await relay.wait_events(job, body.after, body.wait)
        return {"events": new, "state": job.state, "conversation": job.conversation, "truncated": job.truncated}

    @router.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str, request: Request):
        device, _ = await signed(request, EmptyBody)
        await relay.cancel(relay.job_for(device, job_id))
        return {"ok": True}

    add_doorbell_routes(router, rt, signed)
    return router
