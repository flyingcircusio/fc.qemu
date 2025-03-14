import tempfile
import typing

import mock
import pytest

from fc.qemu.hazmat.guestagent import GuestAgent


@pytest.fixture
def guest_agent(monkeypatch, tmpdir):
    guest_agent = GuestAgent("testvm", 0.1)

    class ClientStub(object):
        timeout: int = 0
        messages_sent: typing.List[bytes]
        responses: typing.List[str]

        receive_buffer = ""

        def __init__(self):
            self.messages_sent = []
            self.responses = []

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            pass

        def close(self):
            pass

        def send(self, msg: bytes):
            self.messages_sent.append(msg)

        def recv(self, buffersize):
            return self.receive_buffer

        def makefile(self):
            pseudo_socket_filename = tempfile.mktemp(dir=tmpdir)
            with open(pseudo_socket_filename, "w") as f:
                f.write("\n".join(self.responses))
            return open(pseudo_socket_filename)

    client_stub = ClientStub()

    guest_agent.client_factory = lambda family, type: client_stub
    guest_agent._client_stub = client_stub

    # Ensure guest agent sync ids are stable.
    randint = mock.Mock(return_value=87643)
    monkeypatch.setattr("random.randint", randint)

    return guest_agent
