"""Accounts: invite / reset links, per-user isolation, administrators, and the v1 -> v2 database upgrade."""
import json
import secrets
import sqlite3
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.conftest import ADMIN_PASSWORD, AgentThread
from webcoder.agent.__main__ import pair_device
from webcoder.common.crypto import hash_password, pkce_challenge, sha256_hex
from webcoder.hub.auth import Auth
from webcoder.hub.config import Settings
from webcoder.hub.store import Store

PASSWORD = "bobs-own-password"
LOCAL_PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def invite(hub, note="bob") -> str:
    response = hub.panel.post("/api/users/invites", json={"note": note})
    assert response.status_code == 200, response.text
    return response.json()["url"].rsplit("/", 1)[1]


def client(hub) -> httpx.Client:
    return httpx.Client(base_url=hub.url, trust_env=False, timeout=60)


def signed_in(http: httpx.Client, response: httpx.Response) -> httpx.Client:
    assert response.status_code == 200, response.text
    http.headers["X-CSRF-Token"] = response.json()["csrf"]
    return http


@pytest.fixture
def bob(hub):
    http = client(hub)
    signed_in(http, http.post(f"/api/links/invite/{invite(hub)}", json={"username": "bob", "password": PASSWORD}))
    yield http
    http.close()


@pytest.fixture
def agent_for(hub, tmp_path):
    """Pair and run an agent for a device owned by whoever `http` is signed in as."""
    runners = []

    def start(http, name, root):
        root.mkdir(parents=True, exist_ok=True)
        device = http.post("/api/devices", json={"name": name, "platform": LOCAL_PLATFORM, "roots": [str(root)]})
        assert device.status_code == 200, device.text
        config = pair_device(hub.url, device.json()["code"], [str(root)], tmp_path / f"agent-{name}" / "config.json",
                             proxy="none")
        runner = AgentThread(config)
        runner.start()
        runners.append(runner)
        device_id = device.json()["device_id"]
        hub.wait_for(lambda: hub.runtime.agents.is_online(device_id), what=f"agent {name} online")
        return device_id

    start.runners = runners
    yield start
    for runner in runners:
        runner.stop()


def test_users_only_see_their_own_devices_projects_tokens_and_audit(hub, start_agent, agent_for, bob, tmp_path):
    admin_device, _ = start_agent("admin-pc", tmp_path / "admin")
    (tmp_path / "admin" / "secret.txt").write_text("admin only", encoding="utf-8")
    hub.add_project("app", admin_device, tmp_path / "admin")
    admin_project = hub.panel.get("/api/projects").json()["projects"][0]["id"]
    admin_token = hub.create_token()

    bob_device = agent_for(bob, "bob-pc", tmp_path / "bob")
    (tmp_path / "bob" / "notes.txt").write_text("bob's notes", encoding="utf-8")
    same_alias = bob.post("/api/projects", json={"alias": "APP", "device_id": bob_device,
                                                 "root": str(tmp_path / "bob")})
    assert same_alias.status_code == 200, same_alias.text  # aliases are unique per user, not globally

    assert [d["name"] for d in bob.get("/api/devices").json()["devices"]] == ["bob-pc"]
    assert [d["name"] for d in hub.panel.get("/api/devices").json()["devices"]] == ["admin-pc"]
    assert [p["device_name"] for p in bob.get("/api/projects").json()["projects"]] == ["bob-pc"]
    assert bob.get("/api/tokens").json()["tokens"] == []
    assert bob.get("/api/overview").json()["devices"] == {"total": 1, "online": 1}

    for method, path, body in (("PATCH", f"/api/devices/{admin_device}", {"enabled": False}),
                               ("DELETE", f"/api/devices/{admin_device}", None),
                               ("GET", f"/api/devices/{admin_device}/browse", None),
                               ("POST", f"/api/devices/{admin_device}/pairing-code", None),
                               ("POST", f"/api/devices/{admin_device}/roots-command", {"roots": []}),
                               ("PATCH", f"/api/projects/{admin_project}", {"mode": "ro"}),
                               ("DELETE", f"/api/projects/{admin_project}", None),
                               ("POST", "/api/projects", {"alias": "steal", "device_id": admin_device,
                                                          "root": str(tmp_path / "admin")})):
        response = bob.request(method, path, json=body)
        assert response.status_code == 404, (method, path, response.text)
    stolen = bob.post("/api/tokens", json={"label": "x", "scopes": ["read"], "projects": [admin_project]})
    assert stolen.status_code == 400 and stolen.json()["error"]["code"] == "INVALID_PROJECTS"
    assert hub.device(admin_device)["enabled"] is True

    bob_token = bob.post("/api/tokens", json={"label": "bob", "scopes": ["read", "write"], "projects": "*"}
                         ).json()["token"]
    listing = hub.ok(bob_token, "list_projects")
    assert "APP" in listing and "bob-pc" in listing and "admin-pc" not in listing
    assert "bob's notes" in hub.ok(bob_token, "read_file", project="app", path="notes.txt")
    text, is_error = hub.call(bob_token, "read_file", project="app", path="secret.txt")
    assert is_error and "admin only" not in text
    assert "admin only" in hub.ok(admin_token, "read_file", project="app", path="secret.txt")
    assert "bob-pc" not in hub.ok(admin_token, "list_projects")

    bob_audit = bob.get("/api/audit").json()["entries"]
    assert {"device.created", "device.paired", "project.created", "read_file"} <= {e["action"] for e in bob_audit}
    assert all(e["user_id"] == bob_audit[0]["user_id"] for e in bob_audit)
    admin_audit = hub.panel.get("/api/audit").json()["entries"]
    assert not any("bob" in e["actor"] or e["target"] == bob_device for e in admin_audit)


def test_oauth_consent_grants_access_to_the_approving_users_projects(hub, start_agent, agent_for, bob, tmp_path):
    admin_device, _ = start_agent("admin-pc", tmp_path / "admin")
    hub.add_project("admin-app", admin_device, tmp_path / "admin")
    bob_device = agent_for(bob, "bob-pc", tmp_path / "bob")
    bob.post("/api/projects", json={"alias": "bob-app", "device_id": bob_device, "root": str(tmp_path / "bob")}
             ).raise_for_status()

    redirect = "https://chatgpt.com/connector_platform_oauth_redirect"
    client_id = hub.http.post("/oauth/register", json={"client_name": "ChatGPT", "redirect_uris": [redirect]}
                              ).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    authorize = hub.http.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "state": "s",
        "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256", "resource": hub.url})
    request_id = authorize.headers["location"].rsplit("/", 1)[1]
    details = bob.get(f"/api/oauth/requests/{request_id}").json()
    assert details["username"] == "bob" and [p["alias"] for p in details["projects"]] == ["bob-app"]
    decision = bob.post(f"/api/oauth/requests/{request_id}", json={"approve": True, "scopes": ["read"],
                                                                   "projects": "*"}).json()
    code = parse_qs(urlsplit(decision["redirect_to"]).query)["code"][0]
    tokens = hub.http.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                 "client_id": client_id, "redirect_uri": redirect,
                                                 "code_verifier": verifier, "resource": hub.url}).json()
    listing = hub.ok(tokens["access_token"], "list_projects")
    assert "bob-app" in listing and "admin-app" not in listing
    assert [t["kind"] for t in bob.get("/api/tokens").json()["tokens"]] == ["oauth"]
    assert hub.panel.get("/api/tokens").json()["tokens"] == []

    hub.panel.patch(f"/api/users/{user_id(hub, 'bob')}", json={"disabled": True}).raise_for_status()
    refreshed = hub.http.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                    "refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 400 and refreshed.json()["error"] == "invalid_grant"


def user_id(hub, username: str) -> str:
    return next(u["id"] for u in hub.panel.get("/api/users").json()["users"] if u["username"] == username)


def test_the_last_login_outlives_the_session(hub, bob):
    """Logging out deletes the session; the users list still shows when bob last signed in."""
    bob.post("/api/logout").raise_for_status()
    bob_id = user_id(hub, "bob")
    assert hub.runtime.store.one("SELECT count(*) AS n FROM sessions WHERE user_id=?", (bob_id,))["n"] == 0
    users = {u["username"]: u for u in hub.panel.get("/api/users").json()["users"]}
    assert users["bob"]["last_login"] > users["bob"]["created"] - 1 and users["admin"]["last_login"] is not None


def test_only_administrators_manage_users(hub, bob):
    for method, path, body in (("GET", "/api/users", None), ("POST", "/api/users/invites", {"note": "x"}),
                               ("PATCH", f"/api/users/{user_id(hub, 'admin')}", {"disabled": True}),
                               ("POST", f"/api/users/{user_id(hub, 'admin')}/reset-link", None),
                               ("DELETE", f"/api/users/{user_id(hub, 'admin')}", None)):
        response = bob.request(method, path, json=body)
        assert response.status_code == 403 and response.json()["error"]["code"] == "ADMIN_REQUIRED", path
    assert bob.get("/api/session").json()["role"] == "user"

    me = user_id(hub, "admin")
    assert hub.panel.patch(f"/api/users/{me}", json={"disabled": True}).json()["error"]["code"] == "SELF_CHANGE"
    assert hub.panel.delete(f"/api/users/{me}").json()["error"]["code"] == "SELF_CHANGE"
    hub.panel.patch(f"/api/users/{user_id(hub, 'bob')}", json={"role": "admin"}).raise_for_status()
    assert bob.get("/api/users").status_code == 200  # the role applies to the existing session
    hub.panel.patch(f"/api/users/{user_id(hub, 'bob')}", json={"role": "user"}).raise_for_status()
    assert bob.get("/api/users").status_code == 403


def test_invite_links(hub):
    token = invite(hub, note="carol")
    assert client(hub).get(f"/api/links/invite/{token}").json()["note"] == "carol"
    invites = hub.panel.get("/api/users").json()["invites"]
    assert [i["note"] for i in invites] == ["carol"]
    http = client(hub)
    for username, password, status in (("ADMIN", "long-enough-pw", 409), ("-bad", "long-enough-pw", 400),
                                       ("carol", "short", 400)):
        failed = http.post(f"/api/links/invite/{token}", json={"username": username, "password": password})
        assert failed.status_code == status, failed.text  # failures do not use the link up
    signed_in(http, http.post(f"/api/links/invite/{token}", json={"username": "carol", "password": "carols-pw-123"}))
    assert http.get("/api/session").json()["username"] == "carol"
    reused = client(hub).post(f"/api/links/invite/{token}", json={"username": "dave", "password": "daves-pw-1234"})
    assert reused.status_code == 404 and reused.json()["error"]["code"] == "LINK_INVALID"
    assert hub.panel.get("/api/users").json()["invites"] == []

    revoked = invite(hub, note="erin")
    link_id = hub.panel.get("/api/users").json()["invites"][0]["id"]
    hub.panel.delete(f"/api/users/invites/{link_id}").raise_for_status()
    assert client(hub).get(f"/api/links/invite/{revoked}").status_code == 404
    expired = invite(hub, note="frank")
    hub.runtime.store.execute("UPDATE links SET expires=? WHERE token_hash=?", (time.time() - 1, sha256_hex(expired)))
    assert client(hub).get(f"/api/links/invite/{expired}").status_code == 404
    assert client(hub).get("/api/links/reset/" + expired).status_code == 404  # wrong kind


def test_names_are_unique_whatever_their_case_in_the_database_too(hub):
    hub.runtime.auth.create_user("Dave", "daves-own-password")
    with pytest.raises(sqlite3.IntegrityError):  # as login finds names: one account per name, whatever the case
        hub.runtime.store.execute("INSERT INTO users(id, username, password_hash, created, role) "
                                  "VALUES ('u_x', 'DAVE', 'x', 0, 'user')")


def test_the_reset_password_command_finds_names_as_login_does(hub):
    hub.runtime.auth.create_user("Carol", "carols-first-password")
    hub.runtime.auth.set_password("carol", "carols-second-password")  # reset-password --username carol
    assert client(hub).post("/api/login", json={"username": "CAROL", "password": "carols-second-password"}
                            ).status_code == 200


def test_a_new_password_voids_an_unused_reset_link(hub, bob):
    """A reset link from before would set the password once more: gone once the password changed otherwise."""
    token = hub.panel.post(f"/api/users/{user_id(hub, 'bob')}/reset-link").json()["url"].rsplit("/", 1)[1]
    hub.runtime.auth.set_password("bob", "bobs-changed-password")  # reset-password on the hub, say
    assert client(hub).get(f"/api/links/reset/{token}").status_code == 404
    assert client(hub).post(f"/api/links/reset/{token}", json={"password": "somebody-elses"}).status_code == 404
    login = client(hub).post("/api/login", json={"username": "bob", "password": "bobs-changed-password"})
    assert login.status_code == 200


def test_reset_link_replaces_the_password_and_signs_out_everywhere(hub, bob):
    bob_id = user_id(hub, "bob")
    first = hub.panel.post(f"/api/users/{bob_id}/reset-link").json()
    second = hub.panel.post(f"/api/users/{bob_id}/reset-link").json()  # replaces the first one
    assert client(hub).get(first["url"].replace("/#/", "/api/links/")).status_code == 404
    token = second["url"].rsplit("/", 1)[1]
    assert client(hub).get(f"/api/links/reset/{token}").json()["username"] == "bob"
    http = client(hub)
    signed_in(http, http.post(f"/api/links/reset/{token}", json={"password": "bobs-new-password"}))
    assert bob.get("/api/devices").status_code == 401  # the old session is gone
    assert http.get("/api/devices").status_code == 200
    login = client(hub).post("/api/login", json={"username": "bob", "password": PASSWORD})
    assert login.status_code == 401
    assert client(hub).post("/api/login", json={"username": "Bob", "password": "bobs-new-password"}).status_code == 200
    assert client(hub).post(f"/api/links/reset/{token}", json={"password": "another-password"}).status_code == 404
    hub.panel.patch(f"/api/users/{bob_id}", json={"disabled": True}).raise_for_status()
    third = hub.panel.post(f"/api/users/{bob_id}/reset-link").json()["url"].rsplit("/", 1)[1]
    refused = client(hub).post(f"/api/links/reset/{third}", json={"password": "while-disabled"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "USER_DISABLED"
    assert client(hub).get(f"/api/links/reset/{third}").status_code == 200  # kept for when bob is back
    hub.panel.patch(f"/api/users/{bob_id}", json={"disabled": False}).raise_for_status()

    mine = hub.panel.post("/api/account/password", json={"current_password": "wrong", "new_password": "x" * 12})
    assert mine.status_code == 403
    changed = hub.panel.post("/api/account/password", json={"current_password": ADMIN_PASSWORD,
                                                            "new_password": "admins-new-password"})
    hub.panel.headers["X-CSRF-Token"] = changed.json()["csrf"]
    assert hub.panel.get("/api/users").status_code == 200  # the fresh session cookie replaced the old one


def test_disabling_and_deleting_a_user(hub, agent_for, bob, tmp_path):
    bob_device = agent_for(bob, "bob-pc", tmp_path / "bob")
    bob.post("/api/projects", json={"alias": "app", "device_id": bob_device, "root": str(tmp_path / "bob")}
             ).raise_for_status()
    token = bob.post("/api/tokens", json={"label": "t", "scopes": ["read"], "projects": "*"}).json()["token"]
    assert "app" in hub.ok(token, "list_projects")
    bob_id = user_id(hub, "bob")

    hub.panel.patch(f"/api/users/{bob_id}", json={"disabled": True}).raise_for_status()
    assert bob.get("/api/devices").status_code == 401
    assert hub.rpc(token, "tools/list").status_code == 401
    hub.wait_for(lambda: not hub.runtime.agents.is_online(bob_device), what="bob's agent disconnected")
    login = client(hub).post("/api/login", json={"username": "bob", "password": PASSWORD})
    assert login.status_code == 403 and login.json()["error"]["code"] == "USER_DISABLED"
    assert [u["disabled"] for u in hub.panel.get("/api/users").json()["users"] if u["id"] == bob_id] == [True]

    hub.panel.patch(f"/api/users/{bob_id}", json={"disabled": False}).raise_for_status()
    assert "app" in hub.ok(token, "list_projects")  # tokens were suspended, not revoked
    hub.panel.delete(f"/api/users/{bob_id}").raise_for_status()
    assert hub.rpc(token, "tools/list").status_code == 401
    assert client(hub).post("/api/login", json={"username": "bob", "password": PASSWORD}).status_code == 401
    store = hub.runtime.store
    for table in ("devices", "projects", "grants", "audit", "sessions"):
        assert store.one(f"SELECT count(*) AS n FROM {table} WHERE user_id=?", (bob_id,))["n"] == 0, table
    assert store.one("SELECT count(*) AS n FROM tokens")["n"] == 0


def test_deleting_a_user_retires_their_agents(hub, agent_for, bob, tmp_path):
    agent_for(bob, "bob-pc", tmp_path / "bob")
    runner = agent_for.runners[-1]
    hub.panel.delete(f"/api/users/{user_id(hub, 'bob')}").raise_for_status()
    runner.thread.join(20)
    assert not runner.thread.is_alive() and runner.removed_autostart == [runner.config.path]


V1_SCHEMA = """
CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
    created REAL NOT NULL);
CREATE TABLE sessions (id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE login_attempts (ip TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, secret_enc TEXT, enabled INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL, paired REAL, last_seen REAL, info TEXT NOT NULL DEFAULT '{}');
CREATE TABLE pairing_codes (code_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE, expires REAL NOT NULL);
CREATE TABLE projects (id TEXT PRIMARY KEY, alias TEXT NOT NULL, alias_key TEXT UNIQUE NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE, root TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('ro', 'rw')), description TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
CREATE TABLE grants (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('pat', 'oauth')), label TEXT NOT NULL,
    client_id TEXT, scopes TEXT NOT NULL, projects TEXT NOT NULL, resource TEXT,
    created REAL NOT NULL, expires REAL, revoked INTEGER NOT NULL DEFAULT 0, last_used REAL);
CREATE TABLE tokens (hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('pat', 'access', 'refresh')), expires REAL NOT NULL,
    created REAL NOT NULL, used REAL);
CREATE INDEX tokens_grant ON tokens(grant_id);
CREATE TABLE oauth_clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL,
    created REAL NOT NULL, last_used REAL);
CREATE TABLE oauth_requests (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, state TEXT,
    code_challenge TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE oauth_codes (hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL, scopes TEXT NOT NULL, projects TEXT NOT NULL, resource TEXT NOT NULL,
    expires REAL NOT NULL);
CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ok',
    detail TEXT NOT NULL DEFAULT '{}', duration_ms INTEGER);
"""


def test_single_user_database_is_upgraded(tmp_path):
    """A hub.db written by the single-user release (schema copied from a real deployment)."""
    data = tmp_path / "hub-data"
    data.mkdir()
    now = time.time()
    db = sqlite3.connect(data / "hub.db")
    db.executescript(V1_SCHEMA)
    db.execute("INSERT INTO users VALUES ('u_owner', 'admin', ?, ?)", (hash_password(ADMIN_PASSWORD), now))
    db.execute("INSERT INTO devices(id, name, created, info) VALUES ('dev_1', 'pc', ?, '{}')", (now,))
    db.execute("INSERT INTO projects VALUES ('prj_1', 'App', 'app', 'dev_1', 'C:/code', 'rw', '', ?)", (now,))
    db.execute("INSERT INTO grants(id, kind, label, scopes, projects, created, expires) "
               "VALUES ('g_1', 'pat', 'old', ?, '\"*\"', ?, ?)", (json.dumps(["read"]), now, now + 3600))
    db.execute("INSERT INTO tokens VALUES (?, 'g_1', 'pat', ?, ?, NULL)", (sha256_hex("wc_pat_old"), now + 3600, now))
    db.execute("INSERT INTO audit(ts, actor, action) VALUES (?, 'panel', 'panel.login')", (now,))
    db.execute("INSERT INTO oauth_codes VALUES ('h', 'c', 'r', 'cc', 's', 'p', 'res', ?)", (now + 60,))
    db.commit()
    db.close()

    settings = Settings(public_url="http://127.0.0.1:1", data_dir=data)
    for _ in range(2):  # the second open must be a no-op
        store = Store(data)
        assert store.one("SELECT role, disabled FROM users") == {"role": "admin", "disabled": 0}
        assert store.one("SELECT last_login FROM users")["last_login"] >= now  # from the audit log
        for table in ("devices", "projects", "grants", "audit"):
            assert store.one(f"SELECT user_id FROM {table}")["user_id"] == "u_owner", table
        assert store.one("SELECT value FROM meta WHERE key='schema'")["value"] == "2"
        assert store.one("SELECT count(*) AS n FROM oauth_codes")["n"] == 0
        assert store.one("PRAGMA foreign_keys")["foreign_keys"] == 1
        auth = Auth(store, settings)
        assert auth.login("127.0.0.1", "admin", ADMIN_PASSWORD)["role"] == "admin"
        principal, state = auth.principal_for_token("wc_pat_old")
        assert state == "ok" and principal.user_id == "u_owner" and principal.username == "admin"
        store.close()

    store = Store(data)
    Auth(store, settings).create_user("bob", PASSWORD)
    bob = store.one("SELECT id FROM users WHERE username='bob'")["id"]
    store.execute("INSERT INTO devices(id, name, created, user_id) VALUES ('dev_2', 'bob-pc', ?, ?)", (now, bob))
    store.execute("INSERT INTO projects(id, alias, alias_key, device_id, root, mode, created, user_id) "
                  "VALUES ('prj_2', 'app', 'app', 'dev_2', '/x', 'rw', ?, ?)", (now, bob))  # same alias, other user
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("INSERT INTO projects(id, alias, alias_key, device_id, root, mode, created, user_id) "
                      "VALUES ('prj_3', 'APP', 'app', 'dev_2', '/y', 'rw', ?, ?)", (now, bob))
    store.close()
