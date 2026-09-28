"""`webcoder dot`: the doorbell room through which the command line mentions a ChatGPT dot chat.

dot is played over MCP with a PAT: it joins with the WAJ code from `webcoder dot setup`, subscribes to mentions
(MCP Events, callbacks answered in-process), reads the request and replies in the room.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import uuid

import pytest

from tests.conftest import Hub
from webcoder.agent.__main__ import pair_device
from webcoder.cli import __main__ as cli
from webcoder.cli.client import HubClient, HubError
from webcoder.hub.collaboration.common import MESSAGE_EVENT
from webcoder.hub.collaboration.network import Reply

WAJ = re.compile(r"WAJ-(?:[0-9A-F]{4}-){3}[0-9A-F]{4}")
MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {}}
SECRET = "whsec_" + base64.b64encode(b"fixture-only-signing-material-32b!").decode()
CALLBACK = "https://example.invalid/dot-callback"


def key() -> str:
    return uuid.uuid4().hex


class Host:
    """dot's host: answers subscription challenges and keeps every event delivered to it."""

    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, url, body, headers):
        payload = json.loads(body)
        if payload.get("type") == "verification":
            return Reply(200, json.dumps({"challenge": payload["challenge"]}).encode())
        self.events.append(payload)
        return Reply(204)

    def mentions(self) -> list[dict]:
        return [event["data"] for event in self.events if event["name"] == MESSAGE_EVENT
                and not event["data"].get("test")]


@pytest.fixture
def hub(tmp_path):
    instance = Hub(tmp_path / "hub-data", collaboration_enabled=True, mcp_events_enabled=True)
    instance.host = Host()
    instance.runtime.collaboration.events.sender = instance.host
    yield instance
    instance.stop()


@pytest.fixture
def device(hub, start_agent, tmp_path, monkeypatch):
    monkeypatch.setenv("WEBCODER_HOME", str(tmp_path / "webcoder-home"))
    return start_agent("laptop", tmp_path / "code")


def client_for(config) -> HubClient:
    return HubClient(config.hub_url, config.device_id, config.device_secret, proxy="none")


def dot_cli(config, *words) -> int:
    return cli.main(["--agent-config", str(config.path), "dot", *words])


def mcp(hub, token, tool, **arguments) -> dict:
    response = hub.rpc(token, "tools/call", {"name": tool, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def subscribe(hub, token, request) -> None:
    """The host's native subscription (the 2026 protocol's events/subscribe)."""
    params = {**request, "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET}, "_meta": META}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}", "MCP-Protocol-Version": MODERN, "Mcp-Method": "events/subscribe",
               "Mcp-Name": request["name"]}
    response = hub.http.post("/mcp", headers=headers,
                             json={"jsonrpc": "2.0", "id": 7, "method": "events/subscribe", "params": params})
    assert response.status_code == 200 and "result" in response.json(), response.text


def setup_code(config, capsys, *flags) -> str:
    assert dot_cli(config, "setup", *flags) == 0
    return WAJ.search(capsys.readouterr().out).group(0)


def test_dot_doorbell_from_setup_to_a_reply(hub, device, tmp_path, monkeypatch, capsys):
    _, config = device
    client = client_for(config)
    assert dot_cli(config, "hi") == 1
    assert "DOORBELL_MISSING" in capsys.readouterr().err

    assert dot_cli(config, "setup") == 0
    out = capsys.readouterr().out
    code = WAJ.search(out).group(0)
    assert "等 dot 用加入码登记" in out and MESSAGE_EVENT in out and "collaboration_join" in out
    assert "测试事件（data.test 为 true）没有真实消息" in out
    doorbell = client.post("/api/cli/dot/status")
    assert doorbell["project"]["root"] == str(tmp_path / "webcoder-home" / "dot-doorbell")
    assert doorbell["subscription"]["arguments"]["conversation_id"] == doorbell["room_id"]
    assert dot_cli(config, "hi") == 1
    assert "DOT_NOT_JOINED" in capsys.readouterr().err

    dot = hub.create_token(scopes=("read", "write"), projects="*", label="dot 聊天")
    mcp(hub, dot, "collaboration_join", code=code, idempotency_key=key())
    assert dot_cli(config, "hi") == 1
    assert "DOT_NOT_SUBSCRIBED" in capsys.readouterr().err
    subscribe(hub, dot, doorbell["subscription"])
    assert dot_cli(config, "status") == 0
    out = capsys.readouterr().out
    assert "提醒订阅 已订阅" in out and "还没允许" in out and "全部项目 · 可读写" in out
    assert dot_cli(config, "setup") == 0  # once dot has joined, setup lets it speak in the room
    assert "已允许 dot 回复" in capsys.readouterr().out

    def answer_test():
        def delivered():
            return [event for event in hub.host.events if event["data"].get("test")]
        hub.wait_for(delivered, timeout=30, what="the test event delivered to dot")
        event = delivered()[0]
        mcp(hub, dot, "collaboration_message_create", project=event["data"]["project_id"],
            environment_id=event["data"]["environment_id"], room_id=event["data"]["room_id"],
            body_text=f"收到测试事件 {event['eventId']}", client_message_id=key(), idempotency_key=key())

    dot_side = threading.Thread(target=answer_test, daemon=True)
    dot_side.start()
    assert dot_cli(config, "test") == 0
    dot_side.join(10)
    out = capsys.readouterr().out
    assert "已被宿主接收" in out and "✓ dot 回复了：收到测试事件" in out
    assert dot_cli(config, "status") == 0
    assert "dot 已回复：收到测试事件" in capsys.readouterr().out

    folder = tmp_path / "code" / "repo"
    (folder / ".git").mkdir(parents=True)
    (folder / "app.py").write_text("print('hi')\n", encoding="utf-8")
    monkeypatch.chdir(folder)
    requests = []

    def answer():
        hub.wait_for(lambda: hub.host.mentions(), timeout=30, what="the mention delivered to dot")
        data = hub.host.mentions()[0]
        scope = {"project": data["project_id"], "environment_id": data["environment_id"]}
        thread = mcp(hub, dot, "collaboration_read", **scope, kind="thread", id=data["message_id"])
        requests.append(thread["items"][0]["body_text"])
        mcp(hub, dot, "collaboration_message_create", **scope, room_id=data["room_id"],
            body_text="看过了：app.py 没问题", client_message_id=key(), idempotency_key=key(),
            reply_to_id=data["message_id"])

    dot_side = threading.Thread(target=answer, daemon=True)
    dot_side.start()
    assert dot_cli(config, "看看", "app.py") == 0
    dot_side.join(10)
    out = capsys.readouterr()
    assert out.out == "看过了：app.py 没问题\n" and "dot 已回复" in out.err and "项目 repo" in out.err
    project = next(p for p in client.post("/api/cli/status")["projects"] if p["alias"] == "repo")
    assert f'project="{project["id"]}"' in requests[0] and str(folder) in requests[0]
    assert requests[0].endswith("请求：\n看看 app.py")
    assert client.post("/api/cli/dot/status")["speech"]["enabled"]
    with pytest.raises(HubError) as missing:
        client.post("/api/cli/dot/messages/" + key())
    assert missing.value.code == "NOT_FOUND"


def test_dot_setup_keeps_one_slot_and_replaces_stale_ones(hub, device, capsys):
    _, config = device
    client = client_for(config)
    store = hub.runtime.store
    first = setup_code(config, capsys)
    slot = client.post("/api/cli/dot/status")["slot"]["id"]
    assert setup_code(config, capsys) == first  # running it again shows the same invitation

    store.execute("UPDATE collaboration_join_slots SET code_expires_at=0 WHERE id=?", (slot,))
    renewed = setup_code(config, capsys)
    assert renewed != first and client.post("/api/cli/dot/status")["slot"]["id"] == slot

    replaced = setup_code(config, capsys, "--new")
    assert replaced != renewed and client.post("/api/cli/dot/status")["slot"]["id"] != slot
    assert store.one("SELECT state FROM collaboration_join_slots WHERE id=?", (slot,))["state"] == "revoked"

    current = client.post("/api/cli/dot/status")["slot"]["id"]
    store.execute("UPDATE collaboration_join_slots SET expires_at=0 WHERE id=?", (current,))
    assert dot_cli(config, "status") == 0
    assert "已到期" in capsys.readouterr().out
    assert dot_cli(config, "hi") == 1
    assert "DOORBELL_EXPIRED" in capsys.readouterr().err
    setup_code(config, capsys)
    assert client.post("/api/cli/dot/status")["slot"]["id"] not in (slot, current)


def test_dot_is_warned_about_projects_outside_its_authorization(hub, device, tmp_path, monkeypatch, capsys):
    _, config = device
    code = setup_code(config, capsys)
    doorbell = client_for(config).post("/api/cli/dot/status")
    dot = hub.create_token(scopes=("read",), projects=[doorbell["project"]["id"]], label="dot 聊天")
    mcp(hub, dot, "collaboration_join", code=code, idempotency_key=key())
    subscribe(hub, dot, doorbell["subscription"])
    folder = tmp_path / "code" / "other"
    folder.mkdir(parents=True)
    monkeypatch.chdir(folder)
    assert dot_cli(config, "--no-wait", "看看") == 0
    err = capsys.readouterr().err
    assert "does not include the project other" in err and "已在门铃讨论室 @dot" in err
    hub.wait_for(lambda: hub.host.mentions(), timeout=30, what="the mention delivered to dot")


def test_dot_needs_the_collaboration_center(tmp_path):
    plain = Hub(tmp_path / "plain-hub")
    try:
        device = plain.add_device("laptop")
        config = pair_device(plain.url, device["code"], [str(tmp_path)], tmp_path / "agent" / "config.json",
                             proxy="none")
        with pytest.raises(HubError) as refused:
            client_for(config).post("/api/cli/dot/status")
        assert refused.value.code == "COLLABORATION_DISABLED"
    finally:
        plain.stop()
