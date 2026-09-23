"""Hub settings, read from WEBCODER_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from webcoder.hub.clients import SUPPORTED_CLIENTS


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    public_url: str = "http://127.0.0.1:8787"   # the URL clients use; OAuth metadata is derived from it
    data_dir: Path = Path("data")
    clients: tuple[str, ...] = ("chatgpt", "codex")
    extra_redirect_uris: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    agent_timeout: float = 45.0
    max_transfer_bytes: int = 16 * 1024 * 1024
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 86400
    session_ttl: int = 7 * 86400

    def __post_init__(self):
        url = self.public_url.strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query or parts.fragment:
            raise ValueError("WEBCODER_PUBLIC_URL must be an origin such as https://hub.example.com (no path)")
        object.__setattr__(self, "public_url", url)
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        unknown = [name for name in self.clients if name not in SUPPORTED_CLIENTS]
        if unknown:
            raise ValueError(f"Unsupported WEBCODER_CLIENTS entries: {', '.join(unknown)} "
                             f"(supported: {', '.join(SUPPORTED_CLIENTS)})")

    @property
    def mcp_url(self) -> str:
        return self.public_url + "/mcp"

    @property
    def issuer(self) -> str:
        return self.public_url

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        env = os.environ
        values: dict = {}
        if env.get("WEBCODER_PUBLIC_URL"):
            values["public_url"] = env["WEBCODER_PUBLIC_URL"]
        if env.get("WEBCODER_DATA_DIR"):
            values["data_dir"] = Path(env["WEBCODER_DATA_DIR"])
        if env.get("WEBCODER_CLIENTS"):
            values["clients"] = _csv(env["WEBCODER_CLIENTS"])
        if env.get("WEBCODER_EXTRA_REDIRECT_URIS"):
            values["extra_redirect_uris"] = _csv(env["WEBCODER_EXTRA_REDIRECT_URIS"])
        if env.get("WEBCODER_ALLOWED_ORIGINS"):
            values["allowed_origins"] = _csv(env["WEBCODER_ALLOWED_ORIGINS"])
        if env.get("WEBCODER_AGENT_TIMEOUT"):
            values["agent_timeout"] = float(env["WEBCODER_AGENT_TIMEOUT"])
        if env.get("WEBCODER_MAX_TRANSFER_MB"):
            values["max_transfer_bytes"] = int(float(env["WEBCODER_MAX_TRANSFER_MB"]) * 1024 * 1024)
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)
