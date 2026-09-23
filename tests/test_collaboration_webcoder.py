"""WebCoder-specific parts of the collaboration port: identities, scope mapping, projects, store transactions and
account deletion (the rest is covered by the tests ported from CodePier)."""
import asyncio
import json

import pytest

from tests.collaboration_support import key, seed_grant
from tests.conftest import Hub
from tests.test_collaboration_service import claimed, result_args
from webcoder.hub.collaboration.errors import DevError
from webcoder.hub.collaboration.host import Principal, capabilities, from_connection, refresh_principal
from webcoder.hub.collaboration.schema import purge_owner
from webcoder.hub.store import Store, TransactionAborted


def test_scope_mapping_and_read_only_workers(collab):
    service, owner, worker, dot, room, agents, clock, scope = collab
    assert capabilities(['read', 'write', 'cli', 'ssh', 'mobile']) == {'read', 'write', 'execute', 'ssh', 'computer'}
    seed_grant(service.store, 'shell', 'owner', scopes=('read', 'ssh'))
    with pytest.raises(DevError) as caught:
        service.register_agent({**scope, 'label': 'ssh', 'kind': 'work_cloud', 'grant_id': 'shell',
                                'idempotency_key': key()}, owner)
    assert caught.value.code == 'WORKER_SCOPE_TOO_BROAD'
    seed_grant(service.store, 'other-project', 'owner', projects=('otherproj',))
    with pytest.raises(DevError) as caught:
        service.register_agent({**scope, 'label': 'elsewhere', 'kind': 'dot', 'grant_id': 'other-project',
                                'idempotency_key': key()}, owner)
    assert caught.value.code == 'PROJECT_NOT_FOUND'


def test_principals_are_rebuilt_from_live_rows(collab):
    service, owner, worker, dot, room, agents, clock, scope = collab
    store, host = service.store, service.runtime
    live = refresh_principal(store, worker)
    assert live.scopes == {'read'} and live.projects == ['proj'] and live.space_id == 'owner' and not live.admin
    panel = refresh_principal(store, owner)
    assert panel.admin and set(panel.projects) == {'proj', 'otherproj'} and panel.grant_id is None
    assert host.project('proj', live)['mode'] == 'write' and host.project('PROJ', live)['id'] == 'proj'
    with pytest.raises(DevError):
        host.project('otherproj', live)  # outside the grant's projects
    store.execute("UPDATE grants SET expires=1 WHERE id='worker'")
    with pytest.raises(DevError) as caught:
        refresh_principal(store, worker)
    assert caught.value.code == 'INVALID_TOKEN'
    store.execute("UPDATE users SET disabled=1 WHERE id='owner'")
    with pytest.raises(DevError) as caught:
        refresh_principal(store, owner)
    assert caught.value.code == 'LOGIN_REQUIRED'


def test_connection_principal_keeps_its_webcoder_identity():
    from webcoder.hub.auth import Principal as Connection
    connection = Connection(kind='oauth', grant_id='g_1', label='ChatGPT', scopes=frozenset({'read', 'cli'}),
                            projects=None, user_id='u_1', username='alice', client_id='c_1')
    actor = from_connection(connection)
    assert actor.scopes == {'read', 'execute'} and actor.space_id == 'u_1' and actor.grant_id == 'g_1'
    assert actor.actor == connection.actor and not actor.admin


def test_nested_transactions_join_and_abort_as_a_whole(tmp_path):
    store = Store(tmp_path / 'hub')
    store.execute("INSERT INTO meta(key, value) VALUES ('probe', '0')")
    with pytest.raises(TransactionAborted):
        with store.transaction():
            store.execute("UPDATE meta SET value='1' WHERE key='probe'")
            try:
                with store.transaction():
                    store.execute("UPDATE meta SET value='2' WHERE key='probe'")
                    raise DevError('INNER', 'inner step failed')
            except DevError:
                pass  # carrying on must not commit half a change
    assert store.one("SELECT value FROM meta WHERE key='probe'")['value'] == '0'
    with store.transaction() as db:
        with store.transaction(immediate=False):
            db.execute("UPDATE meta SET value='3' WHERE key='probe'")
    assert store.one("SELECT value FROM meta WHERE key='probe'")['value'] == '3'
    assert asyncio.run(store.run(store.one, "SELECT value FROM meta WHERE key='probe'"))['value'] == '3'
    store.close()


def test_purge_owner_removes_every_collaboration_row(collab):
    service, owner, worker, dot, room, agents, clock, scope = collab
    request, lease = claimed(collab)
    service.submit(result_args(collab, lease), worker)
    service.joining.create({**scope, 'label': 'dot chat', 'kind': 'dot', 'idempotency_key': key()}, owner)
    store = service.store
    tables = [row['name'] for row in store.all(
        "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'collaboration%' OR name LIKE 'conversation%' "
        "OR name LIKE 'coordination%' OR name LIKE 'monitor%' OR name LIKE 'mcp_event%')")]
    assert store.one('SELECT COUNT(*) AS n FROM collaboration_results')['n'] == 1
    with store.transaction() as db:
        purge_owner(db, 'owner')
    assert store.all('PRAGMA foreign_key_check') == []
    leftovers = {table: store.one(f'SELECT COUNT(*) AS n FROM {table}')['n'] for table in tables
                 if table not in ('collaboration_secrets', 'conversation_idempotency')}
    assert not any(leftovers.values()), leftovers


def test_deleting_a_user_with_collaboration_data(tmp_path):
    hub = Hub(tmp_path / 'hub-data', collaboration_enabled=True)
    try:
        store = hub.runtime.store
        bob = hub.runtime.auth.create_user('bob', 'fixture-password')
        store.execute("INSERT INTO devices(id, name, secret_enc, created, user_id) VALUES ('dev_b', 'pc', ?, 0, ?)",
                      (store.encrypt('x' * 43), bob))
        store.execute("INSERT INTO projects(id, alias, alias_key, device_id, root, mode, created, user_id) "
                      "VALUES ('prj_b', 'b', 'b', 'dev_b', '/tmp/b', 'rw', 0, ?)", (bob,))
        bob_panel = Principal('面板:bob', bob, admin=True)
        service = hub.runtime.collaboration
        conversation = service.conversations.create({'title': 'bob room', 'projects': [{'project': 'b'}],
                                                     'idempotency_key': key()}, bob_panel)['conversation']
        service.chatroom.create({'project': 'b', 'room_id': conversation['projects'][0]['room_id'],
                                 'conversation_id': conversation['id'], 'body_text': 'hello', 'idempotency_key': key(),
                                 'client_message_id': key()}, bob_panel)
        assert store.one('SELECT COUNT(*) AS n FROM collaboration_messages')['n'] == 1
        deleted = hub.panel.delete(f'/api/users/{bob}')
        assert deleted.status_code == 200, deleted.text
        assert store.one('SELECT COUNT(*) AS n FROM collaboration_messages')['n'] == 0
        assert store.one('SELECT COUNT(*) AS n FROM conversation_rooms')['n'] == 0
        assert json.loads(json.dumps(store.all('PRAGMA foreign_key_check'))) == []
    finally:
        hub.stop()


def test_tool_status_reports_a_call_that_ran_but_did_not_succeed(collab):
    from webcoder.hub.auth import Principal as Connection
    from webcoder.hub.tools import Tool, execute

    async def failing(ctx, args):
        ctx.audit_status = 'EXIT_2'
        return 'ran, exit code 2'

    tool = Tool('fixture_run', 'Fixture', 'Fixture tool', 'read',
                {'type': 'object', 'properties': {}, 'additionalProperties': False}, {}, failing)
    connection = Connection(kind='pat', grant_id='worker', label='worker', scopes=frozenset({'read'}),
                            projects=('proj',), user_id='owner', username='owner')
    result, is_error = asyncio.run(execute(collab[0].runtime.rt, connection, tool, {}))
    assert not is_error and result.status == 'EXIT_2' and result.text == 'ran, exit code 2'
