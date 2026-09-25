"""The hub's /api/cli endpoints, each request signed with this computer's device secret (standard library only)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from webcoder import VERSION
from webcoder.common.channel import SIGNATURE_HEADER, request_signature
from webcoder.common.crypto import b64d


class HubError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class HubClient:
    def __init__(self, hub_url: str, device_id: str, device_secret: str, proxy: str = "system"):
        self.hub = hub_url.strip().rstrip("/")
        self.device_id = device_id
        self.secret = b64d(device_secret)
        handlers = [] if proxy == "system" else [urllib.request.ProxyHandler(
            {} if proxy in ("none", "") else {"http": proxy, "https": proxy})]
        self.opener = urllib.request.build_opener(*handlers)

    @classmethod
    def from_config(cls, config) -> HubClient:
        return cls(config.hub_url, config.device_id, config.device_secret, config.proxy or "system")

    def post(self, path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": f"webcoder/{VERSION}",
                   SIGNATURE_HEADER: request_signature(self.secret, self.device_id, "POST", path, body)}
        request = urllib.request.Request(self.hub + path, data=body, method="POST", headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            try:
                error = json.loads(text)["error"]
                raise HubError(str(error.get("code")), str(error.get("message")), exc.code) from None
            except (ValueError, KeyError, TypeError):
                raise HubError(f"HTTP_{exc.code}", text[:300] or exc.reason, exc.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise HubError("HUB_UNREACHABLE", f"Cannot reach the hub at {self.hub}: {reason}") from None
