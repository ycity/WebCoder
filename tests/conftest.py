"""Fixtures: a real hub (uvicorn in a thread) and real agents connected over WebSocket."""
from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webcoder.agent.__main__ import pair_device  # noqa: E402
from webcoder.agent.client import run_agent  # noqa: E402
from webcoder.agent.config import AgentConfig  # noqa: E402
from webcoder.agent.lifecycle import retire  # noqa: E402
from webcoder.hub.app import WS_MAX_QUEUE, create_app, ws_frame_limit  # noqa: E402
from webcoder.hub.config import Settings  # noqa: E402

ADMIN_PASSWORD = "correct-horse-battery"
SHA_RE = re.compile(r"sha256=([0-9a-f]{64})")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sha_of(text: str) -> str:
    match = SHA_RE.search(text)
    assert match, f"no sha256 in: {text}"
    return match.group(1)


def posix_shell() -> str:
    """A real POSIX shell (Git Bash on Windows) for the scripts the agent runs on servers and phones."""
    exe = shutil.which("bash") or shutil.which("sh")
    if not exe or Path(exe).parent.name.lower() == "system32":  # the WSL launcher, not a usable shell
        pytest.skip("no POSIX shell")
    return exe


class Hub:
    def __init__(self, data_dir: Path):
        import uvicorn

        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        settings = Settings(public_url=self.url, data_dir=data_dir)
        self.app = create_app(settings)
        self.runtime = self.app.state.runtime
        self.runtime.auth.create_admin("admin", ADMIN_PASSWORD)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    ws_max_size=ws_frame_limit(settings),
                                                    ws_max_queue=WS_MAX_QUEUE))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("hub did not start")
            time.sleep(0.05)
        self.http = httpx.Client(base_url=self.url, trust_env=False, timeout=60)
        self.panel = httpx.Client(base_url=self.url, trust_env=False, timeout=60)
        login = self.panel.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        login.raise_for_status()
        self.panel.headers["X-CSRF-Token"] = login.json()["csrf"]

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.http.close()
        self.panel.close()

    # ------------------------------------------------------------- panel helpers
    def add_device(self, name: str) -> dict:
        response = self.panel.post("/api/devices", json={"name": name})
        response.raise_for_status()
        return response.json()

    def device(self, device_id: str) -> dict:
        return next(d for d in self.panel.get("/api/devices").json()["devices"] if d["id"] == device_id)

    def wait_for(self, predicate, timeout: float = 15.0, what: str = "condition") -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    def add_project(self, alias: str, device_id: str, root, mode: str = "rw") -> dict:
        response = self.panel.post("/api/projects", json={"alias": alias, "device_id": device_id, "root": str(root),
                                                          "mode": mode})
        assert response.status_code == 200, response.text
        return response.json()

    def create_token(self, scopes=("read", "write"), projects="*", label="test") -> str:
        response = self.panel.post("/api/tokens", json={"label": label, "scopes": list(scopes),
                                                        "projects": projects, "days": 30})
        assert response.status_code == 200, response.text
        return response.json()["token"]

    # ------------------------------------------------------------- MCP helpers
    def rpc(self, token: str | None, method: str, params: dict | None = None, *, request_id=1,
            headers: dict | None = None) -> httpx.Response:
        sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token:
            sent["Authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        return self.http.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": method,
                                            "params": params or {}}, headers=sent)

    def call(self, token: str, tool: str, **arguments) -> tuple[str, bool]:
        response = self.rpc(token, "tools/call", {"name": tool, "arguments": arguments})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return result["content"][0]["text"], bool(result.get("isError"))

    def ok(self, token: str, tool: str, **arguments) -> str:
        text, is_error = self.call(token, tool, **arguments)
        assert not is_error, text
        return text


class AgentThread:
    """Runs one agent (its own event loop) in a background thread."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.removed_autostart: list = []  # what retiring would remove; tests never touch the real system

    def _run(self) -> None:
        async def main():
            self.loop = asyncio.get_running_loop()
            self.stop_event = asyncio.Event()
            self.ready.set()
            await run_agent(self.config, self.stop_event,
                            on_deleted=lambda config: retire(config, remove=self.removed_autostart.append))

        asyncio.run(main())

    def start(self) -> None:
        self.thread.start()
        self.ready.wait(5)

    def stop(self) -> None:
        if self.thread.is_alive() and self.loop is not None and self.stop_event is not None:
            with contextlib.suppress(RuntimeError):  # the loop may finish on its own meanwhile (agent retired)
                self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(15)


@pytest.fixture
def hub(tmp_path):
    instance = Hub(tmp_path / "hub-data")
    yield instance
    instance.stop()


@pytest.fixture
def start_agent(hub, tmp_path):
    runners: list[AgentThread] = []

    def start(name: str, root: Path, *, read_only: bool = False) -> tuple[str, AgentConfig]:
        root.mkdir(parents=True, exist_ok=True)
        device = hub.add_device(name)
        config = pair_device(hub.url, device["code"], [str(root)], tmp_path / f"agent-{name}" / "config.json",
                             proxy="none")
        if read_only:
            config.read_only = True
            config.save()
        runner = AgentThread(config)
        runner.start()
        runners.append(runner)
        hub.wait_for(lambda: hub.device(device["device_id"])["online"], what=f"agent {name} online")
        return device["device_id"], config

    start.runners = runners
    yield start
    for runner in runners:
        runner.stop()
