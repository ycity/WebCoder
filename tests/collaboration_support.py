"""Isolated collaboration fixtures (ported from CodePier's tests): a real Store and service with a controllable
clock, an owner with two projects, three read-only connections and one broad one. Never real host subscriptions."""
import json
import time
import uuid
from dataclasses import replace

import pytest

from webcoder.hub.collaboration.config import CollaborationConfig
from webcoder.hub.collaboration.events import EventService
from webcoder.hub.collaboration.host import HostRuntime, Principal
from webcoder.hub.collaboration.monitor import MonitorService
from webcoder.hub.collaboration.service import CollaborationService
from webcoder.hub.config import Settings
from webcoder.hub.runtime import Runtime


def key():
    return uuid.uuid4().hex


def seed_user(store, user_id, username):
    store.execute("INSERT INTO users(id, username, password_hash, created, role) VALUES (?, ?, 'x', ?, 'user')",
                  (user_id, username, time.time()))


def seed_grant(store, grant_id, user_id, scopes=('read',), projects=('proj',), kind='pat'):
    store.execute("INSERT INTO grants(id, kind, label, scopes, projects, created, user_id) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)", (grant_id, kind, grant_id, json.dumps(list(scopes)),
                                                    json.dumps(list(projects)), time.time(), user_id))


def seed(store):
    seed_user(store, 'owner', 'owner')
    seed_user(store, 'other', 'other')
    info = {'commands': {'programs': [{'name': 'echo'}]}}  # programs allowed: a project may run tasks
    store.execute("INSERT INTO devices(id, name, secret_enc, created, info, user_id) "
                  "VALUES ('dev', 'fixture', ?, ?, ?, 'owner')",
                  (store.encrypt('synthetic-test-device'), time.time(), json.dumps(info)))
    for pid in ('proj', 'otherproj'):
        store.execute("INSERT INTO projects(id, alias, alias_key, device_id, root, mode, created, user_id) "
                      "VALUES (?, ?, ?, 'dev', '/tmp/fixture', 'rw', ?, 'owner')", (pid, pid, pid, time.time()))
    for grant in ('worker', 'dot', 'second'):
        seed_grant(store, grant, 'owner')
    seed_grant(store, 'broad', 'owner', scopes=('read', 'write', 'cli'))


def make_service(runtime, clock, **flags):
    config = CollaborationConfig(enabled=True, **flags)
    service = CollaborationService(HostRuntime(runtime), config, clock=lambda: clock[0])
    service.events = EventService(service)
    service.monitor = MonitorService(service)
    service.health = {}
    return service


@pytest.fixture
def collab(tmp_path):
    runtime = Runtime(Settings(data_dir=tmp_path / 'hub', collaboration_enabled=True))
    seed(runtime.store)
    clock = [time.time()]
    service = make_service(runtime, clock)
    owner = Principal('面板:owner', 'owner', admin=True)
    worker = Principal('mcp:worker:fixture', 'owner', {'read'}, ['proj'], grant_id='worker')
    dot = replace(worker, grant_id='dot', actor='mcp:dot:fixture')
    scope = {'project': 'proj', 'environment_id': 'production'}
    room = service.room_create({**scope, 'idempotency_key': key()}, owner)['room']
    agents = [service.register_agent({**scope, 'label': 'same name', 'kind': kind,
              'grant_id': grant, 'idempotency_key': key()}, owner)
              for kind, grant in [('work_cloud', 'worker'), ('dot', 'dot')]]
    yield service, owner, worker, dot, room, agents, clock, scope
    runtime.close()
