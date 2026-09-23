"""Deleting a device retires its agent (provably, online or later); one agent per folder; autostart cleanup."""
import asyncio
import base64
import json
import os
import stat
import time
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from tests.conftest import AgentThread
from webcoder.agent.__main__ import main as agent_main
from webcoder.agent.client import AgentSession, run_agent
from webcoder.agent.config import AgentConfig
from webcoder.agent.lifecycle import LAUNCHD_LABEL, SYSTEMD_UNIT, acquire_instance_lock, remove_autostart, retire
from webcoder.common.channel import new_nonce
from webcoder.common.crypto import b64e


def assert_retired(runner: AgentThread, config: AgentConfig) -> None:
    runner.thread.join(20)
    assert not runner.thread.is_alive(), "the agent should stop for good, not keep retrying"
    on_disk = json.loads(Path(config.path).read_text(encoding="utf-8"))
    assert on_disk["retired"] is True and on_disk["device_secret"] == ""
    assert runner.removed_autostart == [Path(config.path)]


def test_deleting_an_online_device_retires_its_agent(hub, start_agent, tmp_path):
    device_id, config = start_agent("pc", tmp_path / "work")
    hub.panel.delete(f"/api/devices/{device_id}").raise_for_status()
    assert_retired(start_agent.runners[-1], config)
    # An autostart entry that could not be removed now starts an agent that exits quietly (no restart loop).
    assert agent_main(["--config", str(config.path), "run"]) == 0


def test_an_offline_device_learns_it_was_deleted_when_it_comes_back(hub, start_agent, tmp_path):
    device_id, config = start_agent("pc", tmp_path / "work")
    start_agent.runners[-1].stop()
    hub.wait_for(lambda: not hub.device(device_id)["online"], what="agent offline")
    hub.panel.delete(f"/api/devices/{device_id}").raise_for_status()
    later = AgentThread(AgentConfig.load(config.path))
    later.start()
    assert_retired(later, config)


def test_only_the_real_device_secret_hears_about_the_deletion(hub, start_agent, tmp_path):
    device_id, config = start_agent("pc", tmp_path / "work")
    start_agent.runners[-1].stop()
    hub.panel.delete(f"/api/devices/{device_id}").raise_for_status()
    impostor = AgentConfig(hub_url=config.hub_url, device_id=device_id, device_secret=b64e(os.urandom(32)),
                           proxy="none", path=tmp_path / "impostor" / "config.json")
    impostor.save()
    runner = AgentThread(impostor)
    runner.start()
    time.sleep(3)
    assert runner.thread.is_alive() and runner.removed_autostart == []  # told "unknown device", keeps retrying
    runner.stop()


def test_an_unproven_deletion_notice_is_ignored(tmp_path):
    """A fake hub (or a man in the middle of a plain ws:// connection) cannot make an agent retire."""
    async def fake_hub(ws):
        await ws.send(json.dumps({"t": "challenge", "v": 1, "nonce": b64e(new_nonce())}))
        await ws.recv()
        await ws.send(json.dumps({"t": "error", "code": "DEVICE_DELETED", "message": "gone",
                                  "proof": b64e(os.urandom(32))}))
        await ws.close(4410, "DEVICE_DELETED")

    async def main():
        async with serve(fake_hub, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            config = AgentConfig(hub_url=f"http://127.0.0.1:{port}", device_id="dev_x",
                                 device_secret=b64e(os.urandom(32)), proxy="none",
                                 path=tmp_path / "agent" / "config.json")
            config.save()
            retired, stop = [], asyncio.Event()
            task = asyncio.create_task(run_agent(config, stop, on_deleted=retired.append))
            await asyncio.sleep(2)
            assert not task.done() and retired == []
            stop.set()
            await asyncio.wait_for(task, 10)

    asyncio.run(main())


def test_a_device_revoked_while_away_stops_what_it_left_running(tmp_path, monkeypatch):
    """The connection broke while a command ran; meanwhile the device was disabled. When the hub turns it away on
    the next handshake, the command is stopped then, not left to run to its own timeout."""
    from webcoder.agent import client as client_module

    stops = []
    monkeypatch.setattr(client_module, "stop_all", lambda: stops.append(1) or 0)

    async def rejecting_hub(ws):
        await ws.send(json.dumps({"t": "challenge", "v": 1, "nonce": b64e(new_nonce())}))
        await ws.recv()
        await ws.send(json.dumps({"t": "error", "code": "DEVICE_DISABLED", "message": "disabled in the panel"}))
        await ws.close(4403, "DEVICE_DISABLED")

    async def main():
        async with serve(rejecting_hub, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            config = AgentConfig(hub_url=f"http://127.0.0.1:{port}", device_id="dev_x",
                                 device_secret=b64e(os.urandom(32)), proxy="none",
                                 path=tmp_path / "agent" / "config.json")
            config.save()
            stop = asyncio.Event()
            task = asyncio.create_task(run_agent(config, stop))
            for _ in range(100):
                if stops:
                    break
                await asyncio.sleep(0.05)
            stop.set()
            await asyncio.wait_for(task, 10)

    asyncio.run(main())
    assert stops


def test_the_agent_turns_work_away_beyond_its_queue():
    """Requests wait for a slot with their data (a file's content ...): past the queue, they are refused at once
    instead of piling up in memory."""
    sent = []

    class Channel:  # frames as plain JSON for the test
        def seal(self, message):
            return json.dumps(message).encode()

        def open(self, frame):
            return json.loads(frame)

    class Ops:
        def info(self):
            return {}

        def dispatch(self, op, args):
            time.sleep(0.3)
            return {"done": True}

    class Socket:
        def __aiter__(self):
            async def frames():
                for n in range(5):
                    yield json.dumps({"t": "req", "id": n, "op": "fs.stat", "args": {}}).encode()
                await asyncio.sleep(1.5)  # the connection stays while the accepted work finishes
            return frames()

        async def send(self, data):
            sent.append(json.loads(data))

    asyncio.run(AgentSession(Socket(), Channel(), None, Ops(), concurrency=1, queue=2).serve())
    replies = {message["id"]: message for message in sent if message.get("t") == "res"}
    assert [replies[n]["ok"] for n in range(5)] == [True, True, False, False, False]
    assert replies[4]["error"]["code"] == "AGENT_BUSY"


def test_retire_leaves_a_newer_pairing_alone(tmp_path):
    path = tmp_path / "config.json"
    AgentConfig(hub_url="http://hub", device_id="dev_new", device_secret="new-secret", path=path).save()
    old = AgentConfig(hub_url="http://hub", device_id="dev_old", device_secret="old-secret", path=path)
    removed = []
    assert retire(old, remove=removed.append) is False
    assert json.loads(path.read_text(encoding="utf-8"))["device_secret"] == "new-secret" and removed == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_retiring_keeps_the_config_private(tmp_path):
    path = tmp_path / "config.json"  # in a folder others may enter
    config = AgentConfig(hub_url="http://hub", device_id="dev_x", device_secret="c2VjcmV0",
                         proxy="http://me:pass@proxy:3128", path=path)
    config.save()
    old = os.umask(0o022)
    try:
        assert retire(config, remove=[].append) is True
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and json.loads(path.read_text())["retired"] is True


def test_remove_autostart_only_touches_the_entry_for_this_config(tmp_path):
    calls = []

    class Result:
        def __init__(self, stdout=b""):
            self.stdout = stdout

    def fake_run(args, **kwargs):
        if args[0] == "powershell.exe":
            script = base64.b64decode(args[-1]).decode("utf-16-le")
            calls.append(script)
            if "Get-ScheduledTask" in script:
                arguments = '-m webcoder.agent --config "C:\\Users\\Me\\.webcoder-agent\\config.json" run'
                return Result(base64.b64encode(arguments.encode("utf-8")))
            return Result()
        calls.append(args)
        return Result()

    assert remove_autostart("D:\\other\\config.json", platform="win32", run=fake_run) is None
    assert not any("Unregister" in str(call) for call in calls)
    assert remove_autostart("c:\\users\\me\\.webcoder-agent\\config.json", platform="win32", run=fake_run)
    assert "Unregister-ScheduledTask -TaskName 'WebCoder Agent'" in calls[-1]

    unit = tmp_path / ".config" / "systemd" / "user" / SYSTEMD_UNIT
    unit.parent.mkdir(parents=True)
    unit.write_text('[Service]\nExecStart="/x/python" -m webcoder.agent '
                    '--config "/home/me/.webcoder-agent/config.json" run\n', encoding="utf-8")
    calls.clear()
    assert remove_autostart("/elsewhere/config.json", platform="linux", home=tmp_path, run=fake_run) is None
    assert unit.exists() and calls == []
    assert remove_autostart("/home/me/.webcoder-agent/config.json", platform="linux", home=tmp_path,
                            run=fake_run) == str(unit)
    assert not unit.exists()
    assert calls[0][:3] == ["systemctl", "--user", "disable"] and calls[-1][:3] == ["systemctl", "--user", "stop"]

    plist = tmp_path / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<string>--config</string><string>/Users/me/.webcoder-agent/config.json</string>",
                     encoding="utf-8")
    calls.clear()
    assert remove_autostart("/Users/me/.webcoder-agent/config.json", platform="darwin", home=tmp_path,
                            run=fake_run) == str(plist)
    assert not plist.exists() and calls[0][:2] == ["launchctl", "bootout"]


def test_one_agent_per_folder(tmp_path):
    first = acquire_instance_lock(tmp_path / "agent")
    assert first is not None and acquire_instance_lock(tmp_path / "agent") is None
    config = AgentConfig(hub_url="http://127.0.0.1:1", device_id="dev_x", device_secret=b64e(os.urandom(32)),
                         proxy="none", path=tmp_path / "agent" / "config.json")
    config.save()
    assert agent_main(["--config", str(config.path), "run"]) == 0  # returns at once: the folder is in use
    first.close()
    again = acquire_instance_lock(tmp_path / "agent")
    assert again is not None
    again.close()
