"""Small crypto helpers shared by the hub and the agent."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Crockford base32 without I/L/O/U: easy to read aloud and type.
_PAIRING_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_token(prefix: str = "", nbytes: int = 32) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def sha256_hex(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def pkce_challenge(verifier: str) -> str:
    return b64e(hashlib.sha256(verifier.encode("ascii")).digest())


def pairing_code() -> str:
    raw = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_pairing_code(code: str) -> str:
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    cleaned = cleaned.replace("O", "0").replace("I", "1").replace("L", "1")
    return f"{cleaned[:4]}-{cleaned[4:]}" if len(cleaned) == 8 else cleaned


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${2**14}$8$1${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                 dklen=len(expected) // 2)
        return hmac.compare_digest(derived.hex(), expected)
    except (ValueError, TypeError):
        return False
