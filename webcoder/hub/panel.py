"""JSON API behind the web panel (cookie session + CSRF header), plus agent pairing.

Every signed-in user only ever sees and changes their own devices, projects, tokens,
OAuth grants and audit log. Administrators additionally manage user accounts.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from webcoder import VERSION
from webcoder.common.channel import seal_secret
from webcoder.common.crypto import b64e, normalize_pairing_code, pairing_code, sha256_hex
from webcoder.common.errors import WebCoderError
from webcoder.hub.agents import CLOSE_FORBIDDEN, CLOSE_UNAUTHORIZED
from webcoder.hub.auth import SESSION_COOKIE, ApiError
from webcoder.hub.clients import SUPPORTED_CLIENTS, setup_snippets
from webcoder.hub.collaboration.schema import purge_owner as purge_collaboration
from webcoder.hub.install import (
    PLATFORMS,
    install_commands,
    roots_command,
    uninstall_command,
    validate_hosts,
    validate_programs,
    validate_roots,
)
from webcoder.hub.oauth import client_ip
from webcoder.hub.plugin import PLUGIN_NAME, SKILL_NAME, build_plugin_zip, build_skill_zip

PAIRING_TTL = 900
AGENT_PLATFORMS = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}  # platform.system() -> installer
ProjectScope = Literal["*"] | list[str]
Platform = Literal["windows", "macos", "linux"]


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class DeviceSetup(BaseModel):
    platform: Platform = "windows"
    roots: list[str] = Field(default_factory=list, max_length=20)
    read_only: bool = False
    autostart: bool = True
    mobile: bool = False  # let the hub control phones attached to that computer (adb / hdc)


class DeviceBody(DeviceSetup):
    name: str = Field(min_length=1, max_length=80)


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None


class RootsBody(BaseModel):
    platform: Platform | None = None
    roots: list[str] = Field(default_factory=list, max_length=20)
    mobile: bool | None = None
    ssh_hosts: list[str] | None = Field(default=None, max_length=60)
    skills: bool | None = None
    programs: list[str] | None = Field(default=None, max_length=40)


class ProjectBody(BaseModel):
    alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    device_id: str = Field(min_length=1, max_length=64)
    root: str = Field(min_length=1, max_length=1024)
    mode: Literal["ro", "rw"] = "rw"
    description: str = Field(default="", max_length=500)


class ProjectPatch(BaseModel):
    mode: Literal["ro", "rw"] | None = None
    description: str | None = Field(default=None, max_length=500)


class TokenBody(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(min_length=1, max_length=5)
    projects: ProjectScope = "*"
    days: int = Field(default=90, ge=1, le=365)


class ConsentBody(BaseModel):
    approve: bool
    scopes: list[str] = Field(default_factory=list, max_length=5)
    projects: ProjectScope = "*"


class PairBody(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    hostname: str = Field(default="", max_length=200)
    platform: str = Field(default="", max_length=50)
    agent_version: str = Field(default="", max_length=50)
    key: str | None = Field(default=None, max_length=100)  # the agent's one-time key: the secret comes back sealed


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class InviteBody(BaseModel):
    note: str = Field(default="", max_length=100)
    days: int = Field(default=7, ge=1, le=30)


class AcceptInviteBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class ResetBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class UserPatch(BaseModel):
    disabled: bool | None = None
    role: Literal["admin", "user"] | None = None


def make_panel_router(rt) -> APIRouter:
    router = APIRouter()
    store, settings = rt.store, rt.settings

    def is_local_url() -> bool:
        return urlsplit(settings.public_url).hostname in ("127.0.0.1", "localhost", "::1")

    def session_response(payload: dict, cookie: str) -> JSONResponse:
        response = JSONResponse(payload)
        response.set_cookie(SESSION_COOKIE, cookie, max_age=settings.session_ttl, httponly=True, samesite="lax",
                            secure=settings.public_url.startswith("https://"), path="/")
        return response

    def audit(user: dict, action: str, target: str = "", **kwargs) -> None:
        rt.audit(f"面板:{user['username']}", action, target, user_id=user["user_id"], **kwargs)

    # ------------------------------------------------------------------ ownership helpers
    def own_device(device_id: str, user_id: str) -> dict:
        row = store.one("SELECT * FROM devices WHERE id=? AND user_id=?", (device_id, user_id))
        if not row:
            raise ApiError(404, "NOT_FOUND", "设备不存在")
        return row

    def unique_device_name(name: str, user_id: str, device_id: str | None = None) -> str:
        """The AI picks a computer for phones, SSH and programs by name: names must tell them apart."""
        name = name.strip()
        if not name:
            raise ApiError(400, "INVALID_NAME", "设备名称不能为空")
        taken = store.one("SELECT id FROM devices WHERE user_id=? AND lower(name)=lower(?) AND id!=?",
                          (user_id, name, device_id or ""))
        if taken:
            raise ApiError(409, "DEVICE_NAME_TAKEN", f"已有名为「{name}」的设备，请换一个名字")
        return name

    def own_project(project_id: str, user_id: str) -> dict:
        row = store.one("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, user_id))
        if not row:
            raise ApiError(404, "NOT_FOUND", "项目不存在")
        return row

    def device_rows(user_id: str) -> list[dict]:
        rows = store.all("SELECT d.id, d.name, d.enabled, d.created, d.paired, d.last_seen, d.info, "
                         "(SELECT count(*) FROM projects p WHERE p.device_id = d.id) AS projects "
                         "FROM devices d WHERE d.user_id=? ORDER BY d.created", (user_id,))
        for row in rows:
            row["info"] = json.loads(row["info"] or "{}")
            row["enabled"] = bool(row["enabled"])
            row["online"] = rt.agents.is_online(row["id"])
            row["status"] = ("disabled" if not row["enabled"] else "online" if row["online"]
                             else "pending" if not row["paired"] else "offline")
        return rows

    def project_rows(user_id: str) -> list[dict]:
        rows = store.all("SELECT p.*, d.name AS device_name FROM projects p JOIN devices d ON d.id = p.device_id "
                         "WHERE p.user_id=? ORDER BY p.alias_key", (user_id,))
        for row in rows:
            row["online"] = rt.agents.is_online(row["device_id"])
        return rows

    def check_projects(projects, user_id: str) -> None:
        if projects == "*":
            return
        mine = {row["id"] for row in store.all("SELECT id FROM projects WHERE user_id=?", (user_id,))}
        if not projects or not set(projects) <= mine:
            raise ApiError(400, "INVALID_PROJECTS", "请选择至少一个有效的项目，或选择“全部项目”")

    def device_setup(device: dict) -> dict:
        info = json.loads(device["info"] or "{}")
        setup = {"platform": "windows", "roots": [], "read_only": False, "autostart": True, "mobile": False,
                 **info.get("setup", {})}
        reported = AGENT_PLATFORMS.get(info.get("platform"))  # a connected agent knows its OS for sure
        return {**setup, "platform": reported} if reported else setup

    def save_setup(device_id: str, setup: dict) -> None:
        with store.transaction() as db:  # the agent's status updates write the same column
            row = db.execute("SELECT info FROM devices WHERE id=?", (device_id,)).fetchone()
            info = {**json.loads(row["info"] or "{}"), "setup": setup}
            db.execute("UPDATE devices SET info=? WHERE id=?", (json.dumps(info, ensure_ascii=False), device_id))

    def new_pairing(device_id: str, setup: dict) -> dict:
        code = pairing_code()
        expires = time.time() + PAIRING_TTL
        store.execute("DELETE FROM pairing_codes WHERE device_id=? OR expires<?", (device_id, time.time()))
        store.execute("INSERT INTO pairing_codes(code_hash, device_id, expires) VALUES (?, ?, ?)",
                      (sha256_hex(code), device_id, expires))
        commands = install_commands(settings.public_url, code, setup["platform"], setup["roots"], setup["read_only"],
                                    setup["autostart"], setup.get("mobile", False))
        return {"code": code, "expires": expires, "setup": setup, "commands": commands}

    def agent_error(exc: WebCoderError) -> ApiError:
        status = 409 if exc.code in ("DEVICE_OFFLINE", "AGENT_TIMEOUT", "AGENT_DISCONNECTED") else 400
        return ApiError(status, exc.code, exc.message)

    # ------------------------------------------------------------------ session + account
    @router.get("/api/session")
    async def session(request: Request):
        info = {"version": VERSION, "initialized": rt.auth.has_admin(), "public_url": settings.public_url,
                "mcp_url": settings.mcp_url, "https": settings.public_url.startswith("https://"),
                "local": is_local_url()}
        try:
            row = rt.auth.session(request)
        except ApiError:
            return {"authenticated": False, **info}
        return {"authenticated": True, "username": row["username"], "role": row["role"], "csrf": row["csrf"], **info}

    @router.post("/api/login")
    async def login(request: Request, body: LoginBody):
        result = rt.auth.login(client_ip(request), body.username, body.password)
        rt.audit(f"面板:{result['username']}", "panel.login", user_id=result["user_id"])
        return session_response({"authenticated": True, "username": result["username"], "role": result["role"],
                                 "csrf": result["csrf"]}, result["cookie"])

    @router.post("/api/logout")
    async def logout(request: Request):
        rt.auth.user(request, write=True)
        rt.auth.logout(request)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @router.post("/api/account/password")
    async def change_password(request: Request, body: PasswordBody):
        user = rt.auth.user(request, write=True)
        rt.auth.change_password(user["user_id"], body.current_password, body.new_password)
        audit(user, "account.password")
        started = rt.auth.start_session(user["user_id"])  # every old session was signed out
        return session_response({"authenticated": True, "username": user["username"], "role": user["role"],
                                 "csrf": started["csrf"]}, started["cookie"])

    @router.get("/api/overview")
    async def overview(request: Request):
        user = rt.auth.user(request)
        devices = device_rows(user["user_id"])
        return {"devices": {"total": len(devices), "online": sum(row["online"] for row in devices)},
                "projects": store.one("SELECT count(*) AS n FROM projects WHERE user_id=?", (user["user_id"],))["n"],
                "tokens": len(live_grants(user["user_id"])),  # counted as the token list shows them
                "recent": store.all("SELECT * FROM audit WHERE user_id=? ORDER BY id DESC LIMIT 10",
                                    (user["user_id"],))}

    # ------------------------------------------------------------------ devices
    @router.get("/api/devices")
    async def list_devices(request: Request):
        user = rt.auth.user(request)
        return {"devices": device_rows(user["user_id"]), "platforms": list(PLATFORMS),
                "uninstall": {platform: uninstall_command(platform) for platform in PLATFORMS}}

    @router.post("/api/devices")
    async def create_device(request: Request, body: DeviceBody):
        user = rt.auth.user(request, write=True)
        setup = {"platform": body.platform, "roots": validate_roots(body.roots, body.platform),
                 "read_only": body.read_only, "autostart": body.autostart, "mobile": body.mobile}
        device_id = "dev_" + uuid.uuid4().hex[:12]
        name = unique_device_name(body.name, user["user_id"])
        store.execute("INSERT INTO devices(id, name, created, user_id, info) VALUES (?, ?, ?, ?, ?)",
                      (device_id, name, time.time(), user["user_id"], json.dumps({"setup": setup}, ensure_ascii=False)))
        audit(user, "device.created", name, detail={"roots": setup["roots"]})
        return {"device_id": device_id, **new_pairing(device_id, setup)}

    @router.post("/api/devices/{device_id}/pairing-code")
    async def repair_device(device_id: str, request: Request, body: DeviceSetup | None = None):
        user = rt.auth.user(request, write=True)
        device = own_device(device_id, user["user_id"])
        setup = device_setup(device)
        if body is not None:  # the form's settings; the rest (servers, skills, programs) stays as it was
            setup = {**setup, "platform": body.platform, "roots": validate_roots(body.roots, body.platform),
                     "read_only": body.read_only, "autostart": body.autostart, "mobile": body.mobile}
            save_setup(device_id, setup)
        return {"device_id": device_id, **new_pairing(device_id, setup)}

    @router.post("/api/devices/{device_id}/roots-command")
    async def device_roots_command(device_id: str, request: Request, body: RootsBody):
        user = rt.auth.user(request, write=True)
        device = own_device(device_id, user["user_id"])
        setup = device_setup(device)
        platform = body.platform or setup["platform"]
        roots = validate_roots(body.roots, platform)
        mobile = setup["mobile"] if body.mobile is None else body.mobile
        hosts = None if body.ssh_hosts is None else validate_hosts(body.ssh_hosts)
        programs = None if body.programs is None else validate_programs(body.programs)
        save_setup(device_id, {**setup, "platform": platform, "roots": roots, "mobile": mobile,
                               **({"ssh_hosts": hosts} if hosts is not None else {}),
                               **({"skills": body.skills} if body.skills is not None else {}),
                               **({"programs": programs} if programs is not None else {})})
        return {"command": roots_command(platform, roots, mobile, hosts, body.skills, programs),
                "platform": platform, "roots": roots, "mobile": mobile, "ssh_hosts": hosts, "skills": body.skills,
                "programs": programs}

    @router.patch("/api/devices/{device_id}")
    async def update_device(device_id: str, request: Request, body: DevicePatch):
        user = rt.auth.user(request, write=True)
        own_device(device_id, user["user_id"])
        if body.name is not None:
            store.execute("UPDATE devices SET name=? WHERE id=?",
                          (unique_device_name(body.name, user["user_id"], device_id), device_id))
        if body.enabled is not None:
            store.execute("UPDATE devices SET enabled=? WHERE id=?", (int(body.enabled), device_id))
            if not body.enabled:
                await rt.agents.disconnect(device_id, CLOSE_FORBIDDEN, "disabled")
        audit(user, "device.updated", device_id, detail=body.model_dump(exclude_none=True))
        return {"ok": True}

    def remove_devices(db, devices: list[dict]) -> None:
        """Delete devices, keeping a tombstone (with the old secret) so their agents can be told, provably,
        to stop and remove their autostart entry, even if they only come back online later."""
        now = time.time()
        for device in devices:
            if device["secret_enc"]:
                db.execute("INSERT OR REPLACE INTO deleted_devices(id, user_id, secret_enc, deleted) "
                           "VALUES (?, ?, ?, ?)", (device["id"], device["user_id"], device["secret_enc"], now))
            db.execute("DELETE FROM projects WHERE device_id=?", (device["id"],))
            db.execute("DELETE FROM pairing_codes WHERE device_id=?", (device["id"],))
            db.execute("DELETE FROM devices WHERE id=?", (device["id"],))

    @router.delete("/api/devices/{device_id}")
    async def delete_device(device_id: str, request: Request):
        user = rt.auth.user(request, write=True)
        device = own_device(device_id, user["user_id"])
        with store.transaction() as db:
            remove_devices(db, [device])
        await rt.agents.retire(device_id)
        audit(user, "device.deleted", device["name"])
        return {"ok": True}

    @router.get("/api/devices/{device_id}/browse")
    async def browse(device_id: str, request: Request, path: str = ""):
        user = rt.auth.user(request)
        own_device(device_id, user["user_id"])
        try:
            return await rt.agents.call(device_id, "agent.browse", {"path": path or None}, 15.0)
        except WebCoderError as exc:
            raise agent_error(exc) from None

    @router.post("/api/agent/pair")
    async def pair(request: Request, body: PairBody):
        rt.limiter.check("pair:" + client_ip(request), 10, 600)
        if body.key is None and not settings.public_url.startswith("https://") and not is_local_url():
            raise ApiError(400, "INSECURE_PAIRING", "这个 Hub 通过明文 HTTP 服务，旧版 Agent 配对会明文收到设备密钥："
                           "请用面板里最新的安装命令重新安装 Agent（新版会加密配对）")
        if body.key is not None:
            try:
                seal_secret(body.key, "check", "check")  # a key that can be used, before the code is spent
            except ValueError:
                raise ApiError(400, "INVALID_KEY", "配对请求里的密钥无效") from None
        now = time.time()
        code_hash = sha256_hex(normalize_pairing_code(body.code))
        row = store.one("SELECT * FROM pairing_codes WHERE code_hash=? AND expires>?", (code_hash, now))
        if not row:
            raise ApiError(400, "INVALID_CODE", "配对码无效或已过期，请在面板中重新生成")
        device = store.one("SELECT * FROM devices WHERE id=?", (row["device_id"],))
        secret = b64e(os.urandom(32))
        info = {**json.loads(device["info"] or "{}"), "hostname": body.hostname, "platform": body.platform,
                "agent_version": body.agent_version}
        with store.transaction() as db:  # the code is used once: the one pairing that consumes it gets the secret
            if db.execute("DELETE FROM pairing_codes WHERE code_hash=? AND expires>?", (code_hash, now)).rowcount != 1:
                raise ApiError(400, "INVALID_CODE", "配对码无效或已过期，请在面板中重新生成")
            db.execute("DELETE FROM pairing_codes WHERE device_id=?", (row["device_id"],))
            db.execute("UPDATE devices SET secret_enc=?, paired=?, info=? WHERE id=?",
                       (store.encrypt(secret), now, json.dumps(info, ensure_ascii=False), row["device_id"]))
        await rt.agents.disconnect(row["device_id"], CLOSE_UNAUTHORIZED, "re-paired")
        rt.audit(f"设备:{device['name']}", "device.paired", body.hostname or device["id"], user_id=device["user_id"])
        sent = seal_secret(body.key, secret, device["id"]) if body.key is not None else {"device_secret": secret}
        return {"device_id": device["id"], **sent, "device_name": device["name"], "hub_url": settings.public_url}

    # ------------------------------------------------------------------ projects
    @router.get("/api/projects")
    async def list_projects(request: Request):
        user = rt.auth.user(request)
        return {"projects": project_rows(user["user_id"])}

    @router.post("/api/projects")
    async def create_project(request: Request, body: ProjectBody):
        user = rt.auth.user(request, write=True)
        if store.one("SELECT id FROM projects WHERE user_id=? AND alias_key=?", (user["user_id"], body.alias.lower())):
            raise ApiError(409, "ALIAS_TAKEN", f"项目名 {body.alias} 已被使用")
        device = own_device(body.device_id, user["user_id"])
        if not rt.agents.is_online(body.device_id):
            raise ApiError(409, "DEVICE_OFFLINE", "设备不在线，无法校验目录。请先在该设备上运行 Agent")
        try:
            checked = await rt.agents.call(body.device_id, "agent.check_root", {"root": body.root}, 15.0)
        except WebCoderError as exc:
            raise agent_error(exc) from None
        project_id = "prj_" + uuid.uuid4().hex[:12]
        store.execute("INSERT INTO projects(id, alias, alias_key, device_id, root, mode, description, created, "
                      "user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (project_id, body.alias, body.alias.lower(), body.device_id, checked["root"], body.mode,
                       body.description, time.time(), user["user_id"]))
        audit(user, "project.created", f"{body.alias} → {device['name']}:{checked['root']}")
        return {"id": project_id, "root": checked["root"]}

    @router.patch("/api/projects/{project_id}")
    async def update_project(project_id: str, request: Request, body: ProjectPatch):
        user = rt.auth.user(request, write=True)
        own_project(project_id, user["user_id"])
        if body.mode is not None:
            store.execute("UPDATE projects SET mode=? WHERE id=?", (body.mode, project_id))
        if body.description is not None:
            store.execute("UPDATE projects SET description=? WHERE id=?", (body.description, project_id))
        audit(user, "project.updated", project_id, detail=body.model_dump(exclude_none=True))
        return {"ok": True}

    @router.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, request: Request):
        user = rt.auth.user(request, write=True)
        own_project(project_id, user["user_id"])
        store.execute("DELETE FROM projects WHERE id=?", (project_id,))
        audit(user, "project.deleted", project_id)
        return {"ok": True}

    # ------------------------------------------------------------------ tokens / grants
    def live_grants(user_id: str) -> list[dict]:
        """Tokens and OAuth grants still usable: a PAT until it expires, an OAuth grant until its refresh token
        does (the grant itself has no expiry)."""
        now = time.time()
        rows = store.all(
            "SELECT g.id, g.kind, g.label, g.client_id, g.scopes, g.projects, g.created, g.expires, g.last_used, "
            "(SELECT max(t.expires) FROM tokens t WHERE t.grant_id = g.id AND t.kind='refresh' AND t.used IS NULL) "
            "AS refresh_expires FROM grants g WHERE g.user_id=? AND g.revoked=0 ORDER BY g.created DESC", (user_id,))
        live = []
        for row in rows:
            expires = row["expires"] if row["kind"] == "pat" else row["refresh_expires"]
            if expires is None or expires > now:
                live.append({**row, "expires": expires})
        return live

    @router.get("/api/tokens")
    async def list_tokens(request: Request):
        user = rt.auth.user(request)
        names = {row["id"]: row["alias"] for row in store.all("SELECT id, alias FROM projects WHERE user_id=?",
                                                              (user["user_id"],))}
        result = []
        for row in live_grants(user["user_id"]):
            projects = json.loads(row["projects"])
            result.append({**row, "scopes": json.loads(row["scopes"]),
                           "projects": projects if projects == "*" else [names.get(p, "(已删除)") for p in projects]})
        return {"tokens": result}

    @router.post("/api/tokens")
    async def create_token(request: Request, body: TokenBody):
        user = rt.auth.user(request, write=True)
        check_projects(body.projects, user["user_id"])
        result = rt.auth.issue_pat(user["user_id"], body.label.strip(), body.scopes, body.projects, body.days)
        audit(user, "token.created", body.label.strip(), detail={"scopes": result["scopes"]})
        return {**result, "snippets": setup_snippets(settings.public_url, result["token"])}

    @router.delete("/api/tokens/{grant_id}")
    async def revoke_token(grant_id: str, request: Request):
        user = rt.auth.user(request, write=True)
        if not store.one("SELECT id FROM grants WHERE id=? AND user_id=?", (grant_id, user["user_id"])):
            raise ApiError(404, "NOT_FOUND", "授权不存在")
        rt.auth.revoke_grant(grant_id)
        audit(user, "token.revoked", grant_id)
        return {"ok": True}

    # ------------------------------------------------------------------ OAuth consent
    @router.get("/api/oauth/requests/{request_id}")
    async def consent_details(request_id: str, request: Request):
        user = rt.auth.user(request)
        details = rt.oauth.request_details(request_id)
        return {**details, "username": user["username"],
                "projects": [{"id": row["id"], "alias": row["alias"], "device_name": row["device_name"]}
                             for row in project_rows(user["user_id"])]}

    @router.post("/api/oauth/requests/{request_id}")
    async def consent_decision(request_id: str, request: Request, body: ConsentBody):
        user = rt.auth.user(request, write=True)
        if not body.approve:
            audit(user, "oauth.denied", request_id)
            return {"redirect_to": rt.oauth.deny(request_id)}
        check_projects(body.projects, user["user_id"])
        location = rt.oauth.approve(request_id, user["user_id"], body.scopes, body.projects)
        audit(user, "oauth.approved", request_id, detail={"scopes": body.scopes})
        return {"redirect_to": location}

    # ------------------------------------------------------------------ clients, plugin, audit
    @router.get("/api/clients")
    async def clients(request: Request):
        rt.auth.user(request)
        return {"clients": [{"id": name, "title": SUPPORTED_CLIENTS[name]["title"],
                             "surfaces": SUPPORTED_CLIENTS[name]["surfaces"]} for name in settings.clients],
                "snippets": setup_snippets(settings.public_url), "https": settings.public_url.startswith("https://"),
                "local": is_local_url()}

    @router.get("/api/chatgpt-plugin.zip")
    async def chatgpt_plugin(request: Request, app_id: str = ""):
        user = rt.auth.user(request)
        try:
            data = await asyncio.to_thread(build_plugin_zip, settings.public_url, app_id.strip() or None)
        except ValueError as exc:
            raise ApiError(400, "INVALID_APP_ID", str(exc)) from None
        audit(user, "plugin.downloaded", settings.mcp_url)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{PLUGIN_NAME}-{VERSION}.zip"'})

    @router.get("/api/chatgpt-skill.zip")
    async def chatgpt_skill(request: Request):
        rt.auth.user(request)
        return Response(build_skill_zip(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{SKILL_NAME}-skill.zip"'})

    @router.get("/api/audit")
    async def audit_log(request: Request, limit: int = 200):
        user = rt.auth.user(request)
        return {"entries": store.all("SELECT * FROM audit WHERE user_id=? ORDER BY id DESC LIMIT ?",
                                     (user["user_id"], max(1, min(limit, 1000))))}

    # ------------------------------------------------------------------ users (administrators)
    @router.get("/api/users")
    async def list_users(request: Request):
        rt.auth.admin(request)
        users = store.all(
            "SELECT u.id, u.username, u.role, u.disabled, u.created, u.last_login, "
            "(SELECT count(*) FROM devices d WHERE d.user_id = u.id) AS devices, "
            "(SELECT count(*) FROM projects p WHERE p.user_id = u.id) AS projects "
            "FROM users u ORDER BY u.created")
        invites = store.all("SELECT id, note, created, expires FROM links WHERE kind='invite' AND used IS NULL "
                            "AND expires>? ORDER BY created DESC", (time.time(),))
        return {"users": [{**user, "disabled": bool(user["disabled"])} for user in users], "invites": invites}

    @router.post("/api/users/invites")
    async def create_invite(request: Request, body: InviteBody):
        admin = rt.auth.admin(request, write=True)
        link = rt.auth.create_link("invite", admin["user_id"], note=body.note.strip(), days=body.days)
        audit(admin, "user.invited", body.note.strip())
        return link

    @router.delete("/api/users/invites/{link_id}")
    async def delete_invite(link_id: str, request: Request):
        admin = rt.auth.admin(request, write=True)
        store.execute("DELETE FROM links WHERE id=? AND kind='invite' AND used IS NULL", (link_id,))
        audit(admin, "user.invite_revoked", link_id)
        return {"ok": True}

    def target_user(user_id: str) -> dict:
        row = store.one("SELECT * FROM users WHERE id=?", (user_id,))
        if not row:
            raise ApiError(404, "NOT_FOUND", "用户不存在")
        return row

    def active_admins() -> int:
        return store.one("SELECT count(*) AS n FROM users WHERE role='admin' AND disabled=0")["n"]

    async def disconnect_user_devices(user_id: str) -> None:
        for row in store.all("SELECT id FROM devices WHERE user_id=?", (user_id,)):
            await rt.agents.disconnect(row["id"], CLOSE_FORBIDDEN, "user disabled")

    @router.patch("/api/users/{user_id}")
    async def update_user(user_id: str, request: Request, body: UserPatch):
        admin = rt.auth.admin(request, write=True)
        target = target_user(user_id)
        if user_id == admin["user_id"]:
            raise ApiError(400, "SELF_CHANGE", "不能停用自己或修改自己的角色")
        losing_admin = target["role"] == "admin" and not target["disabled"] and (
            body.disabled is True or body.role == "user")
        if losing_admin and active_admins() <= 1:
            raise ApiError(400, "LAST_ADMIN", "至少要保留一名可用的管理员")
        if body.role is not None:
            store.execute("UPDATE users SET role=? WHERE id=?", (body.role, user_id))
        if body.disabled is not None:
            store.execute("UPDATE users SET disabled=? WHERE id=?", (int(body.disabled), user_id))
            if body.disabled:
                store.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                await disconnect_user_devices(user_id)
        audit(admin, "user.updated", target["username"], detail=body.model_dump(exclude_none=True))
        return {"ok": True}

    @router.post("/api/users/{user_id}/reset-link")
    async def reset_link(user_id: str, request: Request):
        admin = rt.auth.admin(request, write=True)
        target = target_user(user_id)
        link = rt.auth.create_link("reset", admin["user_id"], user_id=user_id, days=1)
        audit(admin, "user.reset_link", target["username"])
        return link

    @router.delete("/api/users/{user_id}")
    async def delete_user(user_id: str, request: Request):
        admin = rt.auth.admin(request, write=True)
        target = target_user(user_id)
        if user_id == admin["user_id"]:
            raise ApiError(400, "SELF_CHANGE", "不能删除自己")
        if target["role"] == "admin" and not target["disabled"] and active_admins() <= 1:
            raise ApiError(400, "LAST_ADMIN", "至少要保留一名可用的管理员")
        devices = store.all("SELECT * FROM devices WHERE user_id=?", (user_id,))
        with store.transaction() as db:
            purge_collaboration(db, user_id)  # rooms, messages, tasks, subscriptions ... of this account
            db.execute("DELETE FROM tokens WHERE grant_id IN (SELECT id FROM grants WHERE user_id=?)", (user_id,))
            db.execute("DELETE FROM grants WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM projects WHERE user_id=?", (user_id,))
            remove_devices(db, devices)
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM oauth_codes WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM links WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM audit WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
        for device in devices:
            await rt.agents.retire(device["id"])
        audit(admin, "user.deleted", target["username"])
        return {"ok": True}

    # ------------------------------------------------------------------ invite / reset links (public)
    def link_limit(request: Request) -> None:
        rt.limiter.check("link:" + client_ip(request), 30, 600)

    @router.get("/api/links/invite/{token}")
    async def peek_invite(token: str, request: Request):
        link_limit(request)
        link = rt.auth.peek_link(token, "invite")
        return {"note": link["note"], "expires": link["expires"]}

    @router.post("/api/links/invite/{token}")
    async def accept_invite(token: str, request: Request, body: AcceptInviteBody):
        link_limit(request)
        user_id = rt.auth.accept_invite(token, body.username, body.password)
        user = store.one("SELECT username, role FROM users WHERE id=?", (user_id,))
        rt.audit(f"面板:{user['username']}", "user.joined", user_id=user_id)
        started = rt.auth.start_session(user_id)
        return session_response({"authenticated": True, "username": user["username"], "role": user["role"],
                                 "csrf": started["csrf"]}, started["cookie"])

    @router.get("/api/links/reset/{token}")
    async def peek_reset(token: str, request: Request):
        link_limit(request)
        link = rt.auth.peek_link(token, "reset")
        return {"username": link["username"], "expires": link["expires"]}

    @router.post("/api/links/reset/{token}")
    async def use_reset(token: str, request: Request, body: ResetBody):
        link_limit(request)
        user_id = rt.auth.use_reset_link(token, body.password)
        user = store.one("SELECT username, role FROM users WHERE id=?", (user_id,))
        rt.audit(f"面板:{user['username']}", "account.password_reset", user_id=user_id)
        started = rt.auth.start_session(user_id)
        return session_response({"authenticated": True, "username": user["username"], "role": user["role"],
                                 "csrf": started["csrf"]}, started["cookie"])

    return router
