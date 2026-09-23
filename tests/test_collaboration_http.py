"""The collaboration center over real HTTP: panel session/CSRF, MCP tools on both protocol eras, and MCP Events.

Callbacks never leave the process: the event sender is replaced by an in-process receiver."""
import base64
import json

import pytest

from tests.collaboration_support import key
from tests.conftest import Hub
from webcoder.hub.collaboration.common import EVENTS, STATUS_EVENT, TASK_EVENT
from webcoder.hub.collaboration.network import Reply

MODERN = '2026-07-28'
META = {'io.modelcontextprotocol/protocolVersion': MODERN, 'io.modelcontextprotocol/clientCapabilities': {}}
SECRET = 'whsec_' + base64.b64encode(b'fixture-only-signing-material-32b!').decode()
CALLBACK = 'https://example.invalid/callback-private-marker'


def modern(hub, token, method, params=None, headers=None):
    params = {**(params or {}), '_meta': META}
    sent = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': MODERN, 'Mcp-Method': method}
    if token:
        sent['Authorization'] = f'Bearer {token}'
    if method in ('tools/call', 'events/subscribe', 'events/unsubscribe') and 'name' in params:
        sent['Mcp-Name'] = params['name']
    sent.update(headers or {})
    return hub.http.post('/mcp', json={'jsonrpc': '2.0', 'id': 7, 'method': method, 'params': params}, headers=sent)


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def collab_hub(tmp_path):
    instance = Hub(tmp_path / 'hub-data', collaboration_enabled=True, mcp_events_enabled=True)
    device = instance.add_device('fixture-pc')
    # The panel checks a new project's folder through its online agent; these tests need no agent, so the
    # project row is written directly (owned by the panel's admin).
    store = instance.runtime.store
    owner = store.one("SELECT id FROM users WHERE username='admin'")['id']
    store.execute("INSERT INTO projects(id, alias, alias_key, device_id, root, mode, created, user_id) "
                  "VALUES ('prj_fixture', 'imago', 'imago', ?, ?, 'rw', 0, ?)", (device['device_id'], str(tmp_path), owner))
    instance.project = {'id': 'prj_fixture', 'alias': 'imago'}
    yield instance
    instance.stop()


def connection(hub, label, scopes=('read',)):
    token = hub.create_token(scopes=scopes, projects=[hub.project['id']], label=label)
    grants = ok(hub.panel.get('/api/collaboration/grants'))['grants']
    return token, next(grant['id'] for grant in grants if grant['label'] == label)


def test_everything_stays_off_by_default(hub):
    token = hub.create_token(scopes=('read',))
    names = {tool['name'] for tool in hub.rpc(token, 'tools/list').json()['result']['tools']}
    assert not any(name.startswith(('collaboration_', 'monitor_')) for name in names)
    assert hub.rpc(token, 'tools/call', {'name': 'collaboration_read', 'arguments': {}}).json()['error']['code'] == -32602
    status = ok(hub.panel.get('/api/collaboration/status'))
    assert not any(status['features'].values())
    discover = modern(hub, token, 'server/discover')
    assert discover.status_code == 400 and 'initialize' in discover.json()['error']['message']
    refused = hub.panel.post('/api/collaboration/room', json={'project': 'x', 'idempotency_key': key()})
    assert refused.status_code == 409 and refused.json()['error']['code'] == 'COLLABORATION_DISABLED'


def test_panel_and_mcp_share_records_but_not_owner_authority(collab_hub):
    hub = collab_hub
    status = ok(hub.panel.get('/api/collaboration/status'))
    assert status['features']['enabled'] and status['can_manage'] and not status['production_actions_enabled']
    scope = {'project': 'imago', 'environment_id': 'production'}
    room = ok(hub.panel.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))['room']
    token, grant_id = connection(hub, 'Work fixture')
    agent = ok(hub.panel.post('/api/collaboration/agent', json={**scope, 'label': 'Work fixture', 'kind': 'work_cloud',
                                                                'grant_id': grant_id, 'idempotency_key': key()}))
    args = {**scope, 'room_id': room['id'], 'structured_mentions': [{'agent_id': agent['id']}],
            'request': 'Read synthetic project information', 'source_message_id': key(), 'idempotency_key': key()}
    denied = hub.panel.post('/api/collaboration/command', json=args, headers={'X-CSRF-Token': 'wrong'})
    assert denied.status_code == 403
    proposed = ok(hub.rpc(token, 'tools/call', {'name': 'collaboration_command_create', 'arguments': args}))['result']
    assert not proposed.get('isError') and proposed['structuredContent']['state'] == 'awaiting_approval'
    overview = ok(hub.panel.get('/api/collaboration', params=scope))
    assert overview['counts']['open_jobs'] == 0 and overview['counts']['pending_proposals'] == 1
    accepted = ok(hub.panel.post('/api/collaboration/control', json={
        **scope, 'target_id': proposed['structuredContent']['id'], 'action': 'accept_proposal', 'expected_version': 1,
        'reason': 'Approve the bounded read-only analysis', 'idempotency_key': key()}))
    claim = ok(modern(hub, token, 'tools/call', {'name': 'collaboration_claim', 'arguments': {
        **scope, 'job_id': accepted['job_id'], 'expected_version': 1, 'idempotency_key': key()}}))['result']
    assert not claim.get('isError') and claim['structuredContent']['state'] == 'leased'
    assert claim['resultType'] == 'complete'
    malicious = hub.panel.post('/api/collaboration/command', json={**args, 'author': 'owner'})
    assert malicious.status_code == 422
    outside = ok(hub.rpc(token, 'tools/call', {'name': 'collaboration_read', 'arguments': {
        **scope, 'project': 'not-authorized', 'kind': 'job', 'id': accepted['job_id']}}))['result']
    assert outside['isError'] and outside['structuredContent']['error']['code'] == 'PROJECT_NOT_FOUND'
    audit = ok(hub.panel.get('/api/audit'))['entries']
    assert any(entry['action'] == 'collaboration_claim' and entry['status'] == 'ok' for entry in audit)


def test_events_discovery_and_old_clients_are_not_conflated(collab_hub):
    hub = collab_hub
    token, _ = connection(hub, 'events readonly fixture')
    discovered = ok(modern(hub, token, 'server/discover'))['result']
    assert 'events' in discovered['capabilities'] and discovered['supportedVersions'][0] == MODERN
    assert discovered['resultType'] == 'complete' and discovered['cacheScope'] == 'private'
    assert 'WAJ-' in discovered['instructions']
    old = ok(hub.rpc(token, 'initialize', {'protocolVersion': '2025-11-25'}))['result']
    assert 'events' not in old['capabilities']
    assert ok(hub.rpc(token, 'events/list', {}))['error']['code'] == -32601
    catalog = ok(modern(hub, token, 'events/list'))['result']
    assert {event['name'] for event in catalog['events']} == set(EVENTS) and catalog['resultType'] == 'complete'
    assert all(name.startswith('webcoder.') for name in EVENTS)
    args = {'name': TASK_EVENT, 'arguments': {}, 'delivery': {'mode': 'webhook', 'url': CALLBACK, 'secret': 'invalid'}}
    mismatch = modern(hub, token, 'events/subscribe', args, headers={'Mcp-Name': 'wrong'})
    assert mismatch.status_code == 400 and mismatch.json()['error']['code'] == -32020


def test_events_catalog_reuses_an_existing_broad_grant(collab_hub):
    token, _ = connection(collab_hub, 'Existing broad fixture', scopes=('read', 'write', 'cli'))
    catalog = ok(modern(collab_hub, token, 'events/list'))['result']
    assert {event['name'] for event in catalog['events']} == set(EVENTS)


def test_modern_requests_follow_the_2026_contract(collab_hub):
    hub = collab_hub
    token, _ = connection(hub, 'protocol fixture')
    listed = ok(modern(hub, token, 'tools/list'))['result']
    names = {tool['name'] for tool in listed['tools']}
    assert {'read_file', 'collaboration_read', 'collaboration_join', 'collaboration_work_execute'} <= names
    assert listed['ttlMs'] == 0 and listed['resultType'] == 'complete'
    assert listed['_meta']['io.modelcontextprotocol/serverInfo']['title'] == 'WebCoder'
    no_meta = hub.http.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}},
                            headers={'Authorization': f'Bearer {token}', 'MCP-Protocol-Version': MODERN,
                                     'Mcp-Method': 'tools/list'})
    assert no_meta.status_code == 400 and no_meta.json()['error']['code'] == -32602
    wrong_method = modern(hub, token, 'tools/list', headers={'Mcp-Method': 'tools/call'})
    assert wrong_method.status_code == 400 and wrong_method.json()['error']['code'] == -32020
    assert modern(hub, token, 'ping').status_code == 404
    notification = hub.http.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized',
                                               'params': {'_meta': META}},
                                 headers={'Authorization': f'Bearer {token}', 'MCP-Protocol-Version': MODERN,
                                          'Mcp-Method': 'notifications/initialized'})
    assert notification.status_code == 404
    unknown = modern(hub, token, 'tools/call', {'name': 'no_such_tool', 'arguments': {}})
    assert unknown.status_code == 400 and unknown.json()['error']['code'] == -32602


def test_subscribe_challenge_unsubscribe_over_http(collab_hub):
    hub = collab_hub
    token, _ = connection(hub, 'subscriber fixture')
    received = []

    async def receiver(url, body, headers):
        received.append((url, json.loads(body), headers))
        payload = json.loads(body)
        return Reply(200, json.dumps({'challenge': payload['challenge']}).encode())

    hub.runtime.collaboration.events.sender = receiver
    filters = {'project_id': hub.project['id'], 'environment_id': 'production'}
    args = {'name': STATUS_EVENT, 'arguments': filters, 'delivery': {'mode': 'webhook', 'url': CALLBACK, 'secret': SECRET}}
    saved = ok(modern(hub, token, 'events/subscribe', args))['result']
    assert {'id', 'refreshBefore', 'cursor', 'truncated'} <= set(saved) and saved['resultType'] == 'complete'
    assert len(received) == 1 and received[0][0] == CALLBACK and received[0][1]['type'] == 'verification'
    assert received[0][2]['webhook-signature'].startswith('v1,')
    again = ok(modern(hub, token, 'events/subscribe', args))['result']  # verified recently: no second challenge
    assert again['id'] == saved['id'] and len(received) == 1
    room = ok(hub.panel.get('/api/collaboration', params={'project': 'imago'}))['room']
    assert room and room['project_id'] == hub.project['id']  # a passive room, nothing else
    stop = {'name': STATUS_EVENT, 'arguments': filters, 'delivery': {'mode': 'webhook', 'url': CALLBACK}}
    assert ok(modern(hub, token, 'events/unsubscribe', stop))['result']['resultType'] == 'complete'
    unknown = modern(hub, token, 'events/unsubscribe', {**stop, 'delivery': {'mode': 'webhook', 'url': CALLBACK + 'x'}})
    assert unknown.json()['error']['code'] == -32011 and unknown.json()['error']['data']['kind'] == 'subscription'
    bad_key = modern(hub, token, 'events/subscribe', {**args, 'delivery': {**args['delivery'], 'secret': 'nope'}})
    assert bad_key.status_code == 400 and bad_key.json()['error']['code'] == -32602
    by_alias = modern(hub, token, 'events/subscribe', {**args, 'arguments': {**filters, 'project_id': 'imago'}})
    assert by_alias.json()['error']['data']['code'] == 'PROJECT_ID_REQUIRED'


def test_failed_challenge_reports_only_a_fixed_reason(collab_hub):
    hub = collab_hub
    token, _ = connection(hub, 'refused fixture')

    async def refusing(url, body, headers):
        return Reply(404, b'callback-private-marker')

    hub.runtime.collaboration.events.sender = refusing
    args = {'name': STATUS_EVENT, 'arguments': {'project_id': hub.project['id'], 'environment_id': 'production'},
            'delivery': {'mode': 'webhook', 'url': CALLBACK, 'secret': SECRET}}
    failed = modern(hub, token, 'events/subscribe', args)
    assert failed.status_code == 400
    assert failed.json()['error'] == {'code': -32015, 'message': 'Callback verification failed',
                                      'data': {'reason': 'http_4xx'}}
    assert 'callback-private-marker' not in failed.text
    assert hub.runtime.store.one('SELECT COUNT(*) AS n FROM mcp_event_subscriptions')['n'] == 0


def test_dot_chat_joins_subscribes_receives_tests_and_is_confirmed(collab_hub):
    """The whole dot connection: owner slot -> WAJ code in the dot chat -> four host subscriptions -> test events
    really delivered by the hub's event loop -> the owner confirms the chat received them."""
    hub = collab_hub
    scope = {'project': 'imago', 'environment_id': 'production'}
    ok(hub.panel.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))
    slot = ok(hub.panel.post('/api/collaboration/join-slot', json={**scope, 'label': 'dot 协调', 'kind': 'dot',
                                                                   'idempotency_key': key()}))['slot']
    assert slot['status'] == 'invited' and slot['join_code'].startswith('WAJ-')
    assert '@WebCoder' in slot['join_instruction'] and slot['join_code'] in slot['join_instruction']
    token, _ = connection(hub, 'dot chat')
    joined = ok(hub.rpc(token, 'tools/call', {'name': 'collaboration_join', 'arguments': {
        'code': slot['join_code'].lower(), 'idempotency_key': key()}}))['result']['structuredContent']
    assert joined['registered'] and not joined['worker_authorized'] and not joined['chat_identity_verified']
    requests = joined['subscription_requests']
    assert len(requests) == 4  # dot: tasks (dot-coordination queue), status, results and incidents
    assert {r['arguments'].get('queue') for r in requests} == {'dot-coordination', None}
    received = []

    async def receiver(url, body, headers):
        payload = json.loads(body)
        received.append(payload)
        if payload.get('type') == 'verification':
            return Reply(200, json.dumps({'challenge': payload['challenge']}).encode())
        return Reply(204)

    hub.runtime.collaboration.events.sender = receiver
    for request in requests:
        ok(modern(hub, token, 'events/subscribe', {**request, 'delivery': {'mode': 'webhook', 'url': CALLBACK,
                                                                          'secret': SECRET}}))
    view = ok(hub.panel.get('/api/collaboration', params=scope))['join_slots'][0]
    assert view['status'] == 'subscription_verified' and view['subscription_count'] == 4
    ok(hub.panel.post('/api/collaboration/join-slot-control', json={
        **scope, 'slot_id': slot['id'], 'expected_version': view['version'], 'action': 'test', 'idempotency_key': key()}))
    hub.wait_for(lambda: sum(1 for item in received if item.get('data', {}).get('test')) == 4, timeout=30,
                 what='four test events delivered by the event loop')
    view = ok(hub.panel.get('/api/collaboration', params=scope))['join_slots'][0]
    assert view['status'] == 'test_delivered' and len(view['confirmation_event_ids']) == 4
    confirmed = ok(hub.panel.post('/api/collaboration/join-slot-control', json={
        **scope, 'slot_id': slot['id'], 'expected_version': view['version'], 'action': 'confirm_chat',
        'confirmed_received': True, 'test_event_ids': view['confirmation_event_ids'], 'idempotency_key': key()}))
    assert confirmed['slot']['status'] == 'chat_confirmed' and confirmed['slot']['connection_complete']
