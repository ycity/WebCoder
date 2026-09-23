"""Connected agents: the hub side of the agent WebSocket and request routing."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
import uuid

from starlette.websockets import WebSocket, WebSocketDisconnect

from webcoder.common.channel import (
    NONCE_BYTES,
    UPGRADE_HEADER,
    ChannelError,
    SecureChannel,
    agent_proof,
    deleted_proof,
    hub_proof,
    new_nonce,
    upgrade_proof,
)
from webcoder.common.crypto import b64d, b64e
from webcoder.common.errors import WebCoderError

log = logging.getLogger("webcoder.hub.agents")

CLOSE_BAD_REQUEST, CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN, CLOSE_REPLACED = 4400, 4401, 4403, 4409
CLOSE_DELETED = 4410  # the device was deleted in the panel: the agent stops and removes its autostart
# Anyone can open /agent/ws, and the server holds a whole frame (up to ws_frame_limit, and WS_MAX_QUEUE more)
# before the hub sees it: only this many connections from strangers may be before authentication at once, each for
# a short time. A device that proves itself in the upgrade request (UPGRADE_HEADER) needs no such place, so
# strangers holding them cannot keep the hub's devices out.
MAX_HANDSHAKES = 2
HANDSHAKE_SECONDS = 10
UPGRADE_WINDOW = 300  # seconds an upgrade proof is good for, either way (clocks differ); each is taken once
STATUS_FIELDS = ("agent_version", "platform", "platform_release", "hostname", "python", "allowed_roots", "read_only",
                 "mobile", "ssh", "git", "skills", "commands")
_UNKNOWN_OUTCOME = "If this was a write, its outcome is unknown: call stat to check the file before retrying."


class _Rejected(Exception):
    def __init__(self, code: str, message: str, close_code: int, **extra):
        super().__init__(message)
        self.code, self.message, self.close_code, self.extra = code, message, close_code, extra


class AgentConnection:
    def __init__(self, device_id: str, websocket: WebSocket, channel: SecureChannel):
        self.device_id = device_id
        self.websocket = websocket
        self.channel = channel
        self.connected_at = time.time()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}

    async def send(self, message: dict) -> None:
        async with self._send_lock:  # seal + send together so sequence numbers match wire order
            await self.websocket.send_bytes(self.channel.seal(message))

    async def request(self, op: str, args: dict, timeout: float) -> dict:
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            try:
                await self.send({"t": "req", "id": request_id, "op": op, "args": args})
            except Exception:  # noqa: BLE001 - socket already closing
                raise WebCoderError("AGENT_DISCONNECTED", "The device disconnected. " + _UNKNOWN_OUTCOME) from None
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise WebCoderError("AGENT_TIMEOUT", f"The device did not answer within {timeout:.0f}s. "
                                + _UNKNOWN_OUTCOME) from None
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, message: dict) -> None:
        future = self._pending.get(str(message.get("id")))
        if future is not None and not future.done():
            future.set_result(message)

    def fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(WebCoderError("AGENT_DISCONNECTED", "The device disconnected before answering. "
                                                   + _UNKNOWN_OUTCOME))

    async def close(self, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.websocket.close(code=code, reason=reason)


class AgentRegistry:
    def __init__(self, runtime):
        self.rt = runtime
        self.connections: dict[str, AgentConnection] = {}
        self.handshakes = 0  # connections from strangers not yet authenticated
        self._used: dict[bytes, float] = {}  # upgrade proofs already taken, until they expire

    def is_online(self, device_id: str) -> bool:
        return device_id in self.connections

    async def call(self, device_id: str, op: str, args: dict, timeout: float | None = None) -> dict:
        connection = self.connections.get(device_id)
        if connection is None:
            raise WebCoderError("DEVICE_OFFLINE", "The device that hosts this project is offline (its agent is not "
                                "connected to the hub)")
        reply = await connection.request(op, args, timeout or self.rt.settings.agent_timeout)
        if reply.get("ok"):
            return reply.get("result") or {}
        error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        details = {key: value for key, value in error.items() if key not in ("code", "message")}
        raise WebCoderError(str(error.get("code") or "AGENT_ERROR"), str(error.get("message") or "Agent error"),
                            **details)

    async def disconnect(self, device_id: str, code: int = CLOSE_UNAUTHORIZED, reason: str = "revoked") -> None:
        connection = self.connections.pop(device_id, None)
        if connection is not None:
            connection.fail_pending()
            await connection.close(code, reason)

    async def retire(self, device_id: str) -> None:
        """Tell a connected agent that its device was deleted: over the encrypted channel, so only this
        hub can make an agent stop for good and remove its autostart."""
        connection = self.connections.pop(device_id, None)
        if connection is not None:
            connection.fail_pending()
            with contextlib.suppress(Exception):
                await connection.send({"t": "deleted"})
            await connection.close(CLOSE_DELETED, "deleted")

    def _vouched(self, websocket: WebSocket) -> bool:
        """Did one of this hub's devices prove itself in the upgrade request (see upgrade_proof)? Anything else, an
        agent from before this included, is a stranger until the challenge-response."""
        try:
            device_id, stamp, nonce, proof = websocket.headers.get(UPGRADE_HEADER, "").split(" ")
            stamp, nonce, proof = int(stamp), b64d(nonce), b64d(proof)
        except ValueError:
            return False
        now = time.time()
        if abs(now - stamp) > UPGRADE_WINDOW or len(nonce) != NONCE_BYTES or len(proof) != 32:
            return False
        row = self.rt.store.one("SELECT secret_enc FROM devices WHERE id=? AND enabled=1", (device_id,))
        if not row or not row["secret_enc"]:
            return False
        secret = b64d(self.rt.store.decrypt(row["secret_enc"]))
        if not hmac.compare_digest(proof, upgrade_proof(secret, device_id, stamp, nonce)):
            return False
        self._used = {taken: until for taken, until in self._used.items() if until > now}
        if proof in self._used:  # replayed
            return False
        self._used[proof] = now + 2 * UPGRADE_WINDOW
        return True

    async def serve(self, websocket: WebSocket) -> None:
        stranger = not self._vouched(websocket)
        if stranger and self.handshakes >= MAX_HANDSHAKES:  # refused before the upgrade: nothing of it is read
            log.info("agent connection refused: %d handshakes already under way", self.handshakes)
            with contextlib.suppress(Exception):
                await websocket.close(code=1013)
            return
        self.handshakes += stranger
        try:
            await websocket.accept()
            device, channel = await self._handshake(websocket)
        except _Rejected as rejected:
            log.info("agent rejected: %s", rejected.message)
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps({"t": "error", "code": rejected.code,
                                                      "message": rejected.message, **rejected.extra}))
                await websocket.close(code=rejected.close_code, reason=rejected.code)
            return
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError, TypeError, RuntimeError) as exc:
            log.info("agent handshake failed: %s: %s", type(exc).__name__, exc)
            with contextlib.suppress(Exception):
                await websocket.close(code=CLOSE_BAD_REQUEST)
            return
        finally:
            self.handshakes -= stranger

        device_id = device["id"]
        connection = AgentConnection(device_id, websocket, channel)
        previous = self.connections.get(device_id)
        self.connections[device_id] = connection
        if previous is not None:
            previous.fail_pending()
            await previous.close(CLOSE_REPLACED, "replaced by a newer connection")
        self.rt.store.execute("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), device_id))
        self.rt.audit(f"设备:{device['name']}", "device.connected", device_id, user_id=device["user_id"])
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                frame = message.get("bytes")
                if frame is None:
                    raise ChannelError("Text frame after the handshake")
                payload = channel.open(frame)
                kind = payload.get("t")
                if kind == "res":
                    connection.resolve(payload)
                elif kind == "status":  # merged, so the setup chosen in the panel survives
                    with self.rt.store.transaction() as db:
                        row = db.execute("SELECT info FROM devices WHERE id=?", (device_id,)).fetchone()
                        info = json.loads(row["info"] or "{}") if row else {}
                        info.update({key: payload.get(key) for key in STATUS_FIELDS})
                        db.execute("UPDATE devices SET info=?, last_seen=? WHERE id=?",
                                   (json.dumps(info, ensure_ascii=False), time.time(), device_id))
        except ChannelError as exc:
            log.warning("closing agent %s: %s", device_id, exc)
            await connection.close(CLOSE_BAD_REQUEST, "protocol error")
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if self.connections.get(device_id) is connection:
                del self.connections[device_id]
                self.rt.audit(f"设备:{device['name']}", "device.disconnected", device_id, user_id=device["user_id"])
            connection.fail_pending()
            self.rt.store.execute("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), device_id))

    async def _handshake(self, websocket: WebSocket):
        hub_nonce = new_nonce()
        await websocket.send_text(json.dumps({"t": "challenge", "v": 1, "nonce": b64e(hub_nonce)}))
        hello = json.loads(await asyncio.wait_for(websocket.receive_text(), HANDSHAKE_SECONDS))
        if not isinstance(hello, dict) or hello.get("t") != "hello" or hello.get("v") != 1:
            raise _Rejected("BAD_HELLO", "Unexpected handshake message", CLOSE_BAD_REQUEST)
        device_id = hello.get("device_id")
        agent_nonce = b64d(str(hello.get("nonce", "")))
        proof = b64d(str(hello.get("proof", "")))
        if not isinstance(device_id, str) or len(agent_nonce) != NONCE_BYTES or len(proof) != 32:
            raise _Rejected("BAD_HELLO", "Malformed hello", CLOSE_BAD_REQUEST)
        device = self.rt.store.one("SELECT d.*, u.disabled AS owner_disabled FROM devices d "
                                   "LEFT JOIN users u ON u.id = d.user_id WHERE d.id=?", (device_id,))
        tomb = None if device else self.rt.store.one("SELECT secret_enc FROM deleted_devices WHERE id=?", (device_id,))
        if tomb is not None:
            # Only a deliberate deletion says so, proven with the old secret (an unknown id, e.g. after
            # restoring an older database, just keeps retrying).
            secret = b64d(self.rt.store.decrypt(tomb["secret_enc"]))
            if hmac.compare_digest(proof, agent_proof(secret, device_id, hub_nonce, agent_nonce)):
                raise _Rejected("DEVICE_DELETED", "This device was deleted in the hub panel", CLOSE_DELETED,
                                proof=b64e(deleted_proof(secret, device_id, hub_nonce, agent_nonce)))
        if device is None or not device["secret_enc"]:
            raise _Rejected("UNKNOWN_DEVICE", "Unknown or unpaired device; pair it again from the hub panel",
                            CLOSE_UNAUTHORIZED)
        secret = b64d(self.rt.store.decrypt(device["secret_enc"]))
        if not hmac.compare_digest(proof, agent_proof(secret, device_id, hub_nonce, agent_nonce)):
            raise _Rejected("AUTH_FAILED", "Device credentials do not match; pair this device again",
                            CLOSE_UNAUTHORIZED)
        if not device["enabled"]:
            raise _Rejected("DEVICE_DISABLED", "This device is disabled in the hub panel", CLOSE_FORBIDDEN)
        if device["owner_disabled"] != 0:  # owner disabled (1) or deleted (None)
            raise _Rejected("USER_DISABLED", "The account that owns this device is disabled", CLOSE_FORBIDDEN)
        await websocket.send_text(json.dumps({"t": "welcome", "device_name": device["name"],
                                              "proof": b64e(hub_proof(secret, device_id, hub_nonce, agent_nonce))}))
        return device, SecureChannel(secret, device_id, hub_nonce, agent_nonce, "hub")
