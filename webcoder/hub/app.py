"""FastAPI application factory for the hub."""
from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from webcoder import VERSION
from webcoder.hub.auth import ApiError
from webcoder.hub.collaboration.api import make_router as make_collaboration_router
from webcoder.hub.collaboration.errors import DevError
from webcoder.hub.config import Settings
from webcoder.hub.downloads import make_download_router
from webcoder.hub.mcp import make_mcp_router
from webcoder.hub.oauth import make_oauth_router
from webcoder.hub.panel import make_panel_router
from webcoder.hub.runtime import Runtime

STATIC = Path(__file__).resolve().parent / "static"
PANEL_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
MAX_BODY = 1024 * 1024  # OAuth and panel requests are small
# Tool calls carry file content (far less in practice: a model does not write megabytes at once). A large body is
# held several times over while it is handled (bytes, parsed, sealed for the agent): these two keep that within
# the hub's memory budget (MemoryMax=300M in deploy/webcoder-hub.service).
MAX_MCP_BODY = 24 * 1024 * 1024
MAX_LARGE = 2  # such large bodies handled at the same time
WS_MAX_QUEUE = 1  # WebSocket messages the server holds before the hub reads them, per connection


def ws_frame_limit(settings: Settings) -> int:
    """The largest WebSocket frame the hub takes: the largest message it exchanges with an agent (a tool call's
    body, a file read for copy_file or view_image, base64) plus room for the envelope. The server holds a frame
    whole before the hub sees it, also one sent before any agent authenticated: this limit bounds that memory."""
    from webcoder.hub.tools import IMAGE_MAX_BYTES

    def base64(size: int) -> int:
        return (size + 2) // 3 * 4

    return max(MAX_MCP_BODY, base64(settings.max_transfer_bytes), base64(IMAGE_MAX_BYTES)) + 1024 * 1024


class BodyLimit:
    """Stops a request body at the limit while it arrives: the routes read whole bodies, and some are open to
    anyone (OAuth client registration, token, revocation). Nothing is buffered here, so /mcp still checks the
    token before it reads anything. Over the limit, the route sees the client go away and the answer is 413."""

    def __init__(self, app, default: int = MAX_BODY, limits: dict[str, int] | None = None, large: int = MAX_LARGE):
        self.app, self.default, self.limits = app, default, limits or {"/mcp": MAX_MCP_BODY}
        self.large, self.large_now = large, 0  # bodies over the default size being handled right now

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limits.get(scope["path"], self.default)
        too_large = JSONResponse({"error": {"code": "REQUEST_TOO_LARGE",
                                            "message": f"The request body is over {limit:,} bytes"}},
                                 status_code=413, headers={"Connection": "close"})
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await too_large(scope, receive, send)
            return
        # Each large body is held whole while it is handled: only a few at a time, so they fit in memory together.
        large = limit > self.default and (declared is None or int(declared) > self.default)
        if large and self.large_now >= self.large:
            busy = JSONResponse({"error": {"code": "BUSY", "message": "Too many large requests at once; try again "
                                                                     "in a moment"}},
                                status_code=503, headers={"Retry-After": "5", "Connection": "close"})
            await busy(scope, receive, send)
            return
        held = {"large": large}

        def take_large() -> bool:
            """For a small request whose answer is large (a file read as base64, an image ...): one of the same
            places, until the answer has been sent. False when none is free."""
            if not held["large"]:
                if self.large_now >= self.large:
                    return False
                self.large_now += 1
                held["large"] = True
            return True

        scope["webcoder.take_large"] = take_large
        self.large_now += large
        try:
            await self._limited(scope, receive, send, limit, too_large)
        finally:
            self.large_now -= held["large"]

    async def _limited(self, scope, receive, send, limit: int, too_large) -> None:
        state = {"received": 0, "over": False, "answered": False}

        async def counted():
            if state["over"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                state["received"] += len(message.get("body", b""))
                if state["received"] > limit:  # a chunked body declares no length up front
                    state["over"] = True
                    return {"type": "http.disconnect"}
            return message

        async def tracked(message):
            if message["type"] == "http.response.start":
                state["answered"] = True
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except Exception:  # noqa: BLE001 - the route gave up on a body cut off here
            if not state["over"] or state["answered"]:
                raise
        if state["over"] and not state["answered"]:
            await too_large(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    rt = Runtime(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await rt.collaboration.loops.start()  # nothing starts while the collaboration switches are off
        try:
            yield
        finally:
            await rt.collaboration.loops.stop()
            rt.close()

    app = FastAPI(title="WebCoder Hub", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.runtime = rt

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, **exc.extra}},
                            status_code=exc.status)

    @app.exception_handler(DevError)
    async def collaboration_error(request: Request, exc: DevError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, **exc.details}},
                            status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        problems = "; ".join(f"{'.'.join(str(part) for part in item['loc'][1:])}: {item['msg']}"
                             for item in exc.errors())
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": problems or "请求格式不正确"}},
                            status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):  # one user's data (session, CSRF token, tokens ...): no cache keeps it
            response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers.setdefault("Content-Security-Policy", PANEL_CSP)
            response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    app.add_middleware(BodyLimit)  # added last: it runs first
    app.include_router(make_oauth_router(rt))
    app.include_router(make_mcp_router(rt))
    app.include_router(make_panel_router(rt))
    app.include_router(make_collaboration_router(rt))
    app.include_router(make_download_router())

    @app.websocket("/agent/ws")
    async def agent_socket(websocket: WebSocket):
        await rt.agents.serve(websocket)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "version": VERSION, "agents_online": len(rt.agents.connections)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
