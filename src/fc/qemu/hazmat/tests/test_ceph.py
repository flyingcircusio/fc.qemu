from ..ceph import Ceph, Volume
import os.path
import pytest
import rbd
import time


@pytest.yield_fixture
def ceph_inst():
    cfg = {'resource_group': 'test', 'name': 'test00', 'disk': 10}
    ceph = Ceph(cfg)
    ceph.CREATE_VM = 'echo {name}'
    ceph.MKFS_XFS = '-q -f'
    ceph.__enter__()
    try:
        yield ceph
    finally:
        ceph.__exit__(None, None, None)


@pytest.yield_fixture
def volume(ceph_inst):
    volume = Volume(ceph_inst, 'othervolume', 'label')

    try:
        volume.snapshots.purge()
    except Exception:
        pass

    try:
        rbd.RBD().remove(ceph_inst.ioctx, 'othervolume')
    except rbd.ImageNotFound:
        pass

    yield volume
    time.sleep(.1)

    lock = volume.lock_status()
    if lock is not None:
        volume.rbdimage.break_lock(*lock)
    volume.snapshots.purge()
    rbd.RBD().remove(ceph_inst.ioctx, 'othervolume')


@pytest.yield_fixture
def ceph_with_volumes(ceph_inst):
    for vol in ceph_inst.volumes:
        vol.ensure_presence()
        vol.lock()
    yield ceph_inst
    for vol in ceph_inst.volumes:
        vol.unlock(force=True)
        vol.snapshots.purge()
        rbd.RBD().remove(ceph_inst.ioctx, vol.name)


def test_volume_presence(volume):
    assert volume.fullname == 'test/othervolume'
    assert not volume.exists()
    with pytest.raises(rbd.ImageNotFound):
        volume.rbdimage
    volume.ensure_presence()
    assert volume.rbdimage
    # Check that ensure_presence is fine with being called multiple times.
    volume.ensure_presence()


def test_volume_snapshot(volume):
    volume.ensure_presence()
    volume.snapshots.create('s0')
    snaps = list(volume.snapshots)
    assert len(snaps) == 1
    snapshot = snaps[0]
    assert snapshot.name == 'othervolume'
    assert snapshot.snapname == 's0'
    assert snapshot.size == volume.size
    assert snapshot.id == volume.snapshots['s0'].id

    snapshot.remove()
    assert [] == list(volume.snapshots)


def test_purge_snapshots(volume):
    volume.ensure_presence()
    for snap in ['s0', 's1']:
        volume.snapshots.create(snap)
    assert len(volume.snapshots) == 2
    volume.snapshots.purge()
    assert len(volume.snapshots) == 0


def test_volume_size(volume):
    volume.ensure_presence()
    assert volume.rbdimage
    assert volume.size == 1024
    volume.ensure_size(2048)
    assert volume.size == 2048
    # Call ensure multiple times to help triggering caching code paths.
    volume.ensure_size(2048)
    assert volume.size == 2048


def test_volume_shared_lock_protection(volume):
    volume.ensure_presence()
    volume.rbdimage.lock_shared('host1', 'a')
    volume.rbdimage.lock_shared('remotehost', 'a')
    with pytest.raises(NotImplementedError):
        volume.lock_status()
    lockers = volume.rbdimage.list_lockers()
    for client, cookie, _ in lockers['lockers']:
        volume.rbdimage.break_lock(client, cookie)


def test_volume_locking(volume):
    # Non-existing volumes report None as locking status but do not raise
    # an exception.
    assert not volume.exists()
    assert volume.lock_status() is None
    volume.ensure_presence()
    assert volume.lock_status() is None
    volume.lock()
    assert volume.lock_status()[1] == 'host1'
    # We want to smoothen out that some other process has locked the same image
    # for the same tag already and assume that this is another incarnation of
    # us - for that we have our own lock.
    volume.lock()
    assert volume.lock_status()[1] == 'host1'
    volume.unlock()
    assert volume.lock_status() is None
    # We can call unlock twice if it isn't locked.
    volume.unlock()

    volume.rbdimage.lock_exclusive('someotherhost')
    with pytest.raises(rbd.ImageBusy):
        volume.lock()
    with pytest.raises(rbd.ImageBusy):
        # Can not unlock locks that someone else holds.
        volume.unlock()


def test_force_unlock(volume):
    volume.ensure_presence()
    volume.rbdimage.lock_exclusive('someotherhost')
    volume.unlock(force=True)
    assert volume.lock_status() is None


def test_volume_mkswap(volume):
    volume.ensure_presence()
    volume.ensure_size(5 * 1024 ** 2)
    with volume.mapped():
        volume.mkswap()


def test_volume_mkfs(volume):
    volume.ensure_presence()
    volume.ensure_size(40 * 1024 ** 2)
    with volume.mapped():
        volume.mkfs()


def test_volume_map_unmap(volume):
    volume.ensure_presence()
    volume.map()
    assert os.path.exists('/dev/rbd/test/othervolume')
    volume.map()
    assert os.path.exists('/dev/rbd/test/othervolume')
    volume.unmap()
    assert not os.path.exists('/dev/rbd/test/othervolume')
    volume.unmap()
    assert not os.path.exists('/dev/rbd/test/othervolume')


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
    ceph_with_volumes.stop()
    assert ceph_with_volumes.root.lock_status()
    assert ceph_with_volumes.swap.lock_status() is None
    assert ceph_with_volumes.tmp.lock_status() is None


def test_map_snapshot(volume):
    volume.ensure_presence()
    volume.snapshots.create('s0')
    with volume.snapshots['s0'].mapped() as device:
        assert os.path.exists(device)


def test_mount_should_fail_if_not_mapped(volume):
    volume.ensure_presence()
    with pytest.raises(RuntimeError):
        volume.mount()


def test_mount_snapshot(volume):
    volume.ensure_presence()
    volume.ensure_size(40 * 1024 ** 2)
    with volume.mapped():
        volume.mkfs(fstype='xfs')
    volume.snapshots.create('s0')
    with volume.snapshots['s0'].mounted() as mp:
        assert os.path.ismount(mp)
        with open('/proc/self/mounts') as mounts:
            assert '{} xfs ro'.format(mp) in mounts.read()
