import os.path
import subprocess
import time
from unittest.mock import Mock

import pytest

from fc.qemu.hazmat import libceph
from fc.qemu.timeout import TimeOut
from tests.conftest import get_log


@pytest.fixture
def tmp_spec(ceph_inst):
    for volume in ceph_inst.opened_volumes:
        volume.snapshots.purge()
        name, ioctx = volume.name, volume.ioctx
        volume.close()
        timeout = TimeOut(10, interval=1)
        while timeout.tick():
            try:
                libceph.RBD().remove(ioctx, name)
            except libceph.ImageBusy:
                pass

    spec = ceph_inst.specs["tmp"]

    yield spec
    time.sleep(0.2)

    try:
        for volume in ceph_inst.opened_volumes:
            volume.unlock(force=True)
            volume.snapshots.purge()
            name, ioctx = volume.name, volume.ioctx
            volume.close()
            libceph.RBD().remove(ceph_inst.ioctxs["rbd.hdd"], name)
    except libceph.ImageNotFound:
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
    # Trying to reduce the size doesn't make a change
    volume.ensure_size(1024)
    assert volume.size == 2048


# --- the advisory rbd lock, as cidata still uses it -----------------------
#
# `tmp_spec`'s image is layering-only, which is how every image is born, so
# these drive the rbd lock path directly. In production only cidata stays in
# this state - see `CloudInitSpec.expected_features`.


def test_volume_locking(ceph_inst, tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    assert volume.lock_status() is None
    volume.lock()
    assert volume.lock_status().host == ceph_inst.CEPH_LOCK_HOST
    # We want to smoothen out that some other process has locked the same image
    # for the same tag already and assume that this is another incarnation of
    # us - for that we have our own lock.
    volume.lock()
    assert volume.lock_status().host == ceph_inst.CEPH_LOCK_HOST
    volume.unlock()
    assert volume.lock_status() is None
    # We can call unlock twice if it isn't locked.
    volume.unlock()

    volume.rbdimage.lock_exclusive("someotherhost")
    with pytest.raises(libceph.ImageBusy):
        volume.lock()

    # Can not unlock locks that someone else holds.
    volume.unlock()
    assert volume.lock_status().host == "someotherhost"


def test_locking_an_image_we_already_hold_does_nothing(
    ceph_inst, tmp_spec, monkeypatch
):
    """Ownership comes from the cookie, not from the ceph client.
    We do not rely on the `locker` field, as that changes at each process call,
    but store the host as cookie. That being equal is enough for not re-acquiring
    the lock again.
    """
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    volume.rbdimage.lock_exclusive(ceph_inst.CEPH_LOCK_HOST)
    lock = volume.lock_status()
    assert lock.host == ceph_inst.CEPH_LOCK_HOST
    assert lock.locker.startswith("client."), "the locker is a ceph client"
    relock = Mock()
    monkeypatch.setattr(volume.rbdimage, "lock_exclusive", relock)
    get_log()

    volume.lock()

    assert not relock.called, "took the lock again instead of keeping it"
    assert get_log() == (
        "lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp"
    )
    assert volume.lock_status().host == ceph_inst.CEPH_LOCK_HOST


def test_force_unlock(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    volume.rbdimage.lock_exclusive("someotherhost")
    volume.unlock(force=True)
    assert volume.lock_status() is None


def test_locking_is_a_noop_with_exclusive_lock(tmp_spec):
    """The one branch the whole lock split rests on.

    Once an image carries `exclusive-lock`, qemu arbitrates access to it and
    the per-VM rados lock expresses the assignment, so `Volume` must keep its
    hands off the image entirely - including staying silent, because an
    `unlock` line here would mean we had placed a lock we cannot remove again.
    """
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    volume.rbdimage.ensure_feature("exclusive-lock")
    get_log()

    volume.lock()
    volume.unlock()
    volume.unlock(force=True)

    assert volume.lock_status() is None
    assert volume.locked_by_me is False
    assert get_log() == ""


# --- end of the rbd lock section ------------------------------------------


def test_volume_mkswap(ceph_inst):
    swap = ceph_inst.specs["swap"]
    swap.ensure_presence()
    swap.start()
    with swap.volume.mapped():
        output = subprocess.check_output(["file", "-sL", swap.volume.device])
        output = output.decode("ascii")
        assert "Linux swap file" in output


def test_volume_tmp_mkfs(tmp_spec):
    tmp_spec.desired_size = 400 * 1024 * 1024
    tmp_spec.ensure_presence()
    with tmp_spec.volume.mapped():
        tmp_spec.mkfs()


def test_unmapped_volume_should_have_no_part1(tmp_spec):
    tmp_spec.ensure_presence()
    volume = tmp_spec.volume
    # not something like 'None-part1'
    assert volume.part1dev is None


@pytest.mark.live
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
    # Need to run live because loopback devices do not go away.
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


def test_mount_snapshot(tmp_spec):
    tmp_spec.desired_size = 500 * 1024 * 1024
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
