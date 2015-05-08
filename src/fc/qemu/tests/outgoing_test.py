from ..outgoing import Outgoing
import pytest
import mock


@pytest.fixture
def outgoing():
    o = Outgoing(mock.MagicMock())
    o.target = mock.MagicMock()
    return o


def test_prefer_remote_rescue(outgoing):
    outgoing.rescue()
    assert outgoing.target.rescue.called is True
    assert outgoing.agent.qemu.destroy.called is True


def test_request_remote_destroy_if_remote_rescue_fails(outgoing):
    outgoing.target.rescue.side_effect = RuntimeError('boom')
    outgoing.rescue()
    assert outgoing.target.destroy.called is True
    assert outgoing.agent.qemu.destroy.called is False
    assert outgoing.agent.ceph.lock.called is True
