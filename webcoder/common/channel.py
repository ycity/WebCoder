"""Authenticated, encrypted hub <-> agent channel over WebSocket frames.

Handshake (cleartext JSON text frames)::

    hub   -> agent  {"t": "challenge", "v": 1, "nonce": <32 random bytes>}
    agent -> hub    {"t": "hello", "v": 1, "device_id", "nonce": <32 random bytes>,
                     "proof": HMAC(secret, "agent-proof" | device | nonces)}
    hub   -> agent  {"t": "welcome", "proof": HMAC(secret, "hub-proof" | device | nonces)}
                    or {"t": "error", "code", "message"}

The device secret never crosses the wire and both sides prove they know it.
Afterwards every message is a binary frame: 8-byte big-endian sequence number
followed by AES-256-GCM ciphertext. Keys are per connection and per direction
(HKDF over the secret, salted with both nonces). Sequence numbers must grow by
exactly one, which rejects replayed or reordered frames. File contents stay
private even when the hub is reached over plain ws://.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from webcoder.common.crypto import b64e

PROTOCOL_VERSION = 1
NONCE_BYTES = 32
_LABEL = b"webcoder/1"


class ChannelError(Exception):
    """The peer sent something that breaks the channel protocol."""


# ---------------------------------------------------------------- pairing
# The device secret travels once, in the pairing reply: sealed for the agent's one-time key (X25519, HKDF,
# AES-GCM), so someone listening to a pairing over plain HTTP (a hub on a LAN) cannot read it.
def pairing_key() -> tuple[X25519PrivateKey, str]:
    """A one-time key pair: (private key, public key to send with the pairing request)."""
    private = X25519PrivateKey.generate()
    return private, base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def _pairing_cipher(private: X25519PrivateKey, peer: str, device_id: str) -> AESGCM:
    shared = private.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(peer, validate=True)))
    return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                       info=b"webcoder/pairing:" + device_id.encode()).derive(shared))


def seal_secret(agent_key: str, secret: str, device_id: str) -> dict:
    """The hub's side: {"hub_key", "sealed_secret"} for the agent that sent `agent_key`."""
    private = X25519PrivateKey.generate()
    nonce = os.urandom(12)
    sealed = _pairing_cipher(private, agent_key, device_id).encrypt(nonce, secret.encode(), device_id.encode())
    return {"hub_key": base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
            "sealed_secret": base64.b64encode(nonce + sealed).decode()}


def open_secret(private: X25519PrivateKey, reply: dict, device_id: str) -> str:
    """The agent's side: the device secret out of a sealed pairing reply."""
    raw = base64.b64decode(reply["sealed_secret"], validate=True)
    try:
        return _pairing_cipher(private, reply["hub_key"], device_id).decrypt(raw[:12], raw[12:],
                                                                             device_id.encode()).decode()
    except InvalidTag:
        raise ChannelError("The pairing reply was not sealed for this agent") from None


def new_nonce() -> bytes:
    return os.urandom(NONCE_BYTES)


def _proof(secret: bytes, role: bytes, device_id: str, hub_nonce: bytes, agent_nonce: bytes) -> bytes:
    message = b"|".join([_LABEL, role, device_id.encode("utf-8"), hub_nonce, agent_nonce])
    return hmac.new(secret, message, hashlib.sha256).digest()


def agent_proof(secret: bytes, device_id: str, hub_nonce: bytes, agent_nonce: bytes) -> bytes:
    return _proof(secret, b"agent-proof", device_id, hub_nonce, agent_nonce)


def hub_proof(secret: bytes, device_id: str, hub_nonce: bytes, agent_nonce: bytes) -> bytes:
    return _proof(secret, b"hub-proof", device_id, hub_nonce, agent_nonce)


UPGRADE_HEADER = "X-WebCoder-Device"  # "<device id> <unix time> <nonce> <upgrade_proof>" on the WebSocket upgrade


def upgrade_proof(secret: bytes, device_id: str, stamp: int, nonce: bytes) -> bytes:
    """Sent with the WebSocket upgrade request, before any frame: lets the hub tell its own devices from anyone
    else before it accepts a socket (the challenge-response follows as always)."""
    message = b"|".join([_LABEL, b"upgrade", device_id.encode("utf-8"), str(stamp).encode("ascii"), nonce])
    return hmac.new(secret, message, hashlib.sha256).digest()


def upgrade_header(secret: bytes, device_id: str, stamp: int, nonce: bytes) -> str:
    return f"{device_id} {stamp} {b64e(nonce)} {b64e(upgrade_proof(secret, device_id, stamp, nonce))}"


def deleted_proof(secret: bytes, device_id: str, hub_nonce: bytes, agent_nonce: bytes) -> bytes:
    """Proves a "this device was deleted" answer comes from the hub that knew the device secret."""
    return _proof(secret, b"device-deleted", device_id, hub_nonce, agent_nonce)


class SecureChannel:
    """Seal/open JSON messages for one connection. Not thread-safe: callers
    serialize sends (seal + send under one lock) and read from one loop."""

    def __init__(self, secret: bytes, device_id: str, hub_nonce: bytes, agent_nonce: bytes, role: str):
        if role not in ("hub", "agent"):
            raise ValueError("role must be 'hub' or 'agent'")
        if len(secret) < 32 or len(hub_nonce) != NONCE_BYTES or len(agent_nonce) != NONCE_BYTES:
            raise ChannelError("Invalid channel parameters")
        device = device_id.encode("utf-8")
        okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=hub_nonce + agent_nonce,
                   info=_LABEL + b"|session|" + device).derive(secret)
        hub_to_agent, agent_to_hub = AESGCM(okm[:32]), AESGCM(okm[32:])
        if role == "hub":
            self._out, self._in = hub_to_agent, agent_to_hub
            out_label, in_label = b"h2a", b"a2h"
        else:
            self._out, self._in = agent_to_hub, hub_to_agent
            out_label, in_label = b"a2h", b"h2a"
        self._out_aad = b"|".join([_LABEL, out_label, device])
        self._in_aad = b"|".join([_LABEL, in_label, device])
        self._sent = 0
        self._received = 0

    def seal(self, message: dict) -> bytes:
        self._sent += 1
        header = self._sent.to_bytes(8, "big")
        plaintext = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return header + self._out.encrypt(b"\0\0\0\0" + header, plaintext, self._out_aad)

    def open(self, frame: bytes) -> dict:
        if not isinstance(frame, (bytes, bytearray)) or len(frame) < 8 + 16:
            raise ChannelError("Malformed frame")
        header = bytes(frame[:8])
        sequence = int.from_bytes(header, "big")
        if sequence != self._received + 1:
            raise ChannelError("Unexpected sequence number (replayed or reordered frame)")
        try:
            plaintext = self._in.decrypt(b"\0\0\0\0" + header, bytes(frame[8:]), self._in_aad)
        except InvalidTag as exc:
            raise ChannelError("Frame authentication failed") from exc
        self._received = sequence
        try:
            message = json.loads(plaintext)
        except ValueError as exc:
            raise ChannelError("Frame payload is not JSON") from exc
        if not isinstance(message, dict):
            raise ChannelError("Frame payload must be a JSON object")
        return message
