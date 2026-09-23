"""Agent network loop: connect out to the hub, authenticate, serve file operations."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import random
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from webcoder import VERSION
from webcoder.agent.config import AgentConfig
from webcoder.agent.fileops import FileOps
from webcoder.agent.lifecycle import retire
from webcoder.agent.procs import stop_all
from webcoder.common.channel import (
    UPGRADE_HEADER,
    ChannelError,
    SecureChannel,
    agent_proof,
    deleted_proof,
    hub_proof,
    new_nonce,
    upgrade_header,
)
from webcoder.common.crypto import b64d, b64e
from webcoder.common.errors import WebCoderError

log = logging.getLogger("webcoder.agent")

MAX_FRAME = 64 * 1024 * 1024
CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN, CLOSE_REPLACED, CLOSE_DELETED = 4401, 4403, 4409, 4410


class AuthRejected(Exception):
    """The hub refused this device (unknown, re-paired or disabled)."""


class DeviceDeleted(Exception):
    """The hub proved (with the device secret, or over the encrypted channel) that this device was deleted."""


def _stop_running() -> None:
    """The hub revoked this device: kill the commands it started (ssh, adb, hdc) instead of letting them run to
    their timeout. File operations are short and atomic; they finish."""
    stopped = stop_all()
    if stopped:
        log.warning("stopped %d running command(s): the hub revoked this device", stopped)


def _proven(value, expected: bytes) -> bool:
    try:
        return hmac.compare_digest(b64d(str(value or "")), expected)
    except ValueError:
        return False


def websocket_url(hub_url: str) -> str:
    base = hub_url.strip().rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/agent/ws"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/agent/ws"
    raise ValueError("hub_url must start with http:// or https://")


def proxy_setting(value: str | None):
    if value in (None, "", "none", "off", "direct"):
        return None
    if value == "system":
        return True
    return value


async def handshake(ws, config: AgentConfig) -> SecureChannel:
    try:
        challenge = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if not isinstance(challenge, dict) or challenge.get("t") != "challenge" or challenge.get("v") != 1:
            raise ChannelError("Unexpected handshake message from the hub")
        hub_nonce = b64d(challenge["nonce"])
        agent_nonce = new_nonce()
        secret = b64d(config.device_secret)
        await ws.send(json.dumps({
            "t": "hello", "v": 1, "device_id": config.device_id, "nonce": b64e(agent_nonce),
            "proof": b64e(agent_proof(secret, config.device_id, hub_nonce, agent_nonce)), "agent_version": VERSION,
        }))
        reply = json.loads(await asyncio.wait_for(ws.recv(), 15))
    except ConnectionClosed as exc:
        if exc.rcvd is not None and exc.rcvd.code in (CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN):
            raise AuthRejected(exc.rcvd.reason or "rejected by hub") from None
        raise
    if isinstance(reply, dict) and reply.get("t") == "error":
        if reply.get("code") == "DEVICE_DELETED" and _proven(
                reply.get("proof"), deleted_proof(secret, config.device_id, hub_nonce, agent_nonce)):
            raise DeviceDeleted()
        raise AuthRejected(f"{reply.get('code')}: {reply.get('message')}")  # includes an unproven "deleted"
    expected = hub_proof(secret, config.device_id, hub_nonce, agent_nonce)
    if not isinstance(reply, dict) or reply.get("t") != "welcome" or not hmac.compare_digest(
            b64d(str(reply.get("proof", ""))), expected):
        raise AuthRejected("The hub could not prove it knows this device's secret")
    return SecureChannel(secret, config.device_id, hub_nonce, agent_nonce, "agent")


def _vouch(config: AgentConfig) -> dict:
    """The upgrade request's proof that this is one of the hub's devices: the hub lets it in even while strangers
    hold the few places for connections not yet authenticated."""
    try:
        secret = b64d(config.device_secret)
    except ValueError:
        return {}
    if not secret or not config.device_id:
        return {}
    return {UPGRADE_HEADER: upgrade_header(secret, config.device_id, int(time.time()), new_nonce())}


def _stamp(path) -> tuple | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _reload(config: AgentConfig) -> AgentConfig:
    """Re-read the config file so re-pairing or folder edits made meanwhile take effect."""
    if config.path is not None and os.path.exists(config.path):
        try:
            return AgentConfig.load(config.path)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("could not reload %s: %s", config.path, exc)
    return config


async def _pause(stop: asyncio.Event, config: AgentConfig, stamp, seconds: float) -> None:
    """Wait before reconnecting; wake early on stop or when the config file changes (e.g. re-paired)."""
    deadline = time.monotonic() + seconds
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=min(remaining, 2.0))
        if config.path is not None and _stamp(config.path) != stamp:
            return


class AgentSession:
    """One authenticated connection: runs requests concurrently, replies in order of completion."""

    def __init__(self, ws, channel: SecureChannel, config: AgentConfig, ops: FileOps, concurrency: int = 8,
                 queue: int = 64):
        self.ws = ws
        self.channel = channel
        self.config = config
        self.ops = ops
        self._send_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task] = set()
        self._queue = queue  # requests running or waiting, each with its data (a file's content ...): no more

    async def watch_config(self, interval: float = 2.0) -> None:
        """Apply edits to the config file (allowed folders, read-only) while connected."""
        path = self.config.path
        if path is None:
            return
        seen = _stamp(path)
        while True:
            await asyncio.sleep(interval)
            stamp = _stamp(path)
            if stamp is None or stamp == seen:
                continue
            seen = stamp
            try:
                fresh = AgentConfig.load(path)
            except (OSError, ValueError, TypeError) as exc:
                log.warning("ignoring an unreadable config change: %s", exc)
                continue
            old = (self.config.hub_url, self.config.device_id, self.config.device_secret)
            if (fresh.hub_url, fresh.device_id, fresh.device_secret) != old:
                log.info("device credentials changed; reconnecting")
                await self.ws.close()
                return
            self.config, self.ops = fresh, fresh.build_fileops()
            log.info("configuration reloaded: %d allowed folder(s)%s", len(self.ops.sb.allowed_roots),
                     " (read-only)" if fresh.read_only else "")
            with contextlib.suppress(ConnectionClosed):
                await self.send({"t": "status", **self.ops.info()})

    async def send(self, message: dict) -> None:
        async with self._send_lock:
            await self.ws.send(self.channel.seal(message))

    async def serve(self) -> None:
        await self.send({"t": "status", **self.ops.info()})
        try:
            async for frame in self.ws:
                if isinstance(frame, str):
                    raise ChannelError("Unexpected text frame after the handshake")
                message = self.channel.open(frame)
                if message.get("t") == "deleted":  # authenticated by the channel
                    raise DeviceDeleted()
                if message.get("t") == "req":
                    if len(self._tasks) >= self._queue:
                        await self.send({"t": "res", "id": message.get("id"), "ok": False, "error": {
                            "code": "AGENT_BUSY", "message": "This computer is busy with other requests; try again "
                                                             "shortly"}})
                        continue
                    task = asyncio.create_task(self._handle(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        finally:
            for task in list(self._tasks):
                task.cancel()

    async def _handle(self, message: dict) -> None:
        request_id, op, args = message.get("id"), message.get("op"), message.get("args") or {}
        started = time.monotonic()
        async with self._slots:
            try:
                result = await asyncio.to_thread(self.ops.dispatch, op, args)
                reply = {"t": "res", "id": request_id, "ok": True, "result": result}
            except WebCoderError as exc:
                reply = {"t": "res", "id": request_id, "ok": False, "error": exc.to_dict()}
            except Exception as exc:  # noqa: BLE001 - report instead of dropping the connection
                log.exception("operation %s failed", op)
                reply = {"t": "res", "id": request_id, "ok": False,
                         "error": {"code": "AGENT_ERROR", "message": f"{type(exc).__name__}: {exc}"}}
        status = "ok" if reply["ok"] else reply["error"]["code"]
        log.info("%s %s -> %s (%.0f ms)", op, args.get("path", ""), status, (time.monotonic() - started) * 1000)
        with contextlib.suppress(ConnectionClosed):
            await self.send(reply)


async def run_agent(config: AgentConfig, stop: asyncio.Event | None = None, on_deleted=retire) -> None:
    """Connect to the hub and serve until ``stop`` is set, reconnecting with backoff.

    Returns for good once the hub proves the device was deleted, after ``on_deleted(config)``
    (by default: drop the credentials and remove this agent's autostart entry).
    """
    stop = stop or asyncio.Event()
    backoff = 1.0
    while not stop.is_set():
        delay = backoff
        stamp = _stamp(config.path) if config.path is not None else None
        config = _reload(config)
        ops = config.build_fileops()
        try:
            async with connect(websocket_url(config.hub_url), max_size=MAX_FRAME, open_timeout=15, ping_interval=20,
                               ping_timeout=20, close_timeout=5, proxy=proxy_setting(config.proxy),
                               additional_headers=_vouch(config),
                               user_agent_header=f"webcoder-agent/{VERSION}") as ws:
                channel = await handshake(ws, config)
                log.info("connected to %s as %s; serving %d root(s)", config.hub_url, config.device_id,
                         len(ops.sb.allowed_roots))
                backoff = 1.0
                session = AgentSession(ws, channel, config, ops)
                serve = asyncio.create_task(session.serve())
                watcher = asyncio.create_task(session.watch_config())
                waiter = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait({serve, waiter}, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                watcher.cancel()
                if serve in done:
                    serve.result()
                else:
                    serve.cancel()
                    with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                        await serve
                    break
        except DeviceDeleted:
            log.warning("this device was deleted in the hub panel; the agent stops for good")
            _stop_running()
            await asyncio.to_thread(on_deleted, config)
            return
        except AuthRejected as exc:
            # Revoked while the connection was down (disabled, re-paired ...): what it had started stops now, not
            # at its own timeout; stopping the task that waited for it does not stop the process.
            _stop_running()
            log.error("hub rejected this device: %s. Pair it again with: webcoder-agent pair", exc)
            delay = 300.0
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd is not None else None
            if code in (CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN, CLOSE_DELETED):  # revoked: disabled, re-paired...
                _stop_running()
            if code == CLOSE_REPLACED:
                log.warning("another agent with the same device credentials connected; retrying in 60s")
                delay = 60.0
            elif code == CLOSE_DELETED:  # the close code alone proves nothing: ask again, the hub answers provably
                log.warning("hub says this device was deleted; checking")
                delay = 2.0
            elif code in (CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN):
                log.error("hub closed the connection: %s", exc.rcvd.reason if exc.rcvd else code)
                delay = 300.0
            else:
                log.warning("connection closed (%s); reconnecting", code)
        except InvalidStatus as exc:
            log.warning("hub refused the WebSocket upgrade (HTTP %s)", exc.response.status_code)
        except (OSError, WebSocketException, asyncio.TimeoutError, ChannelError, ValueError) as exc:
            log.warning("connection problem: %s: %s", type(exc).__name__, exc)
        if stop.is_set():
            break
        backoff = min(backoff * 2, 60.0)
        await _pause(stop, config, stamp, delay + random.uniform(0, delay * 0.2))
