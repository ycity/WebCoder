"""Panel entry: authenticated sessions, current project scopes and CSRF checks."""
import json
import time
from dataclasses import asdict

from fastapi import APIRouter, Query, Request

from webcoder.hub.auth import SESSION_COOKIE
from webcoder.hub.collaboration.errors import DevError
from webcoder.hub.collaboration.host import capabilities, from_session


def make_router(rt):
    router = APIRouter(prefix='/api/collaboration')
    service, store = rt.collaboration, rt.store

    def panel(request, write=False):
        # The signed-in user is the owner of their own projects; writes also need the CSRF header.
        return from_session(rt.auth.user(request, write=write), request.cookies.get(SESSION_COOKIE))

    @router.get('/status')
    async def status(request: Request):
        def inspect():
            principal = panel(request)
            return {'features': asdict(service.config), 'components': service.health,
                    'schema_version': 3, 'capabilities': service.chatroom.capabilities(),
                    'can_manage': bool(principal.admin and not principal.grant_id),
                    'production_actions_enabled': False, 'platform_poc_verified': False}
        return await store.run(inspect)

    @router.get('/grants')
    async def grants(request: Request):
        """The owner's connections that are still usable, with collaboration capabilities for their scopes."""
        def inspect():
            principal = panel(request)
            now = time.time()
            rows = store.all('''SELECT g.id, g.kind, g.label, g.scopes, g.created, g.expires, g.last_used,
                (SELECT max(t.expires) FROM tokens t WHERE t.grant_id=g.id AND t.kind='refresh' AND t.used IS NULL)
                AS refresh_expires FROM grants g WHERE g.user_id=? AND g.revoked=0 ORDER BY g.created DESC''',
                (principal.user_id,))
            result = []
            for row in rows:
                expires = row['expires'] if row['kind'] == 'pat' else row['refresh_expires']
                if expires is not None and expires <= now:
                    continue
                source = json.loads(row['scopes'])
                result.append({'id': row['id'], 'grant_id': row['id'], 'kind': row['kind'], 'label': row['label'],
                               'scopes': sorted(capabilities(source)), 'source_scopes': source, 'revoked': False,
                               'expires': expires, 'last_used': row['last_used'], 'created': row['created']})
            return {'grants': result}
        return await store.run(inspect)

    @router.get('/conversations')
    async def conversations(request: Request):
        return await store.run(lambda: service.conversations.listing(panel(request)))

    @router.get('')
    async def read(request: Request, project: str, environment_id: str = 'production',
                   kind: str = 'overview', id: str = '', cursor: str = '',
                   room_id: str = '', conversation_id: str = '', after: str = '', query: str = '', client_message_id: str = '',
                   job_id: str = '', result_id: str = '', limit: int = Query(default=40, ge=1, le=100)):
        def inspect():
            principal = panel(request)
            # job_id / result_id name the context of a room-scoped evidence read (which task or result cites it).
            result = service.read({'project': project, 'environment_id': environment_id,
                                   'kind': kind, 'id': id, 'cursor': cursor, 'limit': limit,
                                   'room_id': room_id, 'conversation_id': conversation_id, 'after': after, 'query': query, 'client_message_id': client_message_id,
                                   'job_id': job_id, 'result_id': result_id}, principal)
            return {**result, 'components': service.health}
        return await store.run(inspect)

    @router.post('/{operation}')
    async def mutate(operation: str, body: dict, request: Request):
        def perform():
            principal = panel(request, write=True)
            handlers = {'goal-create': service.coordination.create, 'goal-update': service.coordination.update,
                        'goal-approve': service.coordination.approve, 'goal-control': service.coordination.control,
                        'goal-message': service.coordination.message, 'work-create': service.coordination.work_create,
                        'work-assign': service.coordination.work_assign,
                        'conversation': service.conversations.create, 'conversation-project': service.conversations.add_project,
                        'message': service.chatroom.create, 'message-to-task': service.chatroom.to_task,
                        'message-access': service.chatroom.access, 'read-cursor': service.chatroom.set_read_cursor,
                        'room': service.room_create, 'agent': service.register_agent,
                        'join-slot': service.joining.create, 'join-slot-control': service.joining.control,
                        'command': lambda args, actor: service.command(args, actor, from_panel=True),
                        'control': service.control, 'probe': service.monitor.register_probe,
                        'plan-validate': service.monitor.validate_plan,
                        'plan-save': service.monitor.save_plan, 'plan-activate': service.monitor.activate_plan}
            if operation not in handlers:
                raise DevError('NOT_FOUND', '未知的协作管理操作', 404)
            return handlers[operation](body, principal)
        return await store.run(perform)

    return router
