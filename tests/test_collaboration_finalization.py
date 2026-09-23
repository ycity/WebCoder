"""Final integration regressions: budgets, truthful routing and distribution."""
from pathlib import Path

import pytest

from tests.test_collaboration_events import setup_events
from tests.test_collaboration_service import claimed, command, key
from webcoder.hub.collaboration.errors import DevError


def test_reclaim_keeps_job_call_budget_and_refuses_exhausted_budget(collab):
    service, owner, worker, dot, room, agents, clock, scope = collab
    request, lease = claimed(collab)
    service.heartbeat({**scope, 'job_id': lease['id'], 'attempt': lease['attempt'],
                       'fencing_token': lease['fencing_token'], 'idempotency_key': key()}, worker)
    assert service.store.one('SELECT tool_calls FROM collaboration_jobs')['tool_calls'] == 2
    clock[0] += 901
    service.reconcile()
    clock[0] += 31
    service.reconcile()
    current = service.object('collaboration_jobs', room, lease['id'])
    next_lease = service.claim({**request, 'expected_version': current['version'], 'idempotency_key': key()}, worker)
    assert next_lease['tool_calls'] == 3 and next_lease['attempt'] == 2
    service.store.execute('UPDATE collaboration_jobs SET tool_calls=20 WHERE id=?', (lease['id'],))
    blocked = service.block({**scope, 'job_id': lease['id'], 'attempt': next_lease['attempt'],
        'fencing_token': next_lease['fencing_token'], 'reason_code': 'missing_data',
        'summary': 'Need owner review after exhausting the original budget', 'idempotency_key': key()}, worker)
    resumed = service.control({**scope, 'target_id': lease['id'], 'action': 'retry',
        'expected_version': blocked['version'], 'reason': 'Review remaining original allowance', 'idempotency_key': key()}, owner)
    with pytest.raises(DevError) as error:
        service.claim({**request, 'expected_version': resumed['version'], 'idempotency_key': key()}, worker)
    assert error.value.code == 'BUDGET_EXCEEDED'
    assert service.object('collaboration_jobs', room, lease['id'])['attempt'] == 2


@pytest.mark.anyio
async def test_display_requires_matching_current_subscription(collab):
    service, owner, worker, dot, room, agents, clock, scope = collab
    events, receiver, args = setup_events(collab)
    filtered = {**args, 'arguments': {**args['arguments'], 'severity_min': 'critical'}}
    await events.subscribe(filtered, worker)
    created = command(collab)[1]
    job = service.object('collaboration_jobs', room, created['job_id'])
    assert service.delivery_status(job) == 'no_valid_subscription'
    await events.tick()
    assert len(receiver.requests) == 1  # challenge only, no task matching the filter
    matching = await events.subscribe(args, worker)
    assert service.delivery_status(job) == 'no_valid_subscription'  # Null cursor skips old tasks.
    await events.tick()
    assert len(receiver.requests) == 1
    created = command(collab)[1]
    job = service.object('collaboration_jobs', room, created['job_id'])
    assert service.delivery_status(job) == 'event_queued'
    await events.tick()
    assert service.delivery_status(job) == 'event_accepted'
    row = service.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (matching['id'],))
    service.control({**scope, 'target_id': row['id'], 'expected_version': row['version'],
        'action': 'subscription_pause', 'reason': 'Pause the only matching route', 'idempotency_key': key()}, owner)
    assert service.delivery_status(job) == 'no_valid_subscription'


@pytest.mark.anyio
async def test_large_requested_subscription_lifetime_is_clamped_before_conversion(collab):
    events, receiver, args = setup_events(collab)
    result = await events.subscribe({**args, 'ttlMs': 10 ** 400}, collab[2])
    row = collab[0].store.one('SELECT expires_at FROM mcp_event_subscriptions WHERE id=?', (result['id'],))
    assert row['expires_at'] == collab[6][0] + 86400


def test_public_source_profile_contains_collaboration_and_reviewed_docs():
    # The collaboration center is ported from CodePier (MIT): its notice ships with the code.
    import webcoder.hub.collaboration as package
    root = Path(package.__file__).resolve().parent
    assert 'Copyright (c) 2026 Remote Dev MCP Agent contributors' in package.__doc__
    assert 'Permission is hereby granted, free of charge' in package.__doc__
    for name in ('service.py', 'events.py', 'monitor.py', 'contracts.py', 'joining.py', 'coordination.py'):
        assert (root / name).is_file()


@pytest.mark.anyio
@pytest.mark.parametrize('status,expected', [(204, 'event_accepted'), (410, 'dead_letter')])
async def test_delivery_display_preserves_record_at_or_before_scan_cursor(collab, status, expected):
    service, _, worker, _, room, _, _, _ = collab
    events, receiver, args = setup_events(collab)
    await events.subscribe(args, worker)
    created = command(collab)[1]
    receiver.status = status
    await events.tick()
    row = service.store.one('SELECT * FROM mcp_event_deliveries')
    sub = service.store.one('SELECT scan_seq FROM mcp_event_subscriptions')
    assert row['event_seq'] <= sub['scan_seq']
    job = service.object('collaboration_jobs', room, created['job_id'])
    assert service.delivery_status(job) == expected
