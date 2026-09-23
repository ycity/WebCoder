"""How the collaboration center sees WebCoder: identities, projects and the tool execution path.

The collaboration modules (ported from CodePier) expect a Principal with a Space, a grant and the capabilities
read / write / execute, and a runtime that resolves projects. WebCoder has no Spaces: every user owns their own
devices and projects, so a user's personal Space is the user (space_id = user_id). A panel session is that
owner; an MCP connection is one row of `grants`. WebCoder's scopes map onto collaboration capabilities as:
read -> read, write -> write, cli -> execute (programs on the project's computer); ssh and mobile reach other
machines and are never collaboration capabilities, so a connection holding them is not a read-only worker.
Every check re-reads the live user, grant and project rows: IDs kept in collaboration tables grant nothing.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace

from webcoder.common.crypto import sha256_hex
from webcoder.hub.collaboration.errors import DevError

SCOPE_CAPABILITY = {'read': 'read', 'write': 'write', 'cli': 'execute', 'ssh': 'ssh', 'mobile': 'computer'}
OWNER_CAPABILITIES = frozenset({'read', 'write', 'execute'})
TOOL_CAPABILITY = {'read': 'read', 'write': 'write', 'cli': 'execute'}  # WebCoder tool scope -> capability
GOAL_TOOL_EXCLUDED = frozenset({'view_image'})  # shows a picture to the person, not a work step


@dataclass
class Principal:
    actor: str
    user_id: str
    scopes: set = field(default_factory=set)
    projects: list = field(default_factory=list)
    grant_id: str | None = None
    admin: bool = False
    profile_id: str | None = None
    authorization_mode: str = 'fixed'
    role_id: str | None = None
    space_id: str = ''
    instance_admin: bool = False
    user_epoch: int | None = None
    identity_id: str | None = None
    session_hash: str = ''
    token_hash: str = ''
    username: str = ''
    label: str = ''
    kind: str = ''  # 'panel', 'pat' or 'oauth'
    client_id: str | None = None
    source_scopes: list = field(default_factory=list)  # the grant's own WebCoder scopes


def capabilities(scopes) -> set:
    return {SCOPE_CAPABILITY[scope] for scope in scopes if scope in SCOPE_CAPABILITY}


def from_connection(connection) -> Principal:
    """The collaboration principal of an authenticated MCP request (webcoder.hub.auth.Principal)."""
    return Principal(actor=connection.actor, user_id=connection.user_id, scopes=capabilities(connection.scopes),
                     grant_id=connection.grant_id, space_id=connection.user_id, username=connection.username,
                     label=connection.label, kind=connection.kind, client_id=connection.client_id,
                     source_scopes=sorted(connection.scopes))


def from_session(row: dict, cookie: str | None) -> Principal:
    """The collaboration principal of a signed-in panel user: the owner of their own projects."""
    return Principal(actor='面板:' + row['username'], user_id=row['user_id'], scopes=set(OWNER_CAPABILITIES),
                     admin=True, space_id=row['user_id'], username=row['username'], kind='panel',
                     session_hash=sha256_hex(cookie) if cookie else '')


def refresh_principal(store, principal: Principal) -> Principal:
    """Re-read the authority behind a principal: an enabled user, and for a connection its grant (not revoked
    or expired) with its current scopes and projects; for a panel session, the session itself."""
    now = time.time()
    user = store.one('SELECT id, username, disabled FROM users WHERE id=?', (principal.user_id,))
    if not user or user['disabled']:
        raise DevError('LOGIN_REQUIRED', '账号不存在或已停用', 401)
    if principal.space_id not in ('', user['id']):
        raise DevError('SPACE_MISMATCH', '记录不属于当前账号', 403)
    owned = [row['id'] for row in store.all('SELECT id FROM projects WHERE user_id=? ORDER BY id', (user['id'],))]
    if principal.grant_id:
        grant = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
        if (not grant or grant['revoked'] or grant['user_id'] != user['id']
                or (grant['expires'] and grant['expires'] <= now)):
            raise DevError('INVALID_TOKEN', '连接授权已撤销或到期', 401)
        if principal.token_hash and not store.one(
                'SELECT 1 FROM tokens WHERE hash=? AND grant_id=? AND expires>?',
                (principal.token_hash, grant['id'], now)):
            raise DevError('INVALID_TOKEN', '凭据已过期或撤销', 401)
        source = json.loads(grant['scopes'])
        if 'read' not in source:  # every WebCoder credential includes read; one without it is not usable here
            raise DevError('INSUFFICIENT_SCOPE', '连接缺少读取权限', 403, required_scope='read')
        allowed = json.loads(grant['projects'])
        return replace(principal, scopes=capabilities(source), source_scopes=sorted(source), admin=False,
                       projects=[pid for pid in owned if allowed == '*' or pid in allowed], space_id=user['id'],
                       username=user['username'], label=grant['label'], kind=grant['kind'],
                       client_id=grant['client_id'])
    if principal.session_hash and not store.one(
            'SELECT 1 FROM sessions WHERE id_hash=? AND user_id=? AND expires>?',
            (principal.session_hash, user['id'], now)):
        raise DevError('LOGIN_REQUIRED', '登录已过期，请重新登录', 401)
    return replace(principal, scopes=set(OWNER_CAPABILITIES), projects=owned, admin=True, space_id=user['id'],
                   username=user['username'], kind=principal.kind or 'panel')


def goal_tools() -> dict:
    """WebCoder tools a work item may run, by name -> capability: those working inside one project."""
    from webcoder.hub.mcp import ALL_TOOLS
    return {name: TOOL_CAPABILITY[tool.scope] for name, tool in ALL_TOOLS.items()
            if tool.scope in TOOL_CAPABILITY and 'project' in tool.schema.get('properties', {})
            and name not in GOAL_TOOL_EXCLUDED}


class HostRuntime:
    """The `runtime` the collaboration modules call: projects, authorization and execution in WebCoder."""

    def __init__(self, rt):
        self.rt = rt
        self.store = rt.store
        self.settings = rt.settings
        self.dispatch_lock = asyncio.Lock()

    # ------------------------------------------------------------------ projects
    def _rows(self, user_id: str) -> list[dict]:
        return self.store.all('SELECT p.*, d.name AS device_name, d.enabled AS device_enabled, d.info AS device_info '
                              'FROM projects p JOIN devices d ON d.id = p.device_id WHERE p.user_id=? '
                              'ORDER BY p.alias_key', (user_id,))

    @staticmethod
    def _view(row: dict) -> dict:
        info = json.loads(row['device_info'] or '{}')
        programs = [item.get('name') for item in (info.get('commands') or {}).get('programs') or []]
        return {'id': row['id'], 'alias': row['alias'], 'device_id': row['device_id'], 'root': row['root'],
                'mode': 'write' if row['mode'] == 'rw' else 'read', 'allow_tasks': bool(programs),
                'device_name': row['device_name'], 'device_enabled': bool(row['device_enabled']),
                'description': row['description']}

    def project(self, ref, principal: Principal) -> dict:
        """The project `ref` (id or alias) if this principal may see it now."""
        wanted = str(ref or '').strip()
        visible = set(principal.projects or ())
        for row in self._rows(principal.user_id):
            if row['id'] == wanted or row['alias_key'] == wanted.lower():
                if row['id'] in visible:
                    return self._view(row)
                break
        raise DevError('PROJECT_NOT_FOUND', '当前授权下没有这个项目', 404)

    def list_projects(self, principal: Principal) -> list[dict]:
        visible = set(principal.projects or ())
        return [self._view(row) for row in self._rows(principal.user_id) if row['id'] in visible]

    def authorize(self, principal: Principal, capability: str, *, project_id: str | None = None) -> None:
        """May this principal use `capability` (read / write / execute) on the project right now?"""
        if capability not in principal.scopes:
            raise DevError('INSUFFICIENT_SCOPE', '连接当前缺少所需能力', 403, required_scope=capability)
        if project_id is None:
            return
        project = self.project(project_id, principal)
        if capability in ('write', 'execute') and project['mode'] != 'write':
            raise DevError('READ_ONLY', '项目是只读的', 403)
        if capability == 'execute' and not project['allow_tasks']:
            raise DevError('TASKS_DISABLED', '项目所在电脑没有允许运行任何程序', 403)
        if not project['device_enabled']:
            raise DevError('DEVICE_DISABLED', '项目所在电脑已在面板中停用', 403)

    # ------------------------------------------------------------------ goal steps
    def connection(self, principal: Principal):
        """The WebCoder credential (auth.Principal) behind a collaboration principal, as tools expect it."""
        from webcoder.hub.auth import Principal as Connection
        grant = self.store.one('SELECT g.*, u.username FROM grants g JOIN users u ON u.id = g.user_id '
                               'WHERE g.id=?', (principal.grant_id,))
        if not grant:
            raise DevError('INVALID_TOKEN', '连接授权已撤销或到期', 401)
        projects = json.loads(grant['projects'])
        return Connection(kind=grant['kind'], grant_id=grant['id'], label=grant['label'],
                          scopes=frozenset(json.loads(grant['scopes'])),
                          projects=None if projects == '*' else tuple(projects), user_id=grant['user_id'],
                          username=grant['username'], client_id=grant['client_id'])

    async def run_tool(self, principal: Principal, name: str, arguments: dict, timeout: float):
        """Run one WebCoder tool for a work step, exactly as an MCP tools/call would (checks, audit log).
        Returns (text, structured, is_error, status): status is set when the call ran but did not succeed
        (a program's non-zero exit, EXIT_1 ...), which is no tool error but no success either."""
        from webcoder.hub.mcp import ALL_TOOLS
        from webcoder.hub.tools import execute
        connection = await self.store.run(self.connection, principal)
        tool = ALL_TOOLS[name]
        if not tool.required <= connection.scopes:
            return f'Error INSUFFICIENT_SCOPE: this connection may not use {name}', None, True, None
        outcome, is_error = await asyncio.wait_for(execute(self.rt, connection, tool, arguments), timeout)
        return outcome.text, outcome.structured, is_error, outcome.status
