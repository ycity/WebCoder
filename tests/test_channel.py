import os

import pytest

from webcoder.common.channel import ChannelError, SecureChannel, agent_proof, hub_proof, new_nonce


def pair(secret=None):
    secret = secret or os.urandom(32)
    hub_nonce, agent_nonce = new_nonce(), new_nonce()
    return (SecureChannel(secret, "dev_1", hub_nonce, agent_nonce, "hub"),
            SecureChannel(secret, "dev_1", hub_nonce, agent_nonce, "agent"))


def test_round_trip_both_directions():
    hub, agent = pair()
    for n in range(3):
        message = {"t": "req", "n": n, "text": "文件内容"}
        assert agent.open(hub.seal(message)) == message
        assert hub.open(agent.seal({"t": "res", "n": n})) == {"t": "res", "n": n}


def test_tampered_frame_is_rejected():
    hub, agent = pair()
    frame = bytearray(hub.seal({"t": "req"}))
    frame[-1] ^= 1
    with pytest.raises(ChannelError):
        agent.open(bytes(frame))


def test_replay_and_reordering_are_rejected():
    hub, agent = pair()
    first, second = hub.seal({"n": 1}), hub.seal({"n": 2})
    with pytest.raises(ChannelError):
        agent.open(second)  # out of order
    agent.open(first)
    with pytest.raises(ChannelError):
        agent.open(first)  # replay
    assert agent.open(second) == {"n": 2}


def test_directions_use_different_keys():
    hub, agent = pair()
    with pytest.raises(ChannelError):
        hub.open(hub.seal({"reflected": True}))  # a hub frame bounced back must not verify


def test_wrong_secret_cannot_open():
    hub, _ = pair(os.urandom(32))
    _, stranger = pair(os.urandom(32))
    with pytest.raises(ChannelError):
        stranger.open(hub.seal({"t": "req"}))


def test_proofs_are_role_bound():
    secret, a, b = os.urandom(32), new_nonce(), new_nonce()
    assert agent_proof(secret, "dev", a, b) != hub_proof(secret, "dev", a, b)
    assert agent_proof(secret, "dev", a, b) != agent_proof(os.urandom(32), "dev", a, b)
