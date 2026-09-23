"""Owner-approved goals over real HTTP / MCP, with steps running through a real paired agent."""
import pytest

from tests.collaboration_support import key
from tests.conftest import AgentThread, Hub
from webcoder.agent.__main__ import pair_device


@pytest.fixture
def goal_hub(tmp_path):
    hub = Hub(tmp_path / 'hub-data', collaboration_enabled=True)
    root = tmp_path / 'imago'
    root.mkdir()
    device = hub.add_device('fixture-pc')
    config = pair_device(hub.url, device['code'], [str(root)], tmp_path / 'agent' / 'config.json', proxy='none')
    runner = AgentThread(config)
    runner.start()
    hub.wait_for(lambda: hub.device(device['device_id'])['online'], what='agent online')
    hub.project = hub.add_project('imago', device['device_id'], root)
    hub.root = root
    yield hub
    runner.stop()
    hub.stop()


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def grant(hub, label, scopes):
    token = hub.create_token(scopes=scopes, projects=[hub.project['id']], label=label)
    grants = ok(hub.panel.get('/api/collaboration/grants'))['grants']
    return token, next(row['id'] for row in grants if row['label'] == label)


def mcp(hub, token, name, arguments, *, success=True):
    result = ok(hub.rpc(token, 'tools/call', {'name': name, 'arguments': arguments}))['result']
    assert bool(result.get('isError')) is not success, result
    return result['structuredContent']


def test_owner_goal_approval_csrf_and_real_file_execution(goal_hub):
    hub = goal_hub
    scope = {'project': hub.project['id'], 'environment_id': 'production'}
    room = ok(hub.panel.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))['room']
    writer, writer_id = grant(hub, 'writer fixture', ('read', 'write'))
    reviewer, reviewer_id = grant(hub, 'readonly review fixture', ('read',))
    options = ok(hub.panel.get('/api/collaboration', params={**scope, 'conversation_id': room['id'],
                                                             'kind': 'coordination_options'}))
    assert options['can_approve'] and writer_id in {row['grant_id'] for row in options['participants']}
    capabilities = {row['grant_id']: row['project_capabilities'][hub.project['id']] for row in options['participants']}
    assert capabilities[writer_id] == ['read', 'write'] and capabilities[reviewer_id] == ['read']
    proposal = {**scope, 'conversation_id': room['id'], 'objective': 'Create a fixture file, then review it.',
                'acceptance': 'An actual write and read with provenance', 'project_ids': [hub.project['id']],
                'participant_grant_ids': [writer_id, reviewer_id], 'capabilities': ['read', 'write'],
                'idempotency_key': key()}
    goal = mcp(hub, writer, 'collaboration_goal_create', proposal)['goal']
    assert goal['state'] == 'proposed'
    approval = {**scope, 'goal_id': goal['id'], 'expected_version': goal['version'], 'digest': goal['digest'],
                'idempotency_key': key()}
    assert hub.panel.post('/api/collaboration/goal-approve', json=approval,
                          headers={'X-CSRF-Token': 'invalid'}).status_code == 403
    refused = ok(hub.rpc(writer, 'tools/call', {'name': 'collaboration_goal_approve', 'arguments': approval}))
    assert refused['error']['code'] == -32602  # no such tool: only the signed-in owner approves
    goal = ok(hub.panel.post('/api/collaboration/goal-approve', json=approval))['goal']
    assert goal['state'] == 'active'
    work = mcp(hub, writer, 'collaboration_work_create', {
        **scope, 'goal_id': goal['id'], 'objective': 'Create fixture.txt', 'acceptance': 'File contains verified fixture',
        'target_project_id': hub.project['id'], 'assignee_grant_id': writer_id,
        'required_capabilities': ['read', 'write'], 'idempotency_key': key()})['work_item']
    work = mcp(hub, writer, 'collaboration_work_claim', {**scope, 'goal_id': goal['id'], 'work_item_id': work['id'],
                                                         'expected_version': work['version'],
                                                         'idempotency_key': key()})['work_item']
    lease = {**scope, 'goal_id': goal['id'], 'work_item_id': work['id'], 'attempt': work['attempt'],
             'fencing_token': work['fencing_token']}
    write = mcp(hub, writer, 'collaboration_work_execute', {**lease, 'tool': 'write_file', 'arguments': {
        'path': 'fixture.txt', 'content': 'verified fixture\n'}, 'idempotency_key': key()})
    assert write['state'] == 'succeeded', write
    assert (hub.root / 'fixture.txt').read_text() == 'verified fixture\n'
    read = mcp(hub, writer, 'collaboration_work_execute', {**lease, 'tool': 'read_file',
                                                           'arguments': {'path': 'fixture.txt'}, 'idempotency_key': key()})
    assert read['state'] == 'succeeded' and 'verified fixture' in read['output']
    denied = mcp(hub, writer, 'collaboration_work_execute', {**lease, 'tool': 'cli_run', 'arguments': {'program': 'echo'},
                                                             'idempotency_key': key()}, success=False)
    assert denied['error']['code'] == 'GOAL_CAPABILITY_DENIED'
    result = mcp(hub, writer, 'collaboration_work_result', {
        **lease, 'outcome': 'succeeded', 'summary': 'Fixture write and read verified',
        'operation_ids': [write['operation_id'], read['operation_id']], 'idempotency_key': key()})['work_item']
    assert result['result']['execution_verified'] and result['provenance_project_ids'] == [hub.project['id']]
    review = mcp(hub, reviewer, 'collaboration_work_create', {
        **scope, 'goal_id': goal['id'], 'objective': 'Review the fixture evidence', 'acceptance': 'Record limitations',
        'target_project_id': hub.project['id'], 'assignee_grant_id': reviewer_id, 'dependencies': [work['id']],
        'required_capabilities': ['read'], 'idempotency_key': key()})['work_item']
    assert review['state'] == 'queued'
    detail = mcp(hub, reviewer, 'collaboration_goal_read', {**scope, 'goal_id': goal['id']})
    assert len(detail['work_items']) == 3 and len(detail['operations']) == 2
    assert {op['state'] for op in detail['operations']} == {'succeeded'} and not detail['can_approve']
    audit = ok(hub.panel.get('/api/audit'))['entries']
    assert any(entry['action'] == 'write_file' and entry['target'] == 'imago:fixture.txt' for entry in audit)
    paused = ok(hub.panel.post('/api/collaboration/goal-control', json={
        **scope, 'goal_id': goal['id'], 'expected_version': goal['version'], 'action': 'pause', 'idempotency_key': key()}))
    assert paused['goal']['state'] == 'paused'
    blocked = mcp(hub, reviewer, 'collaboration_work_claim', {**scope, 'goal_id': goal['id'], 'work_item_id': review['id'],
                                                              'expected_version': review['version'],
                                                              'idempotency_key': key()}, success=False)
    assert blocked['error']['code'] == 'GOAL_INACTIVE'


def test_disabled_feature_does_not_expose_goal_execution(hub):
    token = hub.create_token(scopes=('read', 'write'))
    tools = ok(hub.rpc(token, 'tools/list'))['result']['tools']
    assert all(not item['name'].startswith(('collaboration_goal_', 'collaboration_work_')) for item in tools)
    assert ok(hub.rpc(token, 'tools/call', {'name': 'collaboration_work_execute', 'arguments': {}}))['error']
