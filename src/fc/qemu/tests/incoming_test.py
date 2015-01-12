from ..incoming import IncomingServer
import mock
import pytest


@pytest.fixture
def mock_agent():
    agent = mock.Mock()
    agent.migration_ctl_address = 'localhost:12345'
    agent.qemu = mock.Mock()
    agent.ceph = mock.Mock()
    agent.ceph.auth_cookie.return_value = '5f620fda'
    return agent


def test_prepare_should_stop_ceph_on_exception(mock_agent):
    mock_agent.qemu.inmigrate.side_effect = Exception('boom!')
    s = IncomingServer(mock_agent)
    with pytest.raises(Exception):
        s.prepare_incoming('args', 'config')
    assert mock_agent.ceph.stop.called is True


def test_rescue(mock_agent):
    s = IncomingServer(mock_agent)
    mock_agent.ceph.lock.side_effect = Exception('boom!')
    with pytest.raises(Exception):
        s.rescue()
    assert mock_agent.qemu.destroy.called is True
    assert mock_agent.ceph.unlock.called is True
