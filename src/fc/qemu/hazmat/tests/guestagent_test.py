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
        "{}",
        '{"return": 87643}',
    ]

    guest_agent.connect()

    # This causes an implicit sync and wires up the client stub.
    assert guest_agent.file.fileno()
    assert guest_agent.client is not None

    assert guest_agent.client.messages_sent == [
        b'{"execute": "guest-fsfreeze-thaw"}',
        b"\xff",
        b'{"execute": "guest-ping", "arguments": {}}',
        b'{"execute": "guest-sync", "arguments": {"id": 87643}}',
    ]


def test_ga_sync_wrong_response(guest_agent):
    guest_agent._client_stub.responses = [
        "{}",
        '{"return": 1}',
    ]

    with pytest.raises(ClientError):
        guest_agent.connect()

    assert guest_agent.client.messages_sent == [
        b'{"execute": "guest-fsfreeze-thaw"}',
        b"\xff",
        b'{"execute": "guest-ping", "arguments": {}}',
        b'{"execute": "guest-sync", "arguments": {"id": 87643}}',
    ]
