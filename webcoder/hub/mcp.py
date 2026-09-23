"""Streamable HTTP MCP endpoint (protocol 2025-11-25 / 2025-06-18 / 2025-03-26).

Stateless: each POST carries one JSON-RPC message (or a 2025-03-26 batch) and
gets a JSON response. There are no sessions and no server-initiated streams.

Auth follows what ChatGPT and Codex expect: a missing or bad token gets HTTP 401
with ``WWW-Authenticate: Bearer resource_metadata=..., scope="read write"``. When
an OAuth grant lacks the scope a tool needs, the tool result is an error that
carries ``_meta["mcp/www_authenticate"]`` (``error="insufficient_scope"``), which
makes ChatGPT show its sign-in prompt again. Requests in the 2026-07-28 "modern"
shape get HTTP 400 with a plain JSON-RPC error, so dual-era clients fall back to
initialize.
"""
from __future__ import annotations

import html
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from webcoder import VERSION
from webcoder.hub import ui
from webcoder.hub.auth import SCOPES
from webcoder.hub.cli_tools import CLI_TOOLS
from webcoder.hub.git_tools import GIT_TOOLS
from webcoder.hub.mobile_tools import MOBILE_TOOLS
from webcoder.hub.skill_tools import SKILL_TOOLS
from webcoder.hub.ssh_tools import SSH_TOOLS
from webcoder.hub.tools import INSTRUCTIONS, TAKE_LARGE, TOOLS, execute

ALL_TOOLS = {**TOOLS, **GIT_TOOLS, **SKILL_TOOLS, **MOBILE_TOOLS, **SSH_TOOLS, **CLI_TOOLS}

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LATEST_VERSION = SUPPORTED_VERSIONS[0]
SERVER_INFO = {"name": "webcoder", "title": "WebCoder", "version": VERSION}
MODERN_META_KEY = "io.modelcontextprotocol/protocolVersion"
NO_STORE = {"Cache-Control": "no-store"}
MAX_BATCH = 50  # JSON-RPC batches (MCP clients hardly send them any more)
MAX_BATCH_REPLIES = 24 * 1024 * 1024  # characters of replies one batch may pile up (within the memory budget)
BROWSER_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>WebCoder · MCP 接口</title>
<style>body {{ font: 15px/1.7 system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  max-width: 640px; margin: 48px auto; padding: 0 20px; color: #1f2328; }}
code {{ background: #f2f4f7; padding: 2px 6px; border-radius: 6px; }} a {{ color: #2563eb; }}</style></head>
<body><h1>这是 MCP 接口地址，服务运行正常</h1>
<p><code>{mcp_url}</code> 是给 ChatGPT、Codex 这类 MCP 客户端用的，只接受 POST 请求，
所以用浏览器直接打开会返回 405，这是正常的。</p>
<p>请把这个地址填到客户端里：ChatGPT 在开发者模式下创建应用（认证选 OAuth），Codex 用 <code>codex mcp add</code>。</p>
<p>管理面板（设备、项目、令牌、用户）在 <a href="{panel_url}/">{panel_url}/</a>。</p></body></html>
"""


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _is_modern(message) -> bool:
    if not isinstance(message, dict):
        return False
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return message.get("method") == "server/discover" or (
        isinstance(meta, dict) and MODERN_META_KEY in meta and message.get("method") != "initialize")


def make_mcp_router(rt) -> APIRouter:
    router = APIRouter()
    settings = rt.settings
    resource_metadata = settings.public_url + "/.well-known/oauth-protected-resource/mcp"

    def challenge(error: str | None = None, description: str | None = None, scopes=("read", "write")) -> str:
        params = [f'error="{error}"'] if error else []
        if description:
            params.append(f'error_description="{description}"')
        params += [f'resource_metadata="{resource_metadata}"', f'scope="{" ".join(scopes)}"']
        return "Bearer " + ", ".join(params)

    def rpc_error(request_id, code: int, message: str, status: int = 200, headers: dict | None = None):
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
                            status_code=status, headers={**NO_STORE, **(headers or {})})

    def origin_allowed(request: Request) -> bool:
        origin = request.headers.get("origin")
        return not origin or origin == settings.public_url or origin in settings.allowed_origins

    def listed_tools(principal) -> list:
        """Phone / SSH tools only for credentials allowed to use them, or for OAuth clients of a user who
        turned the feature on somewhere (so ChatGPT can ask for the extra permission). Keeps the list short."""
        rows = rt.store.all("SELECT id, info FROM devices WHERE user_id=? AND enabled=1", (principal.user_id,))
        if principal.projects is not None:  # only the computers hosting its projects: those the tools can reach
            hosts = {row["device_id"] for row in rt.store.all("SELECT id, device_id FROM projects WHERE user_id=?",
                                                              (principal.user_id,)) if principal.can_access(row["id"])}
            rows = [row for row in rows if row["id"] in hosts]
        infos = [json.loads(row["info"] or "{}") for row in rows]
        offered = {"mobile": any((info.get("mobile") or {}).get("enabled") for info in infos),
                   "ssh": any((info.get("ssh") or {}).get("hosts") for info in infos),
                   "cli": any((info.get("commands") or {}).get("programs") for info in infos)}
        tools = [*TOOLS.values(), *GIT_TOOLS.values(), *SKILL_TOOLS.values()]
        for scope, extra in (("mobile", MOBILE_TOOLS), ("ssh", SSH_TOOLS), ("cli", CLI_TOOLS)):
            if scope in principal.scopes or (principal.kind == "oauth" and offered[scope]):
                tools += extra.values()
        return tools

    def missing_scope(principal, tool) -> dict:
        rt.audit(principal.actor, tool.name, "", "INSUFFICIENT_SCOPE", user_id=principal.user_id)
        lacking = tool.required - principal.scopes
        what = ("run commands on servers" if "ssh" in lacking else "control phones" if "mobile" in lacking
                else "run programs on the computer" if "cli" in lacking else "modify files")
        text = (f"Error INSUFFICIENT_SCOPE: this credential is not allowed to {what}, so it cannot use {tool.name}. "
                + (f"Sign in again and allow it to {what}." if principal.kind == "oauth" else
                   f"Ask the owner for a token that may {what} (WebCoder panel → 访问令牌)."))
        result = {"isError": True, "content": [{"type": "text", "text": text}]}
        if principal.kind == "oauth":  # ChatGPT re-runs OAuth when it sees this
            # Ask for what the grant already has plus what is missing: the new grant replaces the old one.
            wanted = [scope for scope in SCOPES if scope in principal.scopes | tool.required]
            result["_meta"] = {"mcp/www_authenticate": [
                challenge("insufficient_scope", f"This action needs permission to {what}", wanted)]}
        return result

    async def dispatch(method: str, params: dict, principal):
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {"protocolVersion": requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION,
                    "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                    "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool.definition(oauth=principal.kind == "oauth") for tool in listed_tools(principal)]}
        if method == "tools/call":
            name = params.get("name")
            tool = ALL_TOOLS.get(name) if isinstance(name, str) else None
            if tool is None:
                raise _RpcError(-32602, f"Unknown tool: {name!r}")
            if not tool.required <= principal.scopes:
                return missing_scope(principal, tool)
            outcome, is_error = await execute(rt, principal, tool, params.get("arguments"))
            result = {"content": [{"type": "text", "text": outcome.text}]}
            if outcome.structured is not None:
                result["structuredContent"] = outcome.structured
            if outcome.meta:  # for the tool's UI component only (e.g. an image to show)
                result["_meta"] = outcome.meta
            if is_error:
                result["isError"] = True
            return result
        if method == "resources/list":
            return {"resources": ui.resource_list()}
        if method == "resources/read":
            found = ui.resource_read(params.get("uri"))
            if found is None:
                raise _RpcError(-32002, f"Resource not found: {params.get('uri')!r}")
            return found
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        if method == "logging/setLevel":
            return {}
        raise _RpcError(-32601, f"Method not found: {method}")

    async def handle(message, principal):
        """Answer one JSON-RPC message; None means nothing to send back (notification or response)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if "method" not in message:
            return None  # a response to a server request; this server never sends any
        request_id = message.get("id")
        method, params = message.get("method"), message.get("params") or {}
        if "id" not in message:
            return None  # notification (e.g. notifications/initialized)
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request id"}}
        if not isinstance(method, str) or not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid Request"}}
        try:
            result = await dispatch(method, params, principal)
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @router.post("/mcp")
    async def mcp_post(request: Request):
        taking = TAKE_LARGE.set(request.scope.get("webcoder.take_large"))  # tools moving large payloads use it
        try:
            response = await answer_post(request)
        finally:
            TAKE_LARGE.reset(taking)
        origin = request.headers.get("origin")
        if origin and origin_allowed(request):  # a browser client may only read the reply with these
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Expose-Headers"] = "WWW-Authenticate"
            response.headers["Vary"] = "Origin"
        return response

    async def answer_post(request: Request):
        if not origin_allowed(request):
            return rpc_error(None, -32000, "Origin not allowed", 403)
        principal, state = rt.auth.bearer(request)
        if principal is None:
            return rpc_error(None, -32001, "Authentication required: use an access token from the WebCoder panel or "
                             "sign in with OAuth", 401,
                             {"WWW-Authenticate": challenge("invalid_token" if state == "invalid" else None)})
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return rpc_error(None, -32700, "Content-Type must be application/json", 415)
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return rpc_error(None, -32700, "Parse error", 400)
        single_id = body.get("id") if isinstance(body, dict) else None
        version = request.headers.get("mcp-protocol-version")
        if (version and version not in SUPPORTED_VERSIONS) or _is_modern(body):
            return rpc_error(single_id, -32600, "This server speaks MCP " + ", ".join(SUPPORTED_VERSIONS)
                             + "; start with initialize", 400)
        if isinstance(body, list):
            if not body:
                return rpc_error(None, -32600, "Empty batch", 400)
            if len(body) > MAX_BATCH:  # each reply is kept until the last: a huge batch would pile them up
                return rpc_error(None, -32600, f"A batch holds at most {MAX_BATCH} messages", 400)
            replies, size = [], 0
            for item in body:  # all replies are held until the last: within a budget, beyond it nothing more runs
                if size > MAX_BATCH_REPLIES:
                    reply = ({"jsonrpc": "2.0", "id": item["id"], "error": {
                        "code": -32000, "message": "This batch's replies are over the size limit; nothing more in "
                                                   "it was run. Send the rest separately."}}
                             if isinstance(item, dict) and "id" in item else None)
                else:
                    reply = await handle(item, principal)
                    size += len(json.dumps(reply, ensure_ascii=False)) if reply else 0
                if reply:
                    replies.append(reply)
            return JSONResponse(replies, headers=NO_STORE) if replies else Response(status_code=202)
        reply = await handle(body, principal)
        if reply is None:
            return Response(status_code=202)
        return JSONResponse(reply, headers=NO_STORE)

    @router.get("/mcp")
    async def mcp_get(request: Request):
        # No server-initiated streams, so GET is 405 as the spec allows. Someone opening the
        # address in a browser gets an explanation instead of a blank error page.
        if "text/html" in request.headers.get("accept", ""):
            page = BROWSER_PAGE.format(mcp_url=html.escape(settings.mcp_url),
                                       panel_url=html.escape(settings.public_url))
            return HTMLResponse(page, status_code=405, headers={"Allow": "POST", **NO_STORE})
        return Response(status_code=405, headers={"Allow": "POST"})

    @router.delete("/mcp")
    async def mcp_delete():
        return Response(status_code=405, headers={"Allow": "POST"})

    @router.options("/mcp")
    async def mcp_preflight(request: Request):
        origin = request.headers.get("origin")
        if not origin_allowed(request):
            return Response(status_code=403)
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": origin or "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, MCP-Protocol-Version",
            "Access-Control-Max-Age": "600", "Vary": "Origin"})

    return router
