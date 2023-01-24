import pytest

from fc.qemu.hazmat.qemu import Qemu


def test_write_file_expects_bytes(guest_agent):
    qemu = Qemu({"name": "vm00"})
    qemu.guestagent = guest_agent
    with pytest.raises(TypeError):
        qemu.write_file("/tmp/foo", "asdf")


def test_write_file_no_error(guest_agent):
    # We do't have access to a real guest agent here
    # but we saw errors even encoding the data to the socket.
    qemu = Qemu({"name": "vm00"})
    qemu.guestagent = guest_agent
    qemu.write_file("/tmp/foo", b"asdf")
    assert guest_agent.client.messages == ["{'execute': ..."]
