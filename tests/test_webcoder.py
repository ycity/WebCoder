"""webcoder: the command line (device-signed /api/cli), the browser extension's socket and the task relay.

A real hub and a real agent; the browser extension is played by a WebSocket client that answers like it."""
from __future__ import annotations

import io
import json
import os
import signal
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from webcoder.agent.__main__ import pair_device
from webcoder.agent.config import AgentConfig
from webcoder.cli import __main__ as cli
from webcoder.cli.client import HubClient, HubError
from webcoder.common.channel import SIGNATURE_HEADER, request_signature
from webcoder.common.crypto import b64d
from webcoder.hub.browser import PROTOCOL, alias_for, clean_event

EXTENSION = Path(__file__).resolve().parents[1] / "webcoder" / "hub" / "extension"


def client_for(config) -> HubClient:
    return HubClient(config.hub_url, config.device_id, config.device_secret, proxy="none")


class FakeBrowser:
    """Speaks the extension's side of /browser/ws."""

    def __init__(self, hub_url: str, credential: str, origin: str | None = "chrome-extension://abcdefgh"):
        url = hub_url.replace("http://", "ws://") + "/browser/ws"
        self.ws = connect(url, subprotocols=[PROTOCOL, credential], origin=origin, proxy=None, open_timeout=10)
        self.acks: list[str] = []

    def recv(self, timeout: float = 10) -> dict:
        """The next message from the hub; acknowledgements of final events are noted in self.acks."""
        while True:
            message = json.loads(self.ws.recv(timeout=timeout))
            if message.get("t") != "ack":
                return message
            self.acks.append(message["job"])

    def send(self, message: dict) -> None:
        self.ws.send(json.dumps(message, ensure_ascii=False))

    def event(self, job_id: str, **event) -> None:
        self.send({"t": "event", "job": job_id, "event": event})

    def close(self) -> None:
        self.ws.close()


def link_browser(hub, config, name: str = "Edge · Windows", user: str = "admin") -> tuple[FakeBrowser, str]:
    code = client_for(config).post("/api/cli/browser-code")["code"]
    pairing = FakeBrowser(hub.url, "pair." + code)
    paired = pairing.recv()
    assert paired["t"] == "paired" and paired["token"].startswith("wcb_")
    browser = FakeBrowser(hub.url, "token." + paired["token"])
    welcome = browser.recv()
    assert welcome["t"] == "welcome" and welcome["user"] == user
    browser.send({"t": "hello", "name": name, "version": "0.1.0", "running": []})
    browser.send({"t": "ping"})
    assert browser.recv()["t"] == "pong"  # the hello was handled before it
    return browser, paired["token"]


@pytest.fixture
def device(hub, start_agent, tmp_path):
    device_id, config = start_agent("laptop", tmp_path / "code")
    return device_id, config


# --------------------------------------------------------------------------- signatures
def test_cli_requests_must_be_signed_by_a_device(hub, device):
    device_id, config = device
    status = client_for(config).post("/api/cli/status")
    assert status["user"] == "admin" and status["device"]["id"] == device_id and status["device"]["online"]

    unsigned = hub.http.post("/api/cli/status", json={})
    assert unsigned.status_code == 401 and unsigned.json()["error"]["code"] == "SIGNATURE_REQUIRED"

    secret = b64d(config.device_secret)
    body = b"{}"
    signature = request_signature(secret, device_id, "POST", "/api/cli/status", body)
    headers = {SIGNATURE_HEADER: signature, "Content-Type": "application/json"}
    assert hub.http.post("/api/cli/status", content=body, headers=headers).status_code == 200
    replay = hub.http.post("/api/cli/status", content=body, headers=headers)
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "REPLAYED"

    signed = request_signature(secret, device_id, "POST", "/api/cli/status", body)
    tampered = hub.http.post("/api/cli/status", content=b'{"x":1}', headers={SIGNATURE_HEADER: signed})
    assert tampered.json()["error"]["code"] == "BAD_SIGNATURE"
    elsewhere = hub.http.post("/api/cli/browser-code", content=body,
                              headers={SIGNATURE_HEADER: request_signature(secret, device_id, "POST",
                                                                           "/api/cli/status", body)})
    assert elsewhere.json()["error"]["code"] == "BAD_SIGNATURE"
    stale = request_signature(secret, device_id, "POST", "/api/cli/status", body, stamp=int(time.time()) - 3600)
    assert hub.http.post("/api/cli/status", content=body,
                         headers={SIGNATURE_HEADER: stale}).json()["error"]["code"] == "SIGNATURE_EXPIRED"
    wrong = request_signature(b"x" * 32, device_id, "POST", "/api/cli/status", body)
    assert hub.http.post("/api/cli/status", content=body,
                         headers={SIGNATURE_HEADER: wrong}).json()["error"]["code"] == "BAD_SIGNATURE"

    hub.panel.patch(f"/api/devices/{device_id}", json={"enabled": False}).raise_for_status()
    with pytest.raises(HubError) as refused:
        client_for(config).post("/api/cli/status")
    assert refused.value.code == "DEVICE_DISABLED" and refused.value.status == 403


# --------------------------------------------------------------------------- projects
def test_cli_makes_a_folder_a_project_once(hub, device, tmp_path):
    device_id, config = device
    client = client_for(config)
    folder = tmp_path / "code" / "My App"
    folder.mkdir(parents=True)
    first = client.post("/api/cli/project", {"root": str(folder)})
    assert first["created"] and first["alias"] == "My-App" and first["mode"] == "rw"
    assert any("No ChatGPT authorization" in warning for warning in first["warnings"])
    again = client.post("/api/cli/project", {"root": str(folder) + "/"})
    assert not again["created"] and again["id"] == first["id"]

    other = tmp_path / "code" / "nested" / "My App"
    other.mkdir(parents=True)
    second = client.post("/api/cli/project", {"root": str(other)})
    assert second["created"] and second["alias"] == "My-App-2"
    named = tmp_path / "code" / "中文目录"
    named.mkdir()
    assert client.post("/api/cli/project", {"root": str(named)})["alias"] == "project"
    with pytest.raises(HubError) as taken:
        client.post("/api/cli/project", {"root": str(tmp_path / "code"), "alias": "my-app"})
    assert taken.value.code == "ALIAS_TAKEN"

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(HubError) as refused:
        client.post("/api/cli/project", {"root": str(outside)})
    assert refused.value.code == "ROOT_NOT_ALLOWED"

    projects = {row["alias"]: row for row in hub.panel.get("/api/projects").json()["projects"]}
    assert projects["My-App"]["device_id"] == device_id
    token = hub.create_token(projects=[projects["project"]["id"]])
    assert token
    limited = client.post("/api/cli/project", {"root": str(folder)})
    assert any("include this project" in warning for warning in limited["warnings"])
    hub.create_token(projects="*")
    assert client.post("/api/cli/project", {"root": str(folder)})["warnings"] == []


# --------------------------------------------------------------------------- the extension's socket
def test_browser_links_with_a_one_time_code(hub, device):
    _, config = device
    browser, token = link_browser(hub, config)
    browsers = client_for(config).post("/api/cli/status")["browsers"]
    assert [(row["name"], row["online"], row["version"]) for row in browsers] == [("Edge · Windows", True, "0.1.0")]

    code = client_for(config).post("/api/cli/browser-code")["code"]
    FakeBrowser(hub.url, "pair." + code).recv()
    reused = FakeBrowser(hub.url, "pair." + code)
    with pytest.raises(ConnectionClosed) as closed:
        reused.recv()
    assert closed.value.rcvd.code == 4401

    with pytest.raises(ConnectionClosed) as unknown:
        FakeBrowser(hub.url, "token.wcb_nope").recv()
    assert unknown.value.rcvd.code == 4401
    with pytest.raises(InvalidStatus):  # a web page cannot even open it
        FakeBrowser(hub.url, "token." + token, origin="https://evil.example")
    with pytest.raises(InvalidStatus):
        connect(hub.url.replace("http://", "ws://") + "/browser/ws", proxy=None)

    client_for(config).post("/api/cli/browsers/revoke", {"id": browsers[0]["id"]})
    with pytest.raises(ConnectionClosed):
        browser.recv()
    with pytest.raises(ConnectionClosed) as revoked:
        FakeBrowser(hub.url, "token." + token).recv()
    assert revoked.value.rcvd.code == 4401


def test_a_task_goes_to_the_browser_and_its_events_back(hub, device, tmp_path):
    _, config = device
    client = client_for(config)
    folder = tmp_path / "code" / "site"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    project = client.post("/api/cli/project", {"root": str(folder)})

    with pytest.raises(HubError) as offline:
        client.post("/api/cli/jobs", {"project_id": project["id"], "prompt": "fix it"})
    assert offline.value.code == "BROWSER_OFFLINE"

    browser, _ = link_browser(hub, config)
    submitted = client.post("/api/cli/jobs", {"project_id": project["id"], "prompt": "fix it", "model": "gpt-5",
                                              "app": "WebCoder"})
    job_id = submitted["job_id"]
    assert submitted["browser"] == "Edge · Windows" and submitted["project"]["alias"] == "site"
    job = browser.recv()
    assert job["t"] == "job" and job["job"]["id"] == job_id
    assert job["job"]["prompt"] == "fix it" and job["job"]["model"] == "gpt-5" and job["job"]["auto_approve"] is True
    assert job["job"]["conversation"] is None and job["job"]["project"] == "site"

    browser.event(job_id, type="status", text="已发送")
    browser.event(job_id, type="conversation", url="https://chatgpt.com/c/abc-123", title="t")
    browser.event(job_id, type="delta", text="Reading ")
    token = hub.create_token()
    hub.ok(token, "read_file", project="site", path="src/app.py")
    hub.ok(token, "list_projects")  # no project: not shown
    browser.event(job_id, type="approval", tool="edit_file", text="Confirm", decision="auto")
    browser.event(job_id, type="nonsense", text="dropped")
    browser.event(job_id, type="conversation", url="https://evil.example/c/1")

    seen, after = [], 0
    while not any(event["type"] == "approval" for event in seen):
        reply = client.post(f"/api/cli/jobs/{job_id}/events", {"after": after, "wait": 5})
        seen += reply["events"]
        after = seen[-1]["seq"] if seen else 0
    kinds = [event["type"] for event in seen]
    assert kinds[:4] == ["browser", "status", "conversation", "delta"]
    tool = next(event for event in seen if event["type"] == "tool")
    assert tool["name"] == "read_file" and tool["target"] == "site:src/app.py" and tool["status"] == "ok"
    assert "nonsense" not in kinds

    browser.event(job_id, type="done", text="Reading done", url="https://chatgpt.com/c/abc-123")
    final = client.post(f"/api/cli/jobs/{job_id}/events", {"after": after, "wait": 5})
    assert final["state"] == "done" and final["conversation"] == "https://chatgpt.com/c/abc-123"
    assert [event["type"] for event in final["events"]][-1] == "done"
    browser.event(job_id, type="delta", text="late")  # after the end: ignored
    assert client.post(f"/api/cli/jobs/{job_id}/events", {"after": final["events"][-1]["seq"], "wait": 0.2})[
        "events"] == []
    browser.event(job_id, type="done", text="again")  # sent again (the ack got lost): acknowledged again
    browser.send({"t": "ping"})
    assert browser.recv()["t"] == "pong" and browser.acks == [job_id, job_id]

    # The same conversation cannot run two tasks at once; another can.
    first = client.post("/api/cli/jobs", {"project_id": project["id"], "prompt": "a",
                                          "conversation": "https://chatgpt.com/c/abc-123"})["job_id"]
    assert browser.recv()["job"]["id"] == first
    with pytest.raises(HubError) as busy:
        client.post("/api/cli/jobs", {"project_id": project["id"], "prompt": "b",
                                      "conversation": "https://chatgpt.com/c/abc-123"})
    assert busy.value.code == "CONVERSATION_BUSY"
    with pytest.raises(HubError) as invalid:
        client.post("/api/cli/jobs", {"project_id": project["id"], "prompt": "b",
                                      "conversation": "https://evil.example/c/1"})
    assert invalid.value.status == 400

    client.post(f"/api/cli/jobs/{first}/cancel")
    assert browser.recv() == {"t": "cancel", "job": first}
    cancelled = client.post(f"/api/cli/jobs/{first}/events", {"after": 0, "wait": 0})
    assert cancelled["state"] == "cancelled"


def test_a_task_ends_when_its_browser_restarts(hub, device, tmp_path):
    _, config = device
    client = client_for(config)
    folder = tmp_path / "code" / "svc"
    folder.mkdir(parents=True)
    project = client.post("/api/cli/project", {"root": str(folder)})
    browser, token = link_browser(hub, config)
    job_id = client.post("/api/cli/jobs", {"project_id": project["id"], "prompt": "x"})["job_id"]
    browser.recv()
    browser.close()
    events = client.post(f"/api/cli/jobs/{job_id}/events", {"after": 1, "wait": 5})["events"]
    assert events[0]["type"] == "warning"  # waits for it to come back

    again = FakeBrowser(hub.url, "token." + token)
    again.recv()
    again.send({"t": "hello", "name": "Edge", "running": []})  # it does not know the task any more
    deadline = time.time() + 5
    while time.time() < deadline:
        reply = client.post(f"/api/cli/jobs/{job_id}/events", {"after": 0, "wait": 1})
        if reply["state"] != "running":
            break
    assert reply["state"] == "error" and reply["events"][-1]["code"] == "BROWSER_RESTARTED"

    other = client_for(config)
    with pytest.raises(HubError) as missing:
        other.post("/api/cli/jobs/job_nope/events", {"after": 0, "wait": 0})
    assert missing.value.code == "NOT_FOUND"


def test_a_task_cancelled_while_its_browser_was_away_is_stopped_when_it_returns(hub, device, tmp_path):
    _, config = device
    client = client_for(config)
    folder = tmp_path / "code" / "away"
    folder.mkdir(parents=True)
    project = client.post("/api/cli/project", {"root": str(folder)})
    browser, token = link_browser(hub, config)
    job_id = client.post("/api/cli/jobs", {"project_id": project["id"], "prompt": "x"})["job_id"]
    browser.recv()
    browser.close()
    client.post(f"/api/cli/jobs/{job_id}/cancel")  # Ctrl+C while the browser is disconnected
    assert client.post(f"/api/cli/jobs/{job_id}/events", {"after": 0, "wait": 0})["state"] == "cancelled"

    again = FakeBrowser(hub.url, "token." + token)
    again.recv()
    again.send({"t": "hello", "name": "Edge", "running": [job_id, "job_unknown"]})  # it still runs them
    told = {again.recv()["job"], again.recv()["job"]}
    assert told == {job_id, "job_unknown"}


def test_revoking_a_browser_stops_its_tasks_there(hub, device, tmp_path):
    _, config = device
    client = client_for(config)
    folder = tmp_path / "code" / "rev"
    folder.mkdir(parents=True)
    project = client.post("/api/cli/project", {"root": str(folder)})
    browser, _ = link_browser(hub, config)
    job_id = client.post("/api/cli/jobs", {"project_id": project["id"], "prompt": "x"})["job_id"]
    browser.recv()
    link = client.post("/api/cli/status")["browsers"][0]["id"]
    client.post("/api/cli/browsers/revoke", {"id": link})
    assert browser.recv() == {"t": "cancel", "job": job_id}  # told before the connection closes
    with pytest.raises(ConnectionClosed):
        browser.recv()
    reply = client.post(f"/api/cli/jobs/{job_id}/events", {"after": 0, "wait": 0})
    assert reply["state"] == "error" and reply["events"][-1]["code"] == "BROWSER_LOST"


def test_an_earlier_disconnect_does_not_end_a_task_early(hub, device, tmp_path, monkeypatch):
    import webcoder.hub.browser as relay_module

    monkeypatch.setattr(relay_module, "RECONNECT_GRACE", 2.0)
    _, config = device
    client = client_for(config)
    folder = tmp_path / "code" / "flaky"
    folder.mkdir(parents=True)
    project = client.post("/api/cli/project", {"root": str(folder)})
    browser, token = link_browser(hub, config)
    job_id = client.post("/api/cli/jobs", {"project_id": project["id"], "prompt": "x"})["job_id"]
    browser.recv()
    started = time.monotonic()
    browser.close()  # first drop
    time.sleep(0.3)
    again = FakeBrowser(hub.url, "token." + token)
    again.recv()
    again.send({"t": "hello", "name": "Edge", "running": [job_id]})
    again.send({"t": "ping"})
    again.recv()
    time.sleep(0.4)
    again.close()  # second drop, 0.7 s after the first
    time.sleep(max(0.0, 2.4 - (time.monotonic() - started)))  # the first drop's grace is over, the second's not
    assert client.post(f"/api/cli/jobs/{job_id}/events", {"after": 0, "wait": 0})["state"] == "running"
    reply = client.post(f"/api/cli/jobs/{job_id}/events", {"after": 0, "wait": 0})
    deadline = time.time() + 5
    while reply["state"] == "running" and time.time() < deadline:
        reply = client.post(f"/api/cli/jobs/{job_id}/events", {"after": 0, "wait": 1})
    assert reply["events"][-1]["code"] == "BROWSER_LOST"


def test_disabling_a_user_drops_their_browsers_only(hub, device, tmp_path):
    _, config = device
    admins_browser, _ = link_browser(hub, config)
    invite = hub.panel.post("/api/users/invites", json={"note": "x"}).json()
    with httpx.Client(base_url=hub.url, trust_env=False) as bob_panel:
        joined = bob_panel.post(f"/api/links/invite/{invite['token']}",
                                json={"username": "bob", "password": "bob-password-1"})
        joined.raise_for_status()
        bob_panel.headers["X-CSRF-Token"] = joined.json()["csrf"]
        bobs_device = bob_panel.post("/api/devices", json={"name": "bob-pc"}).json()
    (tmp_path / "bob").mkdir()
    bob_config = pair_device(hub.url, bobs_device["code"], [str(tmp_path / "bob")],
                             tmp_path / "bob-agent" / "config.json", proxy="none")
    bobs_browser, _ = link_browser(hub, bob_config, name="bob's browser", user="bob")
    assert [row["name"] for row in client_for(bob_config).post("/api/cli/status")["browsers"]] == ["bob's browser"]
    assert [row["name"] for row in client_for(config).post("/api/cli/status")["browsers"]] == ["Edge · Windows"]

    bob = next(row for row in hub.panel.get("/api/users").json()["users"] if row["username"] == "bob")
    hub.panel.patch(f"/api/users/{bob['id']}", json={"disabled": True}).raise_for_status()
    with pytest.raises(ConnectionClosed) as dropped:
        bobs_browser.recv()
    assert dropped.value.rcvd.code == 4403
    with pytest.raises(HubError) as refused:
        client_for(bob_config).post("/api/cli/status")
    assert refused.value.code == "USER_DISABLED"
    admins_browser.send({"t": "ping"})
    assert admins_browser.recv()["t"] == "pong"


# --------------------------------------------------------------------------- the command line itself
def test_webcoder_runs_a_task_end_to_end(hub, device, tmp_path, monkeypatch, capsys):
    _, config = device
    monkeypatch.setenv("WEBCODER_HOME", str(tmp_path / "webcoder-home"))
    folder = tmp_path / "code" / "repo"
    (folder / ".git").mkdir(parents=True)
    (folder / "pkg").mkdir()
    monkeypatch.chdir(folder / "pkg")  # inside the repository: the project is its top
    browser, _ = link_browser(hub, config)
    token = hub.create_token()
    seen_prompts = []

    def play():
        for number, reply_text in enumerate(("Done: edited app.py", "Second answer")):
            job = browser.recv(timeout=30)["job"]
            seen_prompts.append(job)
            try:
                browser.event(job["id"], type="status", text="已发送，等待 ChatGPT 回复")
                browser.event(job["id"], type="conversation", url="https://chatgpt.com/c/conv-1")
                hub.ok(token, "write_file", project=job["project"], path=f"app{number}.py", content="x = 1\n")
                browser.event(job["id"], type="delta", text=reply_text[:5])
                browser.event(job["id"], type="text", text=reply_text)
                browser.event(job["id"], type="done", text=reply_text, url="https://chatgpt.com/c/conv-1")
            except Exception as exc:  # noqa: BLE001 - end the task instead of leaving the CLI waiting
                browser.event(job["id"], type="error", code="TEST", text=repr(exc))
                raise

    player = threading.Thread(target=play, daemon=True)
    player.start()
    assert cli.main(["--agent-config", str(config.path), "-m", "gpt-5-thinking", "把", "app.py", "改好"]) == 0
    out = capsys.readouterr()
    assert out.out == "Done: edited app.py\n"
    assert "项目 repo" in out.err and "write_file" in out.err and "改动 1 处" in out.err
    first = seen_prompts[0]
    assert first["model"] == "gpt-5-thinking" and first["conversation"] is None and first["app"] == "WebCoder"
    assert 'project="repo"' in first["prompt"] and first["prompt"].endswith("把 app.py 改好")

    assert cli.main(["--agent-config", str(config.path), "再看看"]) == 0  # the same conversation, follow-up
    player.join(10)
    second = seen_prompts[1]
    assert second["conversation"] == "https://chatgpt.com/c/conv-1" and second["model"] is None
    assert second["prompt"].startswith("（项目 repo") and second["prompt"].endswith("再看看")
    assert capsys.readouterr().out == "Second answer\n"
    sessions = json.loads((tmp_path / "webcoder-home" / "sessions.json").read_text(encoding="utf-8"))
    assert [item["conversation"] for item in sessions.values()] == ["https://chatgpt.com/c/conv-1"]

    assert cli.main(["--agent-config", str(config.path), "forget"]) == 0
    assert "已忘记" in capsys.readouterr().out


def test_an_ephemeral_bare_run_leaves_nothing_behind(hub, start_agent, tmp_path, monkeypatch, capsys):
    """What a review service runs in a fresh checkout each time: its own prompt, then no project, folder or
    conversation left over."""
    _, config = start_agent("box", tmp_path / "allowed")
    monkeypatch.setenv("WEBCODER_HOME", str(tmp_path / "webcoder-home"))
    folder = tmp_path / "tasks" / "7" / "repo"
    (folder / ".git").mkdir(parents=True)
    monkeypatch.chdir(folder)
    browser, _ = link_browser(hub, config)
    seen = []

    def play():
        job = browser.recv(timeout=30)["job"]
        seen.append(job)
        browser.event(job["id"], type="done", text="reviewed", url="https://chatgpt.com/c/review-7")

    player = threading.Thread(target=play, daemon=True)
    player.start()
    assert cli.main(["--agent-config", str(config.path), "--bare", "--ephemeral", "--alias", "dolrev-7",
                     "-n", "Review this change."]) == 0
    player.join(10)
    assert capsys.readouterr().out == "reviewed\n"
    prompt = seen[0]["prompt"]
    assert 'project="dolrev-7"' in prompt and prompt.endswith("\n\nReview this change.")
    assert "list_projects" in prompt  # says the app's tools are there: one review run was answered "no such tool"
    assert "edit_file" not in prompt  # no coding-task instructions of its own
    assert all(row["alias"] != "dolrev-7" for row in hub.panel.get("/api/projects").json()["projects"])
    assert str(folder.resolve()) not in AgentConfig.load(config.path).allowed_roots
    assert not (tmp_path / "webcoder-home" / "sessions.json").exists()

    client = client_for(config)  # only a project webc made can be taken back this way
    made = client.post("/api/cli/project", {"root": str(tmp_path / "allowed")})
    hub.panel.delete(f"/api/projects/{made['id']}").raise_for_status()
    panel_made = hub.panel.post("/api/projects", json={"alias": "kept", "device_id": config.device_id,
                                                        "root": str(tmp_path / "allowed")}).json()
    with pytest.raises(HubError) as refused:
        client.post("/api/cli/project/remove", {"project_id": panel_made["id"]})
    assert refused.value.code == "NOT_CLI_PROJECT"
    with pytest.raises(HubError) as missing:
        client.post("/api/cli/project/remove", {"project_id": "prj_nothere"})
    assert missing.value.code == "PROJECT_NOT_FOUND"


def test_sigterm_cancels_the_task_in_the_browser(hub, device, tmp_path, monkeypatch, capsys):
    """A supervisor stops webc with SIGTERM: the browser must stop too, not let ChatGPT work on unseen."""
    _, config = device
    monkeypatch.setenv("WEBCODER_HOME", str(tmp_path / "webcoder-home"))
    folder = tmp_path / "code" / "svc"
    folder.mkdir(parents=True)
    monkeypatch.chdir(folder)
    browser, _ = link_browser(hub, config)
    cancelled = []

    def play():
        job = browser.recv(timeout=30)["job"]
        browser.event(job["id"], type="status", text="working")
        time.sleep(0.5)
        signal.raise_signal(signal.SIGTERM)
        message = browser.recv(timeout=30)
        cancelled.append(message)
        browser.event(job["id"], type="cancelled")

    player = threading.Thread(target=play, daemon=True)
    player.start()
    assert cli.main(["--agent-config", str(config.path), "keep going"]) == 130
    player.join(30)
    assert cancelled and cancelled[0]["t"] == "cancel"
    assert "已取消" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # restored for whatever runs next


def test_webcoder_adds_the_folder_to_the_agent(hub, start_agent, tmp_path, monkeypatch):
    _, config = start_agent("box", tmp_path / "allowed")
    folder = tmp_path / "other" / "proj"
    folder.mkdir(parents=True)
    out = cli.Output(quiet=True)
    assert cli.ensure_allowed(config, folder, out) is True
    assert str(folder.resolve()) in config.allowed_roots
    assert cli.ensure_allowed(config, folder / "sub", out) is False
    project = cli.ensure_project(client_for(config), folder, None, True)  # waits for the agent to reload
    assert project["alias"] == "proj"


def test_cli_helpers(tmp_path, capsys):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / "a" / "b").mkdir(parents=True)
    assert cli.project_root(repo / "a" / "b", here=False) == repo.resolve()
    assert cli.project_root(repo / "a" / "b", here=True) == (repo / "a" / "b").resolve()
    assert cli.too_broad(Path.home()) and cli.too_broad(Path(Path.cwd().anchor))
    assert not cli.too_broad(repo)
    assert cli.first_word(["--agent-config", "x.json", "link"]) == "link"
    assert cli.first_word(["-m", "gpt", "fix", "it"]) == "fix"
    assert cli.first_word(["--new"]) is None

    out = cli.Output()
    out.reply("Hello")
    out.reply("Hello, world")
    out.reply("Hi, world")  # rewritten: nothing more is streamed
    out.final("Hi, world!")
    captured = capsys.readouterr()
    assert captured.out == "Hello, worldHi, world!\n" and "完整回复" in captured.err


def test_piped_input_is_attached_to_the_task(tmp_path, monkeypatch):
    assert cli.with_context("review this", "diff --git a b\n+x\n") == (
        "review this\n\n通过管道传入的内容：\n```\ndiff --git a b\n+x\n```")
    assert cli.with_context("", "only piped\n") == "only piped" and cli.with_context("task", "  \n") == "task"
    fenced = cli.with_context("explain", "````md\n```py\nx\n```\n````")
    assert fenced.split("\n")[3] == "`````"  # longer than any fence inside
    piped = tmp_path / "in.txt"
    piped.write_text("from a file\n", encoding="utf-8")
    with piped.open(encoding="utf-8") as handle:
        monkeypatch.setattr(cli.sys, "stdin", handle)
        assert cli.piped_input() == "from a file\n"
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("no descriptor"))  # never read: not a pipe or a file
    assert cli.piped_input() == ""


def test_a_slow_pipe_is_announced_once(monkeypatch):
    """`ssh host 'webc "..."'` hands webc a pipe nobody closes: say what it waits for instead of hanging silently."""
    monkeypatch.setattr(cli, "STDIN_HINT_AFTER", 0.2)
    for delay, hinted in ((0.0, 0), (1.0, 1)):
        read_end, write_end = os.pipe()
        calls = []

        def feed(write_end=write_end, delay=delay):
            time.sleep(delay)
            os.write(write_end, b"late diff\n")
            os.close(write_end)

        with os.fdopen(read_end, encoding="utf-8") as handle:
            monkeypatch.setattr(cli.sys, "stdin", handle)
            feeder = threading.Thread(target=feed)
            feeder.start()
            assert cli.piped_input(lambda calls=calls: calls.append(1)) == "late diff\n"
            feeder.join(10)
        time.sleep(0.3)  # a timer that was not cancelled would fire now
        assert len(calls) == hinted, delay


def test_agent_service_hint_names_the_platforms_autostart():
    assert "计划任务「WebCoder Agent」" in cli.agent_service("win32")
    assert "launchctl print gui/$(id -u)/com.webcoder.agent" in cli.agent_service("darwin")
    assert "systemctl --user status webcoder-agent" in cli.agent_service("linux")


def test_sessions_from_concurrent_tasks_are_all_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBCODER_HOME", str(tmp_path / "home"))
    threads = [threading.Thread(target=cli.update_sessions,
                                args=(lambda sessions, n=n: sessions.__setitem__(f"k{n}", {"conversation": n}),))
               for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    saved = json.loads((tmp_path / "home" / "sessions.json").read_text(encoding="utf-8"))
    assert sorted(saved) == [f"k{n}" for n in range(8)]
    assert not (tmp_path / "home" / "sessions.json.lock").exists()


def test_folders_added_at_once_are_all_kept(tmp_path):
    from webcoder.agent.config import AgentConfig

    path = tmp_path / "agent" / "config.json"
    AgentConfig(hub_url="http://hub", device_id="dev_x", device_secret="secret", allowed_roots=[], path=path).save()
    folders = [tmp_path / f"p{n}" for n in range(6)]
    for folder in folders:
        folder.mkdir()
    configs = [AgentConfig.load(path) for _ in folders]  # each run read it before any of them added a folder
    threads = [threading.Thread(target=cli.ensure_allowed, args=(config, folder, cli.Output(quiet=True)))
               for config, folder in zip(configs, folders, strict=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    saved = AgentConfig.load(path).allowed_roots
    assert sorted(saved) == sorted(os.path.realpath(str(folder)) for folder in folders)


def test_event_cleaning_and_aliases():
    assert clean_event({"type": "delta", "text": "x", "evil": 1}) == {"type": "delta", "text": "x"}
    assert clean_event({"type": "conversation", "url": "javascript:alert(1)"}) == {"type": "conversation"}
    assert clean_event({"type": "conversation", "url": "https://chatgpt.com/c/1"})["url"] == "https://chatgpt.com/c/1"
    assert clean_event({"type": "tool", "name": "x"}) is None and clean_event("x") is None
    assert len(clean_event({"type": "text", "text": "a" * 500_000})["text"]) == 400_000
    assert alias_for("E:\\code\\My App") == "My-App" and alias_for("/srv/中文") == "project"
    assert alias_for("/") == "project" and alias_for("/x/.hidden") == "hidden"


def test_hub_serves_the_extension(hub):
    response = hub.http.get("/downloads/webcoder-extension.zip")
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    for name in ("manifest.json", "background.js", "content.js", "options.html", "options.js", "icons/icon128.png"):
        assert "webcoder-extension/" + name in names
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3 and manifest["host_permissions"] == ["https://chatgpt.com/*"]
