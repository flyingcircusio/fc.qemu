import pytest
import rbd


@pytest.yield_fixture
def ceph_with_volumes(ceph_inst):
    for vol in ceph_inst.volumes:
        vol.ensure_presence()
    ceph_inst.lock()
    yield ceph_inst
    for vol in ceph_inst.volumes:
        vol.unlock(force=True)
        vol.snapshots.purge()
        if vol._image:
            vol._image.close()
        rbd.RBD().remove(ceph_inst.ioctx, vol.name)


def test_ceph_stop_should_unlock_all_volumes(ceph_with_volumes):
    for vol in ceph_with_volumes.volumes:
        assert vol.lock_status()
    ceph_with_volumes.stop()
    for vol in ceph_with_volumes.volumes:
        assert vol.lock_status() is None


def test_ceph_stop_remove_only_own_locks(ceph_with_volumes):
    """Test case where failed migrations leave inconsistent locking."""
    ceph_with_volumes.root.unlock()
    ceph_with_volumes.root.rbdimage.lock_exclusive('someotherhost')
    # It unlocks what it can.
    ceph_with_volumes.stop()
    assert ceph_with_volumes.root.lock_status()
    assert ceph_with_volumes.swap.lock_status() is None
    assert ceph_with_volumes.tmp.lock_status() is None


def test_is_unlocked(ceph_with_volumes):
    assert ceph_with_volumes.is_unlocked() is False
    ceph_with_volumes.unlock()
    assert ceph_with_volumes.is_unlocked() is True
