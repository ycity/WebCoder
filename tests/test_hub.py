"""End-to-end tests: real hub + real agents over WebSocket, driven through /mcp."""
import asyncio
import base64
import dataclasses
import hashlib
import io
import json
import os
import re
import secrets
import stat
import time
import zipfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from tests.conftest import ADMIN_PASSWORD, sha_of
from webcoder.agent.fileops import FileOps
from webcoder.common.channel import UPGRADE_HEADER, ChannelError, open_secret, pairing_key, upgrade_header
from webcoder.common.crypto import b64d, pkce_challenge
from webcoder.common.errors import WebCoderError
from webcoder.hub import agents as agents_module
from webcoder.hub import mcp as mcp_module
from webcoder.hub import tools as tools_module
from webcoder.hub.app import BodyLimit, create_app, ws_frame_limit
from webcoder.hub.config import Settings
from webcoder.hub.downloads import agent_sources, agent_version
from webcoder.hub.runtime import RateLimiter
from webcoder.hub.schema import SchemaError, validate
from webcoder.hub.store import Store
from webcoder.hub.tools import IMAGE_MAX_BYTES, _search, load_image


def test_health_and_discovery_metadata(hub):
    assert hub.http.get("/healthz").json()["status"] == "ok"
    prm = hub.http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == hub.url + "/mcp" and prm["authorization_servers"] == [hub.url]
    meta = hub.http.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == hub.url and meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["registration_endpoint"] == hub.url + "/oauth/register"
    assert meta["authorization_response_iss_parameter_supported"] is True


def test_missing_or_bad_token_gets_401_challenge(hub):
    response = hub.rpc(None, "initialize")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert 'resource_metadata="' + hub.url + '/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'scope="read write"' in challenge
    bad = hub.rpc("wc_pat_nope", "initialize")
    assert bad.status_code == 401 and 'error="invalid_token"' in bad.headers["www-authenticate"]


def test_dynamic_registration_accepts_only_chatgpt_and_codex_redirects(hub):
    def register(uri):
        return hub.http.post("/oauth/register", json={"client_name": "t", "redirect_uris": [uri]})

    for allowed in ("https://chatgpt.com/connector_platform_oauth_redirect",
                    "https://chatgpt.com/connector/oauth/cb_1a2B3c", "http://127.0.0.1:51234/callback",
                    "http://localhost:1455/callback"):
        assert register(allowed).status_code == 201, allowed
    for refused in ("https://evil.example/callback", "https://claude.ai/api/mcp/auth_callback",
                    "https://chatgpt.com/connector/oauth/a/../../evil", "https://chatgpt.com/connector/oauth/",
                    "https://chatgpt.com.evil.example/connector_platform_oauth_redirect",
                    "http://chatgpt.com/connector_platform_oauth_redirect", "http://192.168.1.5:3000/callback"):
        response = register(refused)
        assert response.status_code == 400 and response.json()["error"] == "invalid_redirect_uri", refused


def test_allowed_browser_origins_can_read_mcp_replies(hub):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    unauthorized = hub.http.post("/mcp", json=body, headers={"Origin": hub.url})  # the hub's own origin is allowed
    assert unauthorized.status_code == 401
    assert unauthorized.headers["access-control-allow-origin"] == hub.url
    assert "WWW-Authenticate" in unauthorized.headers["access-control-expose-headers"]
    assert unauthorized.headers["vary"] == "Origin"
    other = hub.http.post("/mcp", json=body, headers={"Origin": "https://evil.example"})
    assert other.status_code == 403 and "access-control-allow-origin" not in other.headers
    no_origin = hub.http.post("/mcp", json=body)  # ChatGPT and Codex call server-to-server
    assert "access-control-allow-origin" not in no_origin.headers


def test_panel_requires_session_and_csrf(hub):
    refused = hub.http.get("/api/devices")
    assert refused.status_code == 401 and refused.headers["cache-control"] == "no-store"
    session = hub.panel.get("/api/session")  # one user's data and CSRF token: no proxy may keep it
    assert session.status_code == 200 and session.headers["cache-control"] == "no-store"
    no_csrf = hub.panel.post("/api/devices", json={"name": "x"}, headers={"X-CSRF-Token": "wrong"})
    assert no_csrf.status_code == 403


def test_re_pairing_keeps_the_other_settings(hub):
    device_id = hub.add_device("box")["device_id"]
    hub.panel.post(f"/api/devices/{device_id}/roots-command", json={
        "roots": ["C:\\work"], "ssh_hosts": ["web-1"], "skills": True, "programs": ["lark-cli"]}).raise_for_status()
    hub.panel.post(f"/api/devices/{device_id}/pairing-code", json={
        "platform": "windows", "roots": ["C:\\work"], "read_only": False, "autostart": True, "mobile": True}
                   ).raise_for_status()
    setup = hub.runtime.store.one("SELECT info FROM devices WHERE id=?", (device_id,))["info"]
    assert all(part in setup for part in ('"ssh_hosts": ["web-1"]', '"skills": true', '"programs": ["lark-cli"]',
                                          '"mobile": true'))


def test_unauthenticated_agent_sockets_are_bounded(hub):
    """Anyone can open /agent/ws, and the server holds a whole frame before the hub reads it: frames are no larger
    than the hub needs, and only a couple of connections may be before authentication at once."""
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    limit = ws_frame_limit(Settings(public_url="https://hub.example"))
    assert 24 * 1024 * 1024 < limit <= 26 * 1024 * 1024
    assert ws_frame_limit(Settings(public_url="https://hub.example", max_transfer_bytes=40 * 1024 * 1024)) > \
        40 * 1024 * 1024 * 4 // 3
    url = hub.url.replace("http://", "ws://") + "/agent/ws"
    idle = [connect(url, open_timeout=5) for _ in range(agents_module.MAX_HANDSHAKES)]
    try:
        assert all(json.loads(ws.recv(timeout=5))["t"] == "challenge" for ws in idle)
        with pytest.raises(InvalidStatus):  # refused before the upgrade: nothing it sends is read
            connect(url, open_timeout=5)
    finally:
        for ws in idle:
            ws.close()
    hub.wait_for(lambda: hub.runtime.agents.handshakes == 0, what="handshakes to end")
    with connect(url, open_timeout=5) as ws:
        assert json.loads(ws.recv(timeout=5))["t"] == "challenge"


def test_devices_get_in_while_strangers_hold_the_handshake_places(hub, start_agent, tmp_path):
    """A device proves itself in the upgrade request, so strangers holding every place for unauthenticated
    connections cannot keep it out. A proof is taken once, and only while it is fresh."""
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    url = hub.url.replace("http://", "ws://") + "/agent/ws"
    strangers = [connect(url, open_timeout=5) for _ in range(agents_module.MAX_HANDSHAKES)]
    try:
        assert all(json.loads(ws.recv(timeout=5))["t"] == "challenge" for ws in strangers)
        device_id, config = start_agent("laptop", tmp_path / "work")  # connects all the same
        assert hub.device(device_id)["status"] == "online"
        secret = b64d(config.device_secret)

        def proof(stamp: int) -> dict:
            return {UPGRADE_HEADER: upgrade_header(secret, device_id, stamp, os.urandom(32))}
        fresh = proof(int(time.time()))
        with connect(url, open_timeout=5, additional_headers=fresh) as ws:
            assert json.loads(ws.recv(timeout=5))["t"] == "challenge"
        for headers in (fresh, proof(int(time.time()) - 3600), {UPGRADE_HEADER: f"{device_id} 1 x y"}):
            with pytest.raises(InvalidStatus):  # replayed, stale, malformed: a stranger, and no place is free
                connect(url, open_timeout=5, additional_headers=headers)
    finally:
        for ws in strangers:
            ws.close()


def test_pairing_over_plain_http_seals_the_device_secret(tmp_path):
    """A hub on a LAN address over plain HTTP: the device secret comes back sealed for the agent's one-time key,
    and an agent from before sealed pairing (no key) is refused instead of getting the secret in the clear."""
    app = create_app(Settings(public_url="http://192.168.1.5:8787", data_dir=tmp_path / "data"))
    store = app.state.runtime.store
    app.state.runtime.auth.create_admin("admin", ADMIN_PASSWORD)
    with TestClient(app) as client:
        client.headers["X-CSRF-Token"] = client.post("/api/login", json={
            "username": "admin", "password": ADMIN_PASSWORD}).json()["csrf"]
        code = client.post("/api/devices", json={"name": "lan-pc"}).json()["code"]
        old = client.post("/api/agent/pair", json={"code": code, "hostname": "lan-pc"})
        assert old.status_code == 400 and old.json()["error"]["code"] == "INSECURE_PAIRING"
        for key in ("bm90LWEta2V5", "not base64!", base64.b64encode(bytes(32)).decode()):
            bad = client.post("/api/agent/pair", json={"code": code, "key": key})
            assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_KEY", key
        private, public = pairing_key()
        reply = client.post("/api/agent/pair", json={"code": code, "key": public})  # the code was not used up
        assert reply.status_code == 200, reply.text
        reply = reply.json()
        assert "device_secret" not in reply and set(reply) >= {"hub_key", "sealed_secret"}
        secret = open_secret(private, reply, reply["device_id"])
        row = store.one("SELECT secret_enc FROM devices WHERE id=?", (reply["device_id"],))
        assert store.decrypt(row["secret_enc"]) == secret and len(b64d(secret)) == 32
    with pytest.raises(ChannelError):
        open_secret(pairing_key()[0], reply, reply["device_id"])  # only the key that asked can open it
    with pytest.raises(ChannelError):
        open_secret(private, reply, "dev_other")  # and only for this device


def test_oauth_clients_see_the_tools_their_projects_computers_offer(hub, start_agent, tmp_path):
    """An OAuth client sees phone tools when the user turned phone control on somewhere (ChatGPT then asks for
    the permission), but only on computers its projects are on: elsewhere every call would fail."""
    plain_id, _ = start_agent("plain", tmp_path / "plain")
    project = hub.add_project("site", plain_id, tmp_path / "plain")
    phone_id = hub.add_device("phone-box")["device_id"]
    store = hub.runtime.store
    store.execute("UPDATE devices SET info=? WHERE id=?", (json.dumps({"mobile": {"enabled": True}}), phone_id))
    user_id = store.one("SELECT user_id FROM devices WHERE id=?", (plain_id,))["user_id"]
    now = time.time()
    for grant, projects in (("g_all", "*"), ("g_site", [project["id"]])):
        store.execute("INSERT INTO grants(id, kind, label, scopes, projects, created, user_id) VALUES "
                      "(?, 'oauth', 't', '[\"read\"]', ?, ?, ?)", (grant, json.dumps(projects), now, user_id))
        store.execute("INSERT INTO tokens(hash, grant_id, kind, expires, created) VALUES (?, ?, 'access', ?, ?)",
                      (hashlib.sha256(f"{grant}-token".encode()).hexdigest(), grant, now + 3600, now))

    def names(token):
        return {tool["name"] for tool in hub.rpc(token, "tools/list").json()["result"]["tools"]}
    assert "mobile_devices" in names("g_all-token") and "mobile_devices" not in names("g_site-token")


def test_batches_are_bounded(hub):
    token = hub.create_token(scopes=("read",))
    batch = [{"jsonrpc": "2.0", "id": n, "method": "ping"} for n in range(51)]
    refused = hub.http.post("/mcp", json=batch, headers={"Authorization": f"Bearer {token}"})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == -32600
    assert len(hub.http.post("/mcp", json=batch[:50], headers={"Authorization": f"Bearer {token}"}).json()) == 50


def test_a_batch_stops_running_past_its_reply_budget(hub, monkeypatch):
    monkeypatch.setattr(mcp_module, "MAX_BATCH_REPLIES", 50)  # about one ping reply
    token = hub.create_token(scopes=("read",))
    batch = [{"jsonrpc": "2.0", "id": n, "method": "ping"} for n in range(3)]
    replies = hub.http.post("/mcp", json=batch, headers={"Authorization": f"Bearer {token}"}).json()
    assert [("result" in reply) for reply in replies] == [True, True, False]
    assert replies[2]["error"]["code"] == -32000


def test_an_image_that_grew_after_the_check_is_not_shown():
    class Context:
        def heavy(self):
            pass

        async def agent(self, project, op, args):
            if op == "fs.stat":
                return {"exists": True, "type": "file", "size": 100, "path": "shot.png"}
            return {"path": "shot.png", "size": IMAGE_MAX_BYTES + 1,
                    "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()}
    with pytest.raises(WebCoderError) as err:
        asyncio.run(load_image(Context(), {}, "shot.png"))
    assert err.value.code == "FILE_TOO_LARGE"


def test_only_a_few_large_bodies_are_handled_at_once():
    """Each large body is held whole while it is handled: past a few at a time the answer is 503, not more
    memory. Small requests are not counted."""
    release = asyncio.Event()

    async def route(scope, receive, send):
        await release.wait()
        await JSONResponse({"ok": True})(scope, receive, send)
    middleware = BodyLimit(route, large=1)

    async def post(length: int) -> int:
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)
        await middleware({"type": "http", "method": "POST", "path": "/mcp",
                          "headers": [(b"content-length", str(length).encode())]}, receive, send)
        return sent[0]["status"]

    async def main():
        first = asyncio.create_task(post(5_000_000))
        await asyncio.sleep(0.05)
        second = await post(5_000_000)
        small = asyncio.create_task(post(100))
        await asyncio.sleep(0.05)
        release.set()
        return await first, second, await small
    assert asyncio.run(main()) == (200, 503, 200)


def test_small_requests_with_large_answers_take_the_same_places():
    """A file read as base64 or an image is large on the way back: such a call takes one of the places large
    request bodies use, keeps it until the answer is sent, and a request never holds more than one."""
    release, taken = asyncio.Event(), []

    async def route(scope, receive, send):
        taken.append((scope["webcoder.take_large"](), scope["webcoder.take_large"]()))  # a second call: the same place
        await release.wait()
        await JSONResponse({"ok": True})(scope, receive, send)
    middleware = BodyLimit(route, large=1)

    async def post() -> None:
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass
        await middleware({"type": "http", "method": "POST", "path": "/mcp",
                          "headers": [(b"content-length", b"100")]}, receive, send)

    async def main():
        first = asyncio.create_task(post())
        await asyncio.sleep(0.05)
        second = asyncio.create_task(post())  # finds no place while the first answer is on its way
        await asyncio.sleep(0.05)
        assert middleware.large_now == 1
        release.set()
        await first
        await second
        assert middleware.large_now == 0
        await post()  # free again
    asyncio.run(main())
    assert taken == [(True, True), (False, False), (True, True)]


def test_a_large_answer_waits_for_a_place(hub, start_agent, tmp_path):
    """Through /mcp: while the places are taken, a base64 read is turned away (HUB_BUSY), and nothing ran."""
    device_id, _ = start_agent("laptop", tmp_path / "work")
    (tmp_path / "work" / "blob.bin").write_bytes(b"\x00" * 1024)
    hub.add_project("app", device_id, tmp_path / "work")
    token = hub.create_token()
    busy = tools_module.TAKE_LARGE.set(lambda: False)
    try:
        text, is_error = asyncio.run(_call_tool(hub, token, "read_file", project="app", path="blob.bin",
                                                encoding="base64"))
    finally:
        tools_module.TAKE_LARGE.reset(busy)
    assert is_error and "HUB_BUSY" in text
    assert "1,024 bytes as base64" in hub.ok(token, "read_file", project="app", path="blob.bin", encoding="base64")


async def _call_tool(hub, token, name, **arguments):
    """Run one tool as /mcp would, in this task (so a context variable set here applies)."""
    principal, _ = hub.runtime.auth.principal_for_token(token)
    result, is_error = await tools_module.execute(hub.runtime, principal, tools_module.TOOLS[name], arguments)
    return result.text, is_error


def test_integer_arguments_must_be_finite():
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(SchemaError):
            validate({"type": "integer"}, value)


def test_device_names_tell_computers_apart(hub, start_agent, tmp_path):
    """The AI picks a computer for phones, SSH and programs by name."""
    device_id, _ = start_agent("laptop", tmp_path / "work")
    again = hub.panel.post("/api/devices", json={"name": " Laptop "})
    assert again.status_code == 409 and again.json()["error"]["code"] == "DEVICE_NAME_TAKEN"
    blank = hub.panel.post("/api/devices", json={"name": "   "})
    assert blank.status_code == 400 and blank.json()["error"]["code"] == "INVALID_NAME"
    assert hub.panel.patch(f"/api/devices/{device_id}", json={"name": " "}).status_code == 400
    other = hub.add_device("desktop")["device_id"]
    renamed = hub.panel.patch(f"/api/devices/{other}", json={"name": "LAPTOP"})
    assert renamed.status_code == 409
    hub.panel.patch(f"/api/devices/{device_id}", json={"name": "laptop"}).raise_for_status()  # its own name: fine
    hub.runtime.store.execute("UPDATE devices SET name='laptop' WHERE id=?", (other,))  # from before names were unique
    token = hub.create_token(scopes=("read", "ssh"))
    text, is_error = hub.call(token, "ssh_exec", host="web-1", command="hostname", computer="laptop")
    assert is_error and "COMPUTER_AMBIGUOUS" in text and device_id in text and other in text


def test_pairing_and_project_validation(hub, start_agent, tmp_path):
    device_id, config = start_agent("laptop", tmp_path / "work")
    device = hub.device(device_id)
    assert device["status"] == "online" and device["info"]["allowed_roots"] == config.allowed_roots
    (tmp_path / "work" / "app").mkdir()
    roots = hub.panel.get(f"/api/devices/{device_id}/browse").json()
    assert [d["path"] for d in roots["dirs"]] == config.allowed_roots
    inside = hub.panel.get(f"/api/devices/{device_id}/browse", params={"path": config.allowed_roots[0]}).json()
    assert [d["name"] for d in inside["dirs"]] == ["app"]
    escape = hub.panel.get(f"/api/devices/{device_id}/browse", params={"path": str(tmp_path)})
    assert escape.status_code == 400 and escape.json()["error"]["code"] == "ROOT_NOT_ALLOWED"
    hub.add_project("app", device_id, tmp_path / "work" / "app")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    refused = hub.panel.post("/api/projects", json={"alias": "x", "device_id": device_id, "root": str(outside)})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "ROOT_NOT_ALLOWED"
    taken = hub.panel.post("/api/projects", json={"alias": "APP", "device_id": device_id,
                                                  "root": str(tmp_path / "work")})
    assert taken.status_code == 409


def test_mcp_protocol_basics(hub):
    token = hub.create_token()
    init = hub.rpc(token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"] and init["instructions"]
    newer = hub.rpc(token, "initialize", {"protocolVersion": "2099-01-01"}).json()["result"]
    assert newer["protocolVersion"] == "2025-11-25"
    notification = hub.http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                 headers={"Authorization": f"Bearer {token}"})
    assert notification.status_code == 202
    tools = hub.rpc(token, "tools/list").json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert {"list_projects", "read_file", "edit_file", "copy_file", "restore_backup"} <= names
    assert all(tool["annotations"]["readOnlyHint"] == (tool["name"] in {
        "list_projects", "list_dir", "read_file", "read_files", "search", "find_files", "stat", "view_image",
        "list_backups", "git_status", "git_diff", "git_log", "git_show", "git_blame", "git_branches",
        "list_skills", "read_skill"})
               for tool in tools)
    assert all(tool["securitySchemes"] == [{"type": "noauth"}] for tool in tools)  # token callers need no OAuth
    stream = hub.http.get("/mcp", headers={"Accept": "text/event-stream"})  # no server-initiated streams
    assert stream.status_code == 405 and stream.headers["allow"] == "POST" and stream.content == b""
    browser = hub.http.get("/mcp", headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    assert browser.status_code == 405 and "这是 MCP 接口地址" in browser.text and f'href="{hub.url}/"' in browser.text
    unknown = hub.rpc(token, "tools/call", {"name": "format_disk", "arguments": {}}).json()
    assert unknown["error"]["code"] == -32602
    batch = hub.http.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert batch.json() == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_modern_protocol_requests_fall_back_with_plain_400(hub):
    token = hub.create_token()
    discover = hub.rpc(token, "server/discover", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                                          "io.modelcontextprotocol/clientCapabilities": {}}},
                       headers={"MCP-Protocol-Version": "2026-07-28"})
    assert discover.status_code == 400
    assert discover.json()["error"]["code"] == -32600  # not -32022, so dual-era clients fall back to initialize


def test_file_tools_end_to_end(hub, start_agent, tmp_path):
    root = tmp_path / "work"
    device_id, _ = start_agent("laptop", root)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
    hub.add_project("app", device_id, root)
    token = hub.create_token()

    listing = hub.ok(token, "list_projects")
    assert "app [read-write]" in listing and "online" in listing
    tree = hub.ok(token, "list_dir", project="app", depth=2)
    assert "src/app.py" in tree and "[blocked: secret file]" in tree
    read = hub.ok(token, "read_file", project="app", path="src/app.py")
    assert "     2\t    return 1" in read
    edited = hub.ok(token, "edit_file", project="app", path="src/app.py", expected_sha256=sha_of(read),
                    edits=[{"old_string": "return 1", "new_string": "return 42"}])
    assert "+    return 42" in edited
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "def main():\n    return 42\n"
    assert "src/app.py:2:" in hub.ok(token, "search", project="app", query="return 42")
    created = hub.ok(token, "write_file", project="app", path="docs/notes.md", content="# 笔记\n")
    assert created.startswith("Created") and (root / "docs" / "notes.md").read_text(encoding="utf-8") == "# 笔记\n"
    stat = hub.ok(token, "stat", project="app", path="docs/notes.md")
    assert sha_of(stat) == hashlib.sha256("# 笔记\n".encode()).hexdigest()
    moved = hub.ok(token, "move_path", project="app", path="docs/notes.md", new_path="docs/README.md",
                   expected_sha256=sha_of(stat))
    assert "→ docs/README.md" in moved
    deleted = hub.ok(token, "delete_path", project="app", path="docs/README.md", expected_sha256=sha_of(stat))
    backup_id = deleted.split("backup ")[1].split(" ")[0]
    assert not (root / "docs" / "README.md").exists()
    assert backup_id in hub.ok(token, "list_backups", project="app")
    hub.ok(token, "restore_backup", project="app", backup_id=backup_id)
    assert (root / "docs" / "README.md").read_text(encoding="utf-8") == "# 笔记\n"
    many = hub.ok(token, "read_files", project="app", paths=["src/app.py", "missing.txt"])
    assert "return 42" in many and "error NOT_FOUND" in many


def test_errors_are_explained_to_the_model(hub, start_agent, tmp_path):
    root = tmp_path / "work"
    device_id, _ = start_agent("laptop", root)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
    hub.add_project("app", device_id, root)
    token = hub.create_token()
    cases = [
        ({"tool": "write_file", "project": "app", "path": "a.txt", "content": "v2\n"}, "SHA_REQUIRED"),
        ({"tool": "write_file", "project": "app", "path": "a.txt", "content": "v2\n", "expected_sha256": "0" * 64},
         "SHA_MISMATCH"),
        ({"tool": "read_file", "project": "app", "path": "../../etc/passwd"}, "INVALID_PATH"),
        ({"tool": "read_file", "project": "app", "path": ".env"}, "PATH_DENIED"),
        ({"tool": "read_file", "project": "nope", "path": "a.txt"}, "PROJECT_NOT_FOUND"),
        ({"tool": "read_file", "project": "app"}, "INVALID_ARGUMENTS"),
    ]
    for arguments, expected in cases:
        text, is_error = hub.call(token, arguments.pop("tool"), **arguments)
        assert is_error and f"Error {expected}" in text, (expected, text)


def test_read_only_token_gets_tool_error_not_http_error(hub, start_agent, tmp_path):
    device_id, _ = start_agent("laptop", tmp_path / "work")
    hub.add_project("app", device_id, tmp_path / "work")
    token = hub.create_token(scopes=["read"])
    assert "read-only" in hub.ok(token, "list_projects")
    text, is_error = hub.call(token, "write_file", project="app", path="x.txt", content="x")
    assert is_error and "INSUFFICIENT_SCOPE" in text
    assert not (tmp_path / "work" / "x.txt").exists()


def test_project_mode_token_scope_and_agent_switch(hub, start_agent, tmp_path):
    device_a, _ = start_agent("a", tmp_path / "a")
    device_b, _ = start_agent("b", tmp_path / "b", read_only=True)
    ro = hub.add_project("ro", device_a, tmp_path / "a", mode="ro")
    hub.add_project("locked", device_b, tmp_path / "b")
    token = hub.create_token()
    text, is_error = hub.call(token, "write_file", project="ro", path="x.txt", content="x")
    assert is_error and "PROJECT_READ_ONLY" in text
    text, is_error = hub.call(token, "write_file", project="locked", path="x.txt", content="x")
    assert is_error and "AGENT_READ_ONLY" in text
    only_ro = hub.create_token(projects=[ro["id"]])
    listing = hub.ok(only_ro, "list_projects")
    assert "ro [read-only]" in listing and "locked" not in listing
    text, is_error = hub.call(only_ro, "list_dir", project="locked")
    assert is_error and "PROJECT_NOT_FOUND" in text


def test_cross_device_copy(hub, start_agent, tmp_path):
    server_id, _ = start_agent("server", tmp_path / "server")
    laptop_id, _ = start_agent("laptop", tmp_path / "laptop")
    payload = bytes(range(256)) * 64
    (tmp_path / "server" / "backup.tar").write_bytes(payload)
    hub.add_project("srv", server_id, tmp_path / "server")
    hub.add_project("pc", laptop_id, tmp_path / "laptop")
    token = hub.create_token()
    copied = hub.ok(token, "copy_file", from_project="srv", from_path="backup.tar", to_project="pc",
                    to_path="incoming/backup.tar")
    assert "srv:backup.tar → pc:incoming/backup.tar" in copied
    assert (tmp_path / "laptop" / "incoming" / "backup.tar").read_bytes() == payload
    text, is_error = hub.call(token, "copy_file", from_project="srv", from_path="backup.tar", to_project="pc",
                              to_path="incoming/backup.tar")
    assert is_error and "SHA_REQUIRED" in text
    hub.ok(token, "copy_file", from_project="srv", from_path="backup.tar", to_project="pc",
           to_path="incoming/backup.tar", overwrite_sha256=hashlib.sha256(payload).hexdigest())


def test_copies_over_the_hub_limit_are_refused_before_reading(hub, start_agent, tmp_path, monkeypatch):
    device_id, _ = start_agent("server", tmp_path / "server")
    (tmp_path / "server" / "big.bin").write_bytes(b"x" * 5000)
    hub.add_project("srv", device_id, tmp_path / "server")
    reads = []
    real_read = FileOps.read_file
    monkeypatch.setattr(FileOps, "read_file", lambda self, *a, **k: reads.append(a) or real_read(self, *a, **k))
    monkeypatch.setattr(hub.runtime, "settings", dataclasses.replace(hub.runtime.settings, max_transfer_bytes=1000))
    text, is_error = hub.call(hub.create_token(), "copy_file", from_project="srv", from_path="big.bin",
                              to_project="srv", to_path="copy.bin")
    assert is_error and "FILE_TOO_LARGE" in text and reads == []  # not read, not sent through the hub
    assert not (tmp_path / "server" / "copy.bin").exists()


def test_offline_and_disabled_devices(hub, start_agent, tmp_path):
    device_id, _ = start_agent("laptop", tmp_path / "work")
    hub.add_project("app", device_id, tmp_path / "work")
    token = hub.create_token()
    hub.panel.patch(f"/api/devices/{device_id}", json={"enabled": False}).raise_for_status()
    hub.wait_for(lambda: not hub.device(device_id)["online"], what="device offline")
    text, is_error = hub.call(token, "list_dir", project="app")
    assert is_error and "DEVICE_DISABLED" in text


def _oauth_authorize(hub, redirect_uri: str, *, client_name: str = "Codex", scope: str = "read write",
                     resource: str | None = None):
    client = hub.http.post("/oauth/register", json={"client_name": client_name, "redirect_uris": [redirect_uri]})
    client_id = client.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    response = hub.http.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "state": "xyz",
        "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256", "scope": scope,
        "resource": resource or hub.url + "/mcp"})
    assert response.status_code == 302 and response.headers["location"].startswith("/#/consent/")
    return client_id, verifier, response.headers["location"].rsplit("/", 1)[1]


def _approve(hub, request_id: str, scopes) -> dict:
    details = hub.panel.get(f"/api/oauth/requests/{request_id}").json()
    decision = hub.panel.post(f"/api/oauth/requests/{request_id}", json={"approve": True, "scopes": scopes,
                                                                         "projects": "*"}).json()
    query = parse_qs(urlsplit(decision["redirect_to"]).query)
    assert query["state"] == ["xyz"] and query["iss"] == [hub.url]
    return {"details": details, "code": query["code"][0]}


def test_oauth_flow_with_step_up_and_refresh_rotation(hub, start_agent, tmp_path):
    device_id, _ = start_agent("laptop", tmp_path / "work")
    hub.add_project("app", device_id, tmp_path / "work")
    redirect = "http://127.0.0.1:1455/callback"
    client_id, verifier, request_id = _oauth_authorize(hub, redirect)
    approved = _approve(hub, request_id, ["read"])  # owner unticks "write"
    assert approved["details"]["surface"] == "Codex" and approved["details"]["loopback"] is True

    bad = hub.http.post("/oauth/token", data={"grant_type": "authorization_code", "code": approved["code"],
                                              "client_id": client_id, "redirect_uri": redirect,
                                              "code_verifier": "x" * 43})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"  # the code is now burned
    client_id, verifier, request_id = _oauth_authorize(hub, redirect)
    code = _approve(hub, request_id, ["read"])["code"]
    tokens = hub.http.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                 "client_id": client_id, "redirect_uri": redirect,
                                                 "code_verifier": verifier, "resource": hub.url + "/mcp"}).json()
    assert tokens["token_type"] == "Bearer" and tokens["scope"] == "read"
    access = tokens["access_token"]
    assert "app [read-write]" in hub.ok(access, "list_projects")

    step_up = hub.rpc(access, "tools/call", {"name": "write_file",
                                             "arguments": {"project": "app", "path": "x.txt", "content": "x"}})
    assert step_up.status_code == 200  # ChatGPT convention: a tool error carrying the challenge in _meta
    result = step_up.json()["result"]
    challenge = result["_meta"]["mcp/www_authenticate"][0]
    assert result["isError"] and "INSUFFICIENT_SCOPE" in result["content"][0]["text"]
    assert 'error="insufficient_scope"' in challenge and "error_description=" in challenge
    assert 'scope="read write"' in challenge and "resource_metadata=" in challenge
    assert not (tmp_path / "work" / "x.txt").exists()

    live = hub.panel.get("/api/overview").json()["tokens"]
    assert live == len(hub.panel.get("/api/tokens").json()["tokens"]) >= 1
    refreshed = hub.http.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                    "refresh_token": tokens["refresh_token"]}).json()
    assert refreshed["access_token"] != access
    reuse = hub.http.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                "refresh_token": tokens["refresh_token"]})
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    unknown = hub.http.post("/oauth/token", data={"grant_type": "authorization_code", "code": "x",
                                                  "client_id": "wc_c_gone", "code_verifier": verifier})
    assert unknown.status_code == 401 and unknown.json()["error"] == "invalid_client"


def test_oauth_for_chatgpt_web(hub, start_agent, tmp_path):
    """ChatGPT's shape: DCR with the stable chatgpt.com callback, empty scope, resource = server origin."""
    device_id, _ = start_agent("laptop", tmp_path / "work")
    hub.add_project("app", device_id, tmp_path / "work")
    redirect = "https://chatgpt.com/connector_platform_oauth_redirect"
    client_id, verifier, request_id = _oauth_authorize(hub, redirect, client_name="ChatGPT", scope="",
                                                       resource=hub.url)
    approved = _approve(hub, request_id, ["read", "write"])
    details = approved["details"]
    assert details["client"] == "chatgpt" and details["loopback"] is False and details["scopes"] == ["read", "write"]
    tokens = hub.http.post("/oauth/token", data={"grant_type": "authorization_code", "code": approved["code"],
                                                 "client_id": client_id, "redirect_uri": redirect,
                                                 "code_verifier": verifier, "resource": hub.url}).json()
    assert tokens["scope"] == "read write"
    tools = hub.rpc(tokens["access_token"], "tools/list").json()["result"]["tools"]
    schemes = {tool["name"]: tool["securitySchemes"] for tool in tools}
    assert schemes["read_file"] == [{"type": "oauth2", "scopes": ["read"]}]
    assert schemes["edit_file"] == [{"type": "oauth2", "scopes": ["read", "write"]}]
    assert all(tool["_meta"]["securitySchemes"] == tool["securitySchemes"] for tool in tools)
    hub.ok(tokens["access_token"], "write_file", project="app", path="from-chatgpt.txt", content="hi\n")
    assert (tmp_path / "work" / "from-chatgpt.txt").read_text() == "hi\n"
    revoked = hub.http.post("/oauth/revoke", data={"token": tokens["refresh_token"]})
    assert revoked.status_code == 200
    assert hub.rpc(tokens["access_token"], "tools/list").status_code == 401


def test_chatgpt_per_connection_callback_and_error_redirects_carry_iss(hub):
    redirect = "https://chatgpt.com/connector/oauth/cb_42"
    client = hub.http.post("/oauth/register", json={"client_name": "ChatGPT", "redirect_uris": [redirect]}).json()
    denied = hub.http.get("/oauth/authorize", params={"client_id": client["client_id"], "redirect_uri": redirect,
                                                      "response_type": "token", "state": "s"})
    query = parse_qs(urlsplit(denied.headers["location"]).query)
    assert denied.status_code == 302 and denied.headers["location"].startswith(redirect)
    assert query["error"] == ["unsupported_response_type"] and query["iss"] == [hub.url] and query["state"] == ["s"]


def test_authorization_requests_are_rate_limited_and_bounded(hub):
    """Each accepted request is stored for 10 minutes: an anonymous flood must not grow the database."""
    redirect = "https://chatgpt.com/connector/oauth/cb_1"
    client = hub.http.post("/oauth/register", json={"client_name": "ChatGPT", "redirect_uris": [redirect]}).json()
    params = {"client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "state": "s",
              "code_challenge": pkce_challenge(secrets.token_urlsafe(48)), "code_challenge_method": "S256",
              "resource": hub.url + "/mcp"}
    hub.runtime.oauth.MAX_PENDING = 3  # the cap on requests awaiting consent, from everyone
    statuses = [hub.http.get("/oauth/authorize", params=params).status_code for _ in range(4)]
    assert statuses == [302, 302, 302, 400]
    assert hub.runtime.store.one("SELECT count(*) AS n FROM oauth_requests")["n"] == 3
    hub.runtime.oauth.MAX_PENDING = 2000
    statuses = [hub.http.get("/oauth/authorize", params=params).status_code for _ in range(26)]
    limited = hub.http.get("/oauth/authorize", params=params)  # the 31st from this address within 10 minutes
    assert set(statuses) == {302} and limited.status_code == 429 and "请求过于频繁" in limited.text


def test_request_bodies_are_capped_before_they_are_read(hub):
    client = TestClient(hub.app, base_url=hub.url)  # in process: an early answer cannot race the upload
    big = b"token=" + b"x" * (2 * 1024 * 1024)
    declared = client.post("/oauth/revoke", content=big,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert declared.status_code == 413 and "REQUEST_TOO_LARGE" in declared.text

    def chunks():  # no length up front: counted while it arrives
        for _ in range(40):
            yield b"x" * 65536
    streamed = client.post("/oauth/register", content=chunks(), headers={"Content-Type": "application/json"})
    assert streamed.status_code == 413 and "Content-Length" not in streamed.request.headers
    metadata = json.dumps({"client_name": "chunked", "redirect_uris": ["http://127.0.0.1:5000/cb"]}).encode()
    chunked = client.post("/oauth/register", content=iter([metadata[:10], metadata[10:]]),
                          headers={"Content-Type": "application/json"})  # a normal body, in chunks: read whole
    assert chunked.status_code == 201 and chunked.json()["client_name"] == "chunked"
    anonymous = client.post("/mcp", content=b"x" * (8 * 1024 * 1024), headers={"Content-Type": "application/json"})
    assert anonymous.status_code == 401  # the token is checked before the body is read
    token = hub.create_token(scopes=("read",))
    padded = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * 2_000_000}},
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
    assert padded.status_code == 200 and padded.json()["result"] == {}  # tool calls carry file content: more room


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_hub_data_is_private_whatever_the_umask(tmp_path):
    old = os.umask(0o022)
    try:
        store = Store(tmp_path / "data")
        store.execute("INSERT INTO meta(key, value) VALUES ('probe', '1')")  # WAL and SHM files appear
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "data").stat().st_mode) == 0o700
    for name in ("hub.db", "hub.db-wal", "hub.db-shm", "master.key"):
        if (tmp_path / "data" / name).exists():
            assert stat.S_IMODE((tmp_path / "data" / name).stat().st_mode) == 0o600, name
    store.close()
    (tmp_path / "data" / "hub.db").chmod(0o644)  # a database from before this: tightened on start
    (tmp_path / "data" / "master.key").chmod(0o644)  # or data restored from a backup with looser permissions
    (tmp_path / "data").chmod(0o755)
    Store(tmp_path / "data").close()
    assert stat.S_IMODE((tmp_path / "data" / "hub.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "data" / "master.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "data").stat().st_mode) == 0o700


def test_rate_limiter_forgets_idle_addresses():
    limiter = RateLimiter()
    limiter.IDLE = 0.0
    for n in range(2500):  # a flood from many addresses
        limiter.check(f"authorize:10.0.{n // 256}.{n % 256}", 30, 600)
    assert len(limiter._hits) < 1000  # without pruning: 2500


def test_agent_package_download(hub):
    response = hub.http.get("/downloads/webcoder-agent.zip")
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert any(name.endswith("pyproject.toml") for name in names)
    assert any(name.endswith("webcoder/agent/client.py") for name in names)
    assert any(name.endswith("webcoder/bridge/__main__.py") for name in names)
    assert not any("/hub/" in name for name in names)
    # A content-hash local version makes `pip install --upgrade` pick up changed agent code.
    pyproject = next(n for n in names if n.endswith("pyproject.toml"))
    version = re.search(r'version = "([^"]+)"', zipfile.ZipFile(io.BytesIO(response.content)).read(pyproject).decode())
    assert re.fullmatch(r"\d+\.\d+\.\d+\+[0-9a-f]{12}", version.group(1))
    assert version.group(1) == agent_version(agent_sources())


def test_find_files_through_hub(hub, start_agent, tmp_path):
    device_id, _ = start_agent("laptop", tmp_path / "work")
    (tmp_path / "work" / "docs").mkdir()
    (tmp_path / "work" / "docs" / "Guide.md").write_text("# guide\n", encoding="utf-8")
    (tmp_path / "work" / "notes.txt").write_text("x", encoding="utf-8")
    hub.add_project("app", device_id, tmp_path / "work")
    token = hub.create_token(scopes=("read",))
    text = hub.ok(token, "find_files", project="app", pattern="*.MD,docs")
    assert "2 match(es)" in text and "docs/Guide.md" in text and "docs/\n" in text + "\n"
    assert "No files or folders match" in hub.ok(token, "find_files", project="app", pattern="*.py")


def test_search_tells_the_model_why_it_stopped():
    class Ctx:  # just enough of ToolContext for _search
        rt = SimpleNamespace(settings=SimpleNamespace(agent_timeout=45))

        def project(self, alias):
            return {"alias": alias}

        def target(self, project, path):
            pass

        async def agent(self, project, op, args, timeout):
            return {"query": args["query"], "path": ".", "matches": [], "files_scanned": 1, "files_matched": 0,
                    "truncated": "regex_timeout"}

    text = asyncio.run(_search(Ctx(), {"project": "app", "query": "(a|aa)+$", "path": ".", "regex": True,
                                       "case_sensitive": False, "max_results": 100, "context": 0}))
    assert text.startswith("No matches") and "too slow" in text  # not just "no matches"


def test_base64_read_through_hub(hub, start_agent, tmp_path):
    device_id, _ = start_agent("laptop", tmp_path / "work")
    (tmp_path / "work" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(100))
    hub.add_project("app", device_id, tmp_path / "work")
    token = hub.create_token()
    text = hub.ok(token, "read_file", project="app", path="logo.png", encoding="base64")
    assert base64.b64decode(text.splitlines()[1]) == b"\x89PNG\r\n\x1a\n" + bytes(100)
