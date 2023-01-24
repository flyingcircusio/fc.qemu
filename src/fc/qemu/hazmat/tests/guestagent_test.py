import io

import pytest

from ..guestagent import ClientError


def test_ga_read(guest_agent):
    guest_agent.file = io.StringIO('{"return": 17035}\n')
    assert 17035 == guest_agent.read()


def test_ga_read_error(guest_agent):
    guest_agent.file = io.StringIO('{"return": 0, "error": "test failure"}\n')
    with pytest.raises(ClientError):
        guest_agent.read()


def test_ga_sync_immediate(guest_agent):
    guest_agent._client_stub.responses = [
        '{"return": 87643}',
    ]

    with guest_agent:
        # This causes an implicit sync and wires up the client stub.
        assert guest_agent.file.fileno()
        assert guest_agent.client is not None

    assert guest_agent.client.messages_sent == [
        b'\xff{"execute": "guest-sync", "arguments": {"id": 87643}}'
    ]


def test_ga_sync_retry(guest_agent):
    guest_agent._client_stub.responses = [
        '{"return": 2}',
        '{"return": 87643}',
    ]

    with guest_agent:
        # This causes an implicit sync and wires up the client stub.
        assert True

    assert guest_agent.client.messages_sent == [
        b'\xff{"execute": "guest-sync", "arguments": {"id": 87643}}'
    ]


def test_ga_sync_too_often(guest_agent):
    guest_agent._client_stub.responses = [
        f'{{"return": {x}}}' for x in range(20)
    ]

    with pytest.raises(ClientError):
        with guest_agent:
            pass

    assert guest_agent.client.messages_sent == [
        b'\xff{"execute": "guest-sync", "arguments": {"id": 87643}}'
    ]
