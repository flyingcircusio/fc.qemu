import errno

import mock
import pytest

from fc.qemu.outgoing import Outgoing


@pytest.fixture
def outgoing():
    o = Outgoing(mock.MagicMock())
    o.target = mock.MagicMock()
    return o


def test_prefer_remote_rescue(outgoing):
    outgoing.rescue()
    assert outgoing.target.rescue.called is True
    assert outgoing.agent.qemu.destroy.called is True


@pytest.fixture
def outgoing_broken_remote(outgoing):
    """outgoing mock where the remote rescuee throws an exception"""
    outgoing.target.rescue.side_effect = RuntimeError("boom")
    return outgoing


def test_request_remote_destroy_if_remote_rescue_fails(outgoing_broken_remote):
    outgoing = outgoing_broken_remote
    outgoing.rescue()
    assert outgoing.target.destroy.called is True
    assert outgoing.agent.qemu.destroy.called is False
    assert outgoing.agent.ceph.lock.called is True


def test_rescue_continue_on_name_resolution_error(
    outgoing_broken_remote, caplog
):
    """rescue shall continue to run the VM locally despite ceph lock name resolution
    errors, if our heuristics state that the images are still locked by the local host
    """
    from fc.qemu.hazmat.ceph import NameResolutionError

    o = outgoing_broken_remote
    o.agent.ceph.lock.side_effect = NameResolutionError(
        errno.ECONNABORTED, "Test name resolution error"
    )
    o.rescue()
    assert o.agent.qemu.destroy.called is False


def test_rescue_destroy_on_other_errors(outgoing_broken_remote, caplog):
    """rescue shall still abort the locally running VM on unknown other errors
    than the unlock name resolution error.
    """
    from subprocess import CalledProcessError

    o = outgoing_broken_remote
    o.agent.ceph.lock.side_effect = CalledProcessError(
        23, "rbd lock rbd.ssd/test.root"
    )
    o.rescue()
    assert o.agent.qemu.destroy.called is True


def test_rescue_destroy_on_name_resolution_error_when_locks_transferred(
    outgoing_broken_remote, caplog
):
    """rescue shall destroy the local running VM in the face of ceph lock name resolution
    errors, if the locks *might* have been transfered.
    """
    from fc.qemu.hazmat.ceph import NameResolutionError

    o = outgoing_broken_remote
    o.transfer_ceph_locks()
    o.agent.ceph.lock.side_effect = NameResolutionError(
        errno.ECONNABORTED, "Test name resolution error"
    )
    o.rescue()
    assert o.agent.qemu.destroy.called is True


def test_rescue_destroy_on_name_resolution_error_when_locks_transferred_with_exception(
    outgoing_broken_remote, caplog
):
    """
    Same as test_rescue_destroy_on_name_resolution_error_when_locks_transferred,
    but in the face of an exception when transferring the ceph lock.
    """
    from fc.qemu.hazmat.ceph import NameResolutionError

    o = outgoing_broken_remote
    name_err = NameResolutionError(
        errno.ECONNABORTED, "Test name resolution error"
    )
    o.agent.ceph.unlock.side_effect = name_err
    with pytest.raises(NameResolutionError):
        o.transfer_ceph_locks()
    o.agent.ceph.lock.side_effect = name_err
    o.rescue()
    assert o.agent.qemu.destroy.called is True
