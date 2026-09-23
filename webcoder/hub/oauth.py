"""OAuth 2.1 authorization server for MCP clients (ChatGPT web and Codex).

Discovery: RFC 9728 protected-resource metadata + RFC 8414 server metadata.
Clients register dynamically (RFC 7591, public clients only), must use PKCE
S256, and receive audience-bound access tokens plus rotating refresh tokens.
The hub owner approves each connection on the panel's consent page.
Authorization responses carry ``iss`` (RFC 9207).
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import re
import time
import uuid
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from webcoder.common.crypto import new_token, pkce_challenge, sha256_hex
from webcoder.hub.auth import SCOPES, ApiError, normalize_scopes
from webcoder.hub.clients import RedirectPolicy, describe_client

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
CORS = {"Access-Control-Allow-Origin": "*"}


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error, self.description, self.status = error, description, status

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description} if self.description else {
            "error": self.error}


def append_query(url: str, params: dict) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(key, value) for key, value in params.items() if value is not None]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class OAuthService:
    REQUEST_TTL = 600
    CODE_TTL = 300
    MAX_PENDING = 2000  # authorization requests awaiting consent, from everyone: bounds what a flood can store

    def __init__(self, runtime):
        self.rt = runtime
        self.store = runtime.store
        self.settings = runtime.settings

    def resource_ok(self, resource) -> bool:
        # Codex sends the MCP URL; ChatGPT sends the server origin. Both name this hub's only resource.
        public, mcp = self.settings.public_url, self.settings.mcp_url
        return resource in (None, "", mcp, mcp + "/", public, public + "/")

    # ------------------------------------------------------------------ discovery
    def protected_resource_metadata(self) -> dict:
        return {"resource": self.settings.mcp_url, "authorization_servers": [self.settings.issuer],
                "scopes_supported": list(SCOPES), "bearer_methods_supported": ["header"],
                "resource_name": "WebCoder"}

    def server_metadata(self) -> dict:
        base = self.settings.issuer
        return {"issuer": base, "authorization_endpoint": base + "/oauth/authorize",
                "token_endpoint": base + "/oauth/token", "registration_endpoint": base + "/oauth/register",
                "revocation_endpoint": base + "/oauth/revoke", "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
                "scopes_supported": list(SCOPES), "authorization_response_iss_parameter_supported": True}

    # ------------------------------------------------------------------ registration
    def register(self, body) -> dict:
        if not isinstance(body, dict):
            raise OAuthError("invalid_client_metadata", "Body must be a JSON object")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not 1 <= len(uris) <= 10:
            raise OAuthError("invalid_redirect_uri", "redirect_uris must list 1-10 URIs")
        for uri in uris:
            try:
                self.rt.redirects.check(uri)
            except ValueError as exc:
                raise OAuthError("invalid_redirect_uri", f"{uri!r}: {exc}") from None
        grant_types = body.get("grant_types") or ["authorization_code"]
        if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
            raise OAuthError("invalid_client_metadata", "The authorization_code grant is required")
        name = str(body.get("client_name") or "MCP client").strip()[:100] or "MCP client"
        client_id = "wc_c_" + uuid.uuid4().hex
        now = time.time()
        stale = now - 30 * 86400
        with self.store.transaction() as db:
            db.execute("DELETE FROM oauth_clients WHERE created<? AND coalesce(last_used, 0)<? AND id NOT IN "
                       "(SELECT client_id FROM grants WHERE client_id IS NOT NULL AND revoked=0)", (stale, stale))
            db.execute("INSERT INTO oauth_clients(id, name, redirect_uris, created) VALUES (?, ?, ?, ?)",
                       (client_id, name, json.dumps(uris), now))
        return {"client_id": client_id, "client_id_issued_at": int(now), "client_name": name, "redirect_uris": uris,
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}

    # ------------------------------------------------------------------ authorization + consent
    def authorize(self, query) -> tuple[str | None, str | None]:
        """Validate an authorization request: (redirect location, None) or (None, error page text)."""
        client = self.store.one("SELECT * FROM oauth_clients WHERE id=?", (query.get("client_id") or "",))
        if not client:
            return None, "未知的客户端（client_id 无效或已被清理）。请在 ChatGPT / Codex 中重新连接这个 MCP 服务器。"
        registered = json.loads(client["redirect_uris"])
        redirect_uri = query.get("redirect_uri") or (registered[0] if len(registered) == 1 else "")
        if not any(RedirectPolicy.matches(uri, redirect_uri) for uri in registered):
            return None, "回调地址与客户端注册时提交的不一致。"
        try:
            self.rt.redirects.check(redirect_uri)
        except ValueError:
            return None, "该回调地址不在允许范围内（仅支持 ChatGPT 与 Codex）。"
        state = query.get("state")

        def fail(error: str, description: str):
            return append_query(redirect_uri, {"error": error, "error_description": description, "state": state,
                                               "iss": self.settings.issuer}), None

        if query.get("response_type") != "code":
            return fail("unsupported_response_type", "Only response_type=code is supported")
        challenge = query.get("code_challenge") or ""
        if query.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", challenge):
            return fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
        if not self.resource_ok(query.get("resource")):
            return fail("invalid_target", f"Unknown resource; this server is {self.settings.mcp_url}")
        scopes = normalize_scopes((query.get("scope") or "").split()) or ["read", "write"]
        request_id = uuid.uuid4().hex
        now = time.time()
        with self.store.transaction() as db:
            db.execute("DELETE FROM oauth_requests WHERE expires<?", (now,))
            if db.execute("SELECT count(*) FROM oauth_requests").fetchone()[0] >= self.MAX_PENDING:
                return None, "当前等待确认的授权请求过多，请几分钟后再试。"
            db.execute(
                "INSERT INTO oauth_requests(id, client_id, redirect_uri, state, code_challenge, scopes, resource, "
                "expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, client["id"], redirect_uri, state, challenge, json.dumps(scopes), self.settings.mcp_url,
                 now + self.REQUEST_TTL))
        return f"/#/consent/{request_id}", None

    def _pending(self, request_id: str) -> dict:
        row = self.store.one("SELECT * FROM oauth_requests WHERE id=? AND expires>?", (request_id, time.time()))
        if not row:
            raise ApiError(404, "REQUEST_EXPIRED", "授权请求不存在或已过期，请回到客户端重新连接")
        return row

    def request_details(self, request_id: str) -> dict:
        row = self._pending(request_id)
        client = self.store.one("SELECT * FROM oauth_clients WHERE id=?", (row["client_id"],)) or {}
        return {"id": request_id, "client_name": client.get("name", "?"), "redirect_uri": row["redirect_uri"],
                "redirect_host": urlsplit(row["redirect_uri"]).netloc, "scopes": json.loads(row["scopes"]),
                "expires": row["expires"], **describe_client(client.get("name", ""), row["redirect_uri"])}

    def approve(self, request_id: str, user_id: str, scopes, projects) -> str:
        """The signed-in panel user grants the client access to (some of) *their own* projects."""
        row = self._pending(request_id)
        requested = json.loads(row["scopes"])
        chosen = [scope for scope in normalize_scopes(scopes) if scope in requested]
        if "read" not in chosen:
            raise ApiError(400, "INVALID_SCOPE", "至少需要授予读取权限")
        code = new_token("wc_code_")
        with self.store.transaction() as db:
            db.execute("DELETE FROM oauth_requests WHERE id=?", (request_id,))
            db.execute("INSERT INTO oauth_codes(hash, client_id, redirect_uri, code_challenge, scopes, projects, "
                       "resource, expires, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (sha256_hex(code), row["client_id"], row["redirect_uri"], row["code_challenge"],
                        json.dumps(chosen), json.dumps(projects), row["resource"], time.time() + self.CODE_TTL,
                        user_id))
        return append_query(row["redirect_uri"], {"code": code, "state": row["state"], "iss": self.settings.issuer})

    def deny(self, request_id: str) -> str:
        row = self._pending(request_id)
        self.store.execute("DELETE FROM oauth_requests WHERE id=?", (request_id,))
        return append_query(row["redirect_uri"], {"error": "access_denied", "error_description": "The owner denied "
                                                  "access", "state": row["state"], "iss": self.settings.issuer})

    # ------------------------------------------------------------------ token endpoint
    def token(self, form: dict, basic_client_id: str | None) -> dict:
        grant_type = form.get("grant_type")
        client_id = form.get("client_id") or basic_client_id
        if grant_type == "authorization_code":
            return self._exchange(form, client_id)
        if grant_type == "refresh_token":
            return self._refresh(form, client_id)
        raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token")

    def _exchange(self, form: dict, client_id: str | None) -> dict:
        client = self.store.one("SELECT * FROM oauth_clients WHERE id=?", (client_id or "",))
        if not client:
            # 401 invalid_client is the standard signal for a client to register again.
            raise OAuthError("invalid_client", "Unknown client_id; register again", 401)
        now = time.time()
        with self.store.transaction() as db:  # codes are single-use: read and delete atomically
            row = db.execute("SELECT * FROM oauth_codes WHERE hash=?", (sha256_hex(form.get("code") or ""),)).fetchone()
            if row is not None:
                db.execute("DELETE FROM oauth_codes WHERE hash=?", (row["hash"],))
        if row is None or row["expires"] <= now or row["client_id"] != client_id:
            raise OAuthError("invalid_grant", "Authorization code is invalid or expired")
        if form.get("redirect_uri") and form["redirect_uri"] != row["redirect_uri"]:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        verifier = form.get("code_verifier") or ""
        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or not hmac.compare_digest(
                pkce_challenge(verifier), row["code_challenge"]):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        if not self.resource_ok(form.get("resource")):
            raise OAuthError("invalid_target", f"This server is {self.settings.mcp_url}")
        if not self.store.one("SELECT id FROM users WHERE id=? AND disabled=0", (row["user_id"],)):
            raise OAuthError("invalid_grant", "The account that approved this request is no longer active")
        grant_id = "g_" + uuid.uuid4().hex[:16]
        self.store.execute("INSERT INTO grants(id, kind, label, client_id, scopes, projects, resource, created, "
                           "user_id) VALUES (?, 'oauth', ?, ?, ?, ?, ?, ?, ?)",
                           (grant_id, client["name"], client_id, row["scopes"], row["projects"], row["resource"],
                            now, row["user_id"]))
        self.store.execute("UPDATE oauth_clients SET last_used=? WHERE id=?", (now, client_id))
        self.rt.audit(f"OAuth:{client['name']}", "oauth.authorized", grant_id, user_id=row["user_id"])
        return self.rt.auth.issue_oauth_tokens(grant_id)

    def _refresh(self, form: dict, client_id: str | None) -> dict:
        now = time.time()
        row = self.store.one("SELECT t.*, g.client_id, g.revoked, g.user_id, u.disabled FROM tokens t "
                             "JOIN grants g ON g.id = t.grant_id JOIN users u ON u.id = g.user_id "
                             "WHERE t.hash=? AND t.kind='refresh'", (sha256_hex(form.get("refresh_token") or ""),))
        if row is None or row["revoked"] or row["disabled"] or row["expires"] <= now:
            raise OAuthError("invalid_grant", "Refresh token is invalid or expired")
        if client_id and client_id != row["client_id"]:
            raise OAuthError("invalid_grant", "Refresh token was issued to another client")
        if not self.resource_ok(form.get("resource")):
            raise OAuthError("invalid_target", f"This server is {self.settings.mcp_url}")
        claimed = row["used"] is None and self.store.execute(
            "UPDATE tokens SET used=? WHERE hash=? AND used IS NULL", (now, row["hash"])) == 1
        if not claimed:
            # A rotated token presented again (outside a short race window) suggests theft: revoke the grant.
            if row["used"] is not None and now - row["used"] > 30:
                self.rt.auth.revoke_grant(row["grant_id"])
                self.rt.audit("oauth", "grant.revoked", row["grant_id"], "REFRESH_TOKEN_REUSED", user_id=row["user_id"])
            raise OAuthError("invalid_grant", "Refresh token was already used")
        self.store.execute("UPDATE oauth_clients SET last_used=? WHERE id=?", (now, row["client_id"]))
        return self.rt.auth.issue_oauth_tokens(row["grant_id"])

    def revoke(self, token: str | None) -> None:
        row = self.store.one("SELECT * FROM tokens WHERE hash=?", (sha256_hex(token or ""),))
        if row is None:
            return
        if row["kind"] == "refresh":
            self.rt.auth.revoke_grant(row["grant_id"])
        elif row["kind"] == "access":
            self.store.execute("DELETE FROM tokens WHERE hash=?", (row["hash"],))


async def read_form(request: Request) -> dict:
    raw = await request.body()
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return {k: str(v) for k, v in data.items() if isinstance(v, (str, int))} if isinstance(data, dict) else {}
    return {key: values[0] for key, values in parse_qs(raw.decode("utf-8", errors="replace"),
                                                        keep_blank_values=True).items()}


def basic_client_id(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        return unquote(base64.b64decode(header[6:]).decode("utf-8").split(":", 1)[0]) or None
    except (ValueError, UnicodeDecodeError):
        return None


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _error_page(message: str) -> str:
    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>授权失败 · WebCoder</title>"
            "<body style='font-family:system-ui,sans-serif;max-width:560px;margin:80px auto;padding:0 20px'>"
            f"<h2>无法完成授权</h2><p>{html.escape(message)}</p></body></html>")


def make_oauth_router(rt) -> APIRouter:
    router = APIRouter()
    service: OAuthService = rt.oauth

    @router.get("/.well-known/oauth-protected-resource")
    @router.get("/.well-known/oauth-protected-resource/mcp")
    async def protected_resource():
        return JSONResponse(service.protected_resource_metadata(), headers=CORS)

    @router.get("/.well-known/oauth-authorization-server")
    async def authorization_server():
        return JSONResponse(service.server_metadata(), headers=CORS)

    @router.post("/oauth/register")
    async def register(request: Request):
        rt.limiter.check("register:" + client_ip(request), 30, 60)
        try:
            body = json.loads(await request.body() or b"{}")
            result = service.register(body)
        except ValueError:
            return JSONResponse({"error": "invalid_client_metadata", "error_description": "Body must be JSON"},
                                status_code=400, headers=NO_STORE)
        except OAuthError as exc:
            return JSONResponse(exc.body(), status_code=exc.status, headers=NO_STORE)
        rt.audit("oauth", "client.registered", f"{result['client_name']} {' '.join(result['redirect_uris'])}")
        return JSONResponse(result, status_code=201, headers={**NO_STORE, **CORS})

    @router.get("/oauth/authorize")
    async def authorize(request: Request):
        try:  # each accepted request is stored for 10 minutes
            rt.limiter.check("authorize:" + client_ip(request), 30, 600)
        except ApiError as exc:
            return HTMLResponse(_error_page(exc.message), status_code=exc.status)
        location, problem = service.authorize(request.query_params)
        if problem:
            return HTMLResponse(_error_page(problem), status_code=400)
        return RedirectResponse(location, status_code=302)

    @router.post("/oauth/token")
    async def token(request: Request):
        rt.limiter.check("token:" + client_ip(request), 60, 60)
        form = await read_form(request)
        try:
            result = service.token(form, basic_client_id(request))
        except OAuthError as exc:
            return JSONResponse(exc.body(), status_code=exc.status, headers={**NO_STORE, **CORS})
        return JSONResponse(result, headers={**NO_STORE, **CORS})

    @router.post("/oauth/revoke")
    async def revoke(request: Request):
        rt.limiter.check("revoke:" + client_ip(request), 60, 60)
        form = await read_form(request)
        service.revoke(form.get("token"))
        return Response(status_code=200, headers=CORS)

    return router
