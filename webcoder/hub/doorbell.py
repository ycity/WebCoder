"""The dot doorbell: one discussion room where `webcoder dot` mentions the owner's dot chat.

dot wakes only for events it subscribed to itself, and a mention subscription names one room, project and slot. So
the command line keeps one fixed room (on a small folder of the computer that set it up) with one dot slot: dot joins
and subscribes once, and each request names the project to work on, which dot reaches with its own WebCoder
authorization. The CLI acts as the room's owner, the same as the signed-in panel user.
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from webcoder.hub.auth import ApiError
from webcoder.hub.collaboration.common import MESSAGE_EVENT
from webcoder.hub.collaboration.host import OWNER_CAPABILITIES, Principal

LABEL = "WebCoder 门铃"
ENVIRONMENT = "production"
SLOT_DAYS = 30            # the longest a slot may live; dot joins again after that
CODE_MINUTES = 120
SPEECH_DAYS = 30          # dot's permission to reply in the room, renewed by requests
SPEECH_RENEW = 7 * 86400  # a request renews it once less than this is left
MAX_TEXT = 6000           # of the person's request; the room keeps up to 8000 characters per message

JOIN = """@WebCoder 把这个聊天设为我的「WebCoder 门铃」：
1. 用加入码 {code} 调用 collaboration_join，登记当前已授权的连接。
{rest}"""
SUBSCRIBE = """{step}. 用宿主的原生事件订阅能力，只订阅下面这一个事件（collaboration_join 返回的其他订阅请求不用建）：
{subscription}
{next}. 以后每次收到这个事件：用 collaboration_read 读这条消息（project="{project}"，environment_id="{environment}"，\
kind="thread"，id=事件里的 message_id）。消息里写明了要处理的项目和请求，用 WebCoder 工具按要求处理；处理完只回复一次，\
用 collaboration_message_create（project="{project}"，room_id="{room}"，reply_to_id=这条消息的 id，\
client_message_id 和 idempotency_key 各用一个新的随机值）。测试事件只回复事件编号。
回调和签名材料由宿主提供；订阅失败时报告原始错误并停止，不要编造回调或声称已接通。"""
REQUEST = """【WebCoder 门铃】{user} 在 {device} 上用 webcoder 命令行发来的请求。
项目：{alias}（{project_device} 上的 {root}）。调用 WebCoder 工具时传 project="{project_id}"，路径相对于项目根目录。
处理完后只回复一次：collaboration_message_create（project="{doorbell}"，room_id="{room}"，\
reply_to_id 填这条消息的 id）。回复里写结论；改了文件就列出改动的文件。

请求：
{text}"""


class SetupBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    new: bool = False  # replace the doorbell slot (dot joins again)


class AskBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    client_message_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class EmptyBody(BaseModel):
    pass


def add_doorbell_routes(router: APIRouter, rt, signed) -> None:
    """Add /api/cli/dot/... to the command line's router; `signed` checks the device signature."""
    store = rt.store

    def service():
        collaboration = rt.collaboration
        if not collaboration.config.enabled or not collaboration.config.events_enabled:
            raise ApiError(409, "COLLABORATION_DISABLED", "The hub's collaboration center and MCP Events are off: "
                           "set WEBCODER_COLLABORATION_ENABLED=true and WEBCODER_MCP_EVENTS_ENABLED=true and "
                           "restart the hub")
        return collaboration

    def owner(device: dict) -> Principal:
        return Principal(actor=f"设备:{device['name']}·CLI", user_id=device["user_id"],
                         scopes=set(OWNER_CAPABILITIES), admin=True, space_id=device["user_id"],
                         username=device["username"], kind="cli")

    def key() -> str:
        return uuid.uuid4().hex

    def scope(room: dict) -> dict:
        return {"project": room["project_id"], "environment_id": room["environment_id"]}

    def control(collaboration, principal: Principal, room: dict, slot: dict, action: str) -> dict:
        collaboration.joining.control({**scope(room), "slot_id": slot["id"], "expected_version": slot["version"],
                                       "action": action, "idempotency_key": key()}, principal)
        return collaboration.joining.slot(room, slot["id"])

    def find(user_id: str) -> tuple[dict | None, dict | None]:
        """This user's newest doorbell slot that was not revoked (an expired one too), with its room."""
        slot = store.one("""SELECT j.* FROM collaboration_join_slots j JOIN collaboration_rooms r ON r.id=j.room_id
            WHERE r.owner_user_id=? AND r.space_id=? AND j.kind='dot' AND j.label=? AND j.state!='revoked'
            ORDER BY j.created DESC LIMIT 1""", (user_id, user_id, LABEL))
        if slot is None:
            return None, None
        return store.one("SELECT * FROM collaboration_rooms WHERE id=?", (slot["room_id"],)), slot

    def subscription(room: dict, slot: dict) -> dict:
        return {"name": MESSAGE_EVENT, "arguments": {"conversation_id": room["id"], "project_id": room["project_id"],
                                                     "environment_id": room["environment_id"], "slot_id": slot["id"]}}

    def instruction(room: dict, slot: dict, view: dict, subscribed: bool) -> str | None:
        rest = dict(subscription=json.dumps(subscription(room, slot), ensure_ascii=False, indent=2),
                    project=room["project_id"], environment=room["environment_id"], room=room["id"])
        if view["join_code"]:
            return JOIN.format(code=view["join_code"], rest=SUBSCRIBE.format(step=2, next=3, **rest))
        if view["state"] == "registered" and not subscribed:
            return (f"@WebCoder 继续设置「WebCoder 门铃」（位置 {slot['id']} 已登记）：\n"
                    + SUBSCRIBE.format(step=1, next=2, **rest))
        return None

    def connection(slot: dict) -> dict | None:
        grant = store.one("SELECT label, kind, scopes, projects, revoked FROM grants WHERE id=?", (slot["grant_id"],)) \
            if slot["grant_id"] else None
        if grant is None:
            return None
        projects, scopes = json.loads(grant["projects"]), json.loads(grant["scopes"])
        return {"label": grant["label"], "kind": grant["kind"], "revoked": bool(grant["revoked"]),
                "all_projects": projects == "*", "projects": [] if projects == "*" else projects,
                "write": "write" in scopes}

    def speech(room: dict, slot: dict) -> dict | None:
        if not slot["grant_id"]:
            return None
        return store.one("SELECT enabled, expires_at, version FROM conversation_writers WHERE conversation_id=? AND "
                         "room_id=? AND grant_id=?", (room["id"], room["id"], slot["grant_id"]))

    def state(collaboration, principal: Principal, room: dict, slot: dict) -> dict:
        view = collaboration.joining.view(room, slot, principal)
        mentions = [route for route in view["routes"] if route["name"] == MESSAGE_EVENT]
        subscribed = any(route["available"] for route in mentions)
        project = store.one("SELECT id, alias, root FROM projects WHERE id=?", (room["project_id"],))
        writer = speech(room, slot)
        now = collaboration.clock()
        return {"setup": True, "room_id": room["id"], "room_state": room["state"],
                "environment_id": room["environment_id"], "project": project,
                "slot": {key: view[key] for key in ("id", "state", "status", "expires_at", "code_expires_at",
                                                    "joined_at", "join_code")},
                "subscription": subscription(room, slot),
                "mentions": {"subscribed": subscribed,
                             "routes": [{key: route[key] for key in ("id", "state", "available", "expires_at", "test")}
                                        for route in mentions]},
                "routes": [{key: route[key] for key in ("name", "available", "test")} for route in view["routes"]],
                "speech": {"enabled": bool(writer and writer["enabled"] and writer["expires_at"] > now),
                           "expires_at": writer["expires_at"] if writer else None},
                "connection": connection(slot),
                "instruction": instruction(room, slot, view, subscribed)}

    def ready(collaboration, principal: Principal, device: dict) -> tuple[dict, dict]:
        """The doorbell, once dot has joined it and subscribed to mentions; otherwise what is missing."""
        room, slot = find(device["user_id"])
        if slot is None:
            raise ApiError(409, "DOORBELL_MISSING", "There is no dot doorbell yet: run webcoder dot setup")
        if slot["expires_at"] <= collaboration.clock():
            raise ApiError(409, "DOORBELL_EXPIRED", "The dot doorbell has expired: run webcoder dot setup and let "
                           "dot join again")
        if room["state"] != "active":
            raise ApiError(409, "DOORBELL_PAUSED", "The doorbell room is paused in the hub panel: resume it there")
        if slot["state"] != "registered":
            raise ApiError(409, "DOT_NOT_JOINED", "dot has not joined the doorbell yet: send the instruction from "
                           "webcoder dot setup to the dot chat")
        reach = connection(slot)
        if reach is None or reach["revoked"]:
            raise ApiError(409, "DOT_UNAUTHORIZED", "The WebCoder authorization dot joined with was revoked: run "
                           "webcoder dot setup --new and let dot join again")
        routes = collaboration.joining.routes(room, slot)
        if not any(route["name"] == MESSAGE_EVENT and route["available"] for route in routes):
            raise ApiError(409, "DOT_NOT_SUBSCRIBED", "dot joined but is not subscribed to mentions: send the "
                           "instruction from webcoder dot setup to the dot chat again")
        return room, slot

    def renew_speech(collaboration, principal: Principal, room: dict, slot: dict) -> float:
        writer = speech(room, slot)
        if writer and writer["enabled"] and writer["expires_at"] - collaboration.clock() > SPEECH_RENEW:
            return writer["expires_at"]
        granted = collaboration.chatroom.access({**scope(room), "room_id": room["id"], "grant_id": slot["grant_id"],
                                                 "enabled": True, "expires_in_days": SPEECH_DAYS,
                                                 "expected_version": writer["version"] if writer else 0,
                                                 "idempotency_key": key()}, principal)
        return granted["expires_at"]

    def thread_root(user_id: str, message_id: str) -> dict:
        row = store.one("""SELECT m.id, m.room_id, m.origin FROM collaboration_messages m
            JOIN collaboration_rooms r ON r.id=m.room_id WHERE m.id=? AND r.owner_user_id=? AND r.space_id=?""",
                        (message_id, user_id, user_id))
        if row is None or row["origin"] != "panel_owner":
            raise ApiError(404, "NOT_FOUND", "No such doorbell request")
        return store.one("SELECT * FROM collaboration_rooms WHERE id=?", (row["room_id"],))

    @router.post("/dot/setup")
    async def setup(request: Request):
        device, body = await signed(request, SetupBody)

        def perform():
            collaboration, principal = service(), owner(device)
            mine = (body.project_id, device["user_id"])
            if store.one("SELECT 1 FROM projects WHERE id=? AND user_id=?", mine) is None:
                raise ApiError(404, "PROJECT_NOT_FOUND", "No such project")
            room, slot = find(device["user_id"])
            now = collaboration.clock()
            if slot is not None and body.new:
                control(collaboration, principal, room, slot, "revoke")
            if slot is None or body.new or slot["expires_at"] <= now:
                room = collaboration.room_create({"project": body.project_id, "environment_id": ENVIRONMENT,
                                                  "idempotency_key": key()}, principal)["room"]
                created = collaboration.joining.create({**scope(room), "label": LABEL, "kind": "dot",
                                                        "code_ttl_minutes": CODE_MINUTES, "expires_in_days": SLOT_DAYS,
                                                        "idempotency_key": key()}, principal)
                slot = collaboration.joining.slot(room, created["slot"]["id"])
            elif slot["state"] == "invited" and slot["code_expires_at"] <= now:
                slot = control(collaboration, principal, room, slot, "refresh_code")
            return state(collaboration, principal, room, slot)

        return await store.run(perform)

    @router.post("/dot/status")
    async def status(request: Request):
        device, _ = await signed(request, EmptyBody)

        def perform():
            collaboration, principal = service(), owner(device)
            room, slot = find(device["user_id"])
            return {"setup": False} if slot is None else state(collaboration, principal, room, slot)

        return await store.run(perform)

    @router.post("/dot/test")
    async def test(request: Request):
        device, _ = await signed(request, EmptyBody)

        def perform():
            collaboration, principal = service(), owner(device)
            room, slot = ready(collaboration, principal, device)
            return state(collaboration, principal, room, control(collaboration, principal, room, slot, "test"))

        return await store.run(perform)

    @router.post("/dot/messages")
    async def ask(request: Request):
        device, body = await signed(request, AskBody)

        def perform():
            collaboration, principal = service(), owner(device)
            room, slot = ready(collaboration, principal, device)
            target = store.one("SELECT p.id, p.alias, p.root, d.name AS device_name FROM projects p "
                               "JOIN devices d ON d.id=p.device_id WHERE p.id=? AND p.user_id=?",
                               (body.project_id, device["user_id"]))
            if target is None:
                raise ApiError(404, "PROJECT_NOT_FOUND", "No such project")
            warnings = []
            reach = connection(slot)
            if reach and not reach["all_projects"] and target["id"] not in reach["projects"]:
                warnings.append(f"dot's WebCoder authorization does not include the project {target['alias']}: "
                                "authorize the app again in ChatGPT and choose all projects")
            elif reach and not reach["write"]:
                warnings.append("dot's WebCoder authorization is read-only: it can read the project but not change it")
            speech_until = renew_speech(collaboration, principal, room, slot)
            text = REQUEST.format(user=device["username"], device=device["name"], alias=target["alias"],
                                  project_device=target["device_name"], root=target["root"], project_id=target["id"],
                                  doorbell=room["project_id"], room=room["id"], text=body.text)
            created = collaboration.chatroom.create({**scope(room), "room_id": room["id"], "body_text": text,
                                                     "client_message_id": body.client_message_id,
                                                     "idempotency_key": body.client_message_id,
                                                     "mentions": [{"slot_id": slot["id"]}]}, principal)
            notice = created["notifications"][0] if created["notifications"] else {"state": "not_sent"}
            return {"message_id": created["message"]["id"], "notification": notice, "warnings": warnings,
                    "project": {"id": target["id"], "alias": target["alias"], "root": target["root"]},
                    "speech_expires_at": speech_until, "slot_expires_at": slot["expires_at"]}

        return await store.run(perform)

    @router.post("/dot/messages/{message_id}")
    async def replies(message_id: str, request: Request):
        device, _ = await signed(request, EmptyBody)

        def perform():
            collaboration, principal = service(), owner(device)
            room = thread_root(device["user_id"], message_id)
            thread = collaboration.read({**scope(room), "room_id": room["id"], "kind": "thread", "id": message_id,
                                         "limit": 100}, principal)
            items = thread["items"]
            original = next((item for item in items if item["id"] == message_id), None)
            if original is None:
                raise ApiError(404, "NOT_FOUND", "No such doorbell request")
            notices = original["body"].get("notifications") or [{"state": "not_sent"}]
            answers = [{"id": item["id"], "text": item["body_text"], "created": item["created"]}
                       for item in items if item["author_kind"] == "connector"
                       and item["source_sequence"] > original["source_sequence"]]
            return {"notification": notices[0], "replies": answers}

        return await store.run(perform)
