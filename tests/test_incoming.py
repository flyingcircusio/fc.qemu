import mock
import pytest

from fc.qemu.exc import MigrationError
from fc.qemu.incoming import IncomingAPI, IncomingServer


@pytest.fixture
def mock_agent():
    agent = mock.Mock()
    agent.migration_ctl_address = "localhost:12345"
    agent.qemu = mock.Mock()
    agent.ceph = mock.Mock()
    agent.ceph.auth_cookie.return_value = "5f620fda"
    return agent


def test_prepare_should_stop_ceph_on_exception(mock_agent):
    mock_agent.qemu.inmigrate.side_effect = Exception("boom!")
    s = IncomingServer(mock_agent)
    with pytest.raises(Exception):
        s.prepare_incoming("args", "config")
    assert mock_agent.ceph.stop.called is True


def test_rescue(mock_agent):
    s = IncomingServer(mock_agent)
    mock_agent.ceph.lock.side_effect = Exception("boom!")
    with pytest.raises(Exception):
        s.rescue()
    assert mock_agent.qemu.destroy.called is True
    assert mock_agent.ceph.unlock.called is True


@mock.patch("fc.qemu.incoming.IncomingServer")
def test_authentication_match(server):
    api = IncomingAPI(server)
    api.cookie = "cookie1"
    # should not raise an exception
    assert api.ping("cookie1") is None


@mock.patch("fc.qemu.incoming.IncomingServer")
def test_authentication_mismatch(server):
    api = IncomingAPI(server)
    api.cookie = "cookie1"
    with pytest.raises(MigrationError):
        assert api.ping("cookie-does-not-match") is None


def test_screen_config_disable_iommu(mock_agent):
    s = IncomingServer(mock_agent)
    assert (
        s.screen_config(
            """\
[machine]
  type = "pc-i440fx-2.5"
  iommu = "off"
  accel = "kvm"

"""
        )
        == """\
[machine]
  type = "pc-i440fx-2.5"

  accel = "kvm"

"""
    )


def test_screen_config_update_qmp_monitor_syntax(mock_agent):
    s = IncomingServer(mock_agent)
    assert (
        s.screen_config(
            """\

# QMP monitor support via Unix socket

[mon "qmp_monitor"]
  mode = "control"
  chardev = "ch_qmp_monitor"
  default = "on"

[chardev "ch_qmp_monitor"]
  backend = "socket"
  path = "/run/qemu.{name}.qmp.sock"
  server = "on"
  wait = "off"

"""
        )
        == """\

# QMP monitor support via Unix socket

[mon "qmp_monitor"]
  mode = "control"
  chardev = "ch_qmp_monitor"
  pretty = "off"

[chardev "ch_qmp_monitor"]
  backend = "socket"
  path = "/run/qemu.{name}.qmp.sock"
  server = "on"
  wait = "off"

"""
    )
