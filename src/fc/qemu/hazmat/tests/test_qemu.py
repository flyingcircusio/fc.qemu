import pytest

from fc.qemu.hazmat.qemu import Qemu


def test_write_file_expects_bytes(guest_agent):
    qemu = Qemu({"name": "vm00", "id": 2345})
    qemu.guestagent = guest_agent
    with pytest.raises(TypeError):
        qemu.write_file("/tmp/foo", '"asdf"')


def test_write_file_no_error(guest_agent):
    # We do't have access to a real guest agent here
    # but we saw errors even encoding the data to the socket.
    qemu = Qemu({"name": "vm00", "id": 2345})
    # the emulated answers of the guest agent:

    guest_agent._client_stub.responses = [
        # sync ID, hard-coded in fixture
        '{"return": 87643}',
        # emulated non-empty result of executions:
        # guest-file-open
        '{"return": "file-handle-1"}',
        # guest-file-write
        '{"return": "qwer"}',
        # guest-file-close
        '{"return": "zuio"}',
    ]

    qemu.guestagent = guest_agent

    qemu.write_file("/tmp/foo", b'"asdf"')
    print(guest_agent.client.messages_sent)
    assert guest_agent.client.messages_sent == [
        b'\xff{"execute": "guest-sync", "arguments": {"id": 87643}}',
        b'{"execute": "guest-file-open", "arguments": {"path": "/tmp/foo", "mode": "w"}}',
        b'{"execute": "guest-file-write", "arguments": {"handle": "file-handle-1", "buf-b64": "ImFzZGYi\\n"}}',
        b'{"execute": "guest-file-close", "arguments": {"handle": "file-handle-1"}}',
    ]
