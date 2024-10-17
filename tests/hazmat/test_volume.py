import os.path
import socket
import subprocess
import time

import pytest
import rbd


@pytest.fixture
def tmp_spec(ceph_inst):
    for volume in ceph_inst.opened_volumes:
        volume.snapshots.purge()
        name, ioctx = volume.name, volume.ioctx
        volume.close()
        rbd.RBD().remove(ioctx, name)

    spec = ceph_inst.specs["tmp"]

    yield spec
    time.sleep(0.2)

    try:
        for volume in ceph_inst.opened_volumes:
            lock = volume.lock_status()
            if lock is not None:
                volume.rbdimage.break_lock(*lock)
            volume.snapshots.purge()
            name, ioctx = volume.name, volume.ioctx
            volume.close()
            rbd.RBD().remove(ceph_inst.ioctxs["rbd.hdd"], "simplevm.root")
    except rbd.ImageNotFound:
        pass


def test_volume_presence(ceph_inst, tmp_spec):
    assert not tmp_spec.volume
    assert not ceph_inst.volumes["tmp"]
    assert not tmp_spec.exists_in_pool()
    assert tmp_spec.exists_in_pools() == []
    tmp_spec.ensure_presence()
    assert tmp_spec.exists_in_pool() == "rbd.hdd"
    assert tmp_spec.exists_in_pools() == ["rbd.hdd"]
    assert tmp_spec.volume.rbdimage
    assert tmp_spec.volume.rbdimage is ceph_inst.volumes["tmp"].rbdimage
    # Check that ensure_presence is fine with being called multiple times.
    assert tmp_spec.volume.fullname == "rbd.hdd/simplevm.tmp"
    tmp_spec.ensure_presence()
    assert tmp_spec.exists_in_pool() == "rbd.hdd"
    assert tmp_spec.exists_in_pools() == ["rbd.hdd"]


def test_volume_snapshot(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume

    volume.snapshots.create("s0")
    snaps = list(volume.snapshots)
    assert len(snaps) == 1
    snapshot = snaps[0]
    assert snapshot.name == "simplevm.tmp"
    assert snapshot.snapname == "s0"
    assert snapshot.size == volume.size
    assert snapshot.id == volume.snapshots["s0"].id

    snapshot.remove()
    assert [] == list(volume.snapshots)


def test_purge_snapshots(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    for snap in ["s0", "s1"]:
        volume.snapshots.create(snap)
    assert len(volume.snapshots) == 2
    volume.snapshots.purge()
    assert len(volume.snapshots) == 0


def test_snapshot_not_found(tmp_spec):
    tmp_spec.ensure_presence()
    with pytest.raises(KeyError):
        tmp_spec.volume.snapshots["no-such-key"]


def test_volume_size(tmp_spec):
    tmp_spec.desired_size = 1024
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    assert volume.rbdimage
    assert volume.size == 1024
    volume.ensure_size(2048)
    assert volume.size == 2048
    # Call ensure multiple times to help triggering caching code paths.
    volume.ensure_size(2048)
    assert volume.size == 2048


def test_volume_shared_lock_protection(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    volume.rbdimage.lock_shared("host1", "a")
    volume.rbdimage.lock_shared("remotehost", "a")
    with pytest.raises(NotImplementedError):
        volume.lock_status()
    lockers = volume.rbdimage.list_lockers()
    for client, cookie, _ in lockers["lockers"]:
        volume.rbdimage.break_lock(client, cookie)


def test_volume_locking(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    assert volume.lock_status() is None
    volume.lock()
    assert volume.lock_status()[1] == socket.gethostname()
    # We want to smoothen out that some other process has locked the same image
    # for the same tag already and assume that this is another incarnation of
    # us - for that we have our own lock.
    volume.lock()
    assert volume.lock_status()[1] == socket.gethostname()
    volume.unlock()
    assert volume.lock_status() is None
    # We can call unlock twice if it isn't locked.
    volume.unlock()

    volume.rbdimage.lock_exclusive("someotherhost")
    with pytest.raises(rbd.ImageBusy):
        volume.lock()

    # Can not unlock locks that someone else holds.
    volume.unlock()
    assert volume.lock_status()[1] == "someotherhost"


def test_force_unlock(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    volume = tmp_spec.volume
    volume.rbdimage.lock_exclusive("someotherhost")
    volume.unlock(force=True)
    assert volume.lock_status() is None


# increase timeout from the default of 3s
@pytest.mark.timeout(10)
def test_volume_mkswap(ceph_inst):
    swap = ceph_inst.specs["swap"]
    swap.ensure_presence()
    swap.start()
    with swap.volume.mapped():
        output = subprocess.check_output(["file", swap.volume.device])
        output = output.decode("ascii")
        assert "Linux swap file" in output


@pytest.mark.timeout(60)
def test_volume_tmp_mkfs(tmp_spec):
    tmp_spec.desired_size = 100 * 1024 * 1024
    tmp_spec.ensure_presence()
    with tmp_spec.volume.mapped():
        tmp_spec.mkfs()


def test_unmapped_volume_should_have_no_part1(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    # not something like 'None-part1'
    assert volume.part1dev is None


def test_volume_map_unmap_is_idempotent(tmp_spec):
    # This is more of an internal sanity test within our mocking infrastructure.
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    volume.map()
    device = volume.device
    assert os.path.exists(device)
    volume.map()
    assert os.path.exists(device)
    volume.unmap()
    assert not os.path.exists(device)
    volume.unmap()
    assert not os.path.exists(device)


def test_map_snapshot(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    volume.snapshots.create("s0")
    with volume.snapshots["s0"].mapped() as device:
        assert os.path.exists(device)


def test_mount_should_fail_if_not_mapped(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    with pytest.raises(RuntimeError):
        volume.mount()


@pytest.mark.timeout(60)
@pytest.mark.live()
def test_mount_snapshot(tmp_spec):
    tmp_spec.desired_size = 100 * 1024 * 1024
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    with volume.mapped():
        tmp_spec.mkfs()
    volume.snapshots.create("s0")
    snap = volume.snapshots["s0"]
    with snap.mounted() as mountpoint:
        assert mountpoint.is_mount(), "not a mountpoint"
        with open("/proc/self/mounts") as mounts:
            assert f"{mountpoint} xfs ro" in mounts.read()
        # test for idempotence
        snap.mount()
        assert mountpoint.is_mount()
    assert not mountpoint.is_mount()
    # test for idempotence
    snap.umount()
    assert not mountpoint.is_mount()
