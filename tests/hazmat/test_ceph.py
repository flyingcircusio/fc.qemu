import pytest
import rbd

from tests.conftest import get_log


@pytest.fixture
def ceph_with_volumes(ceph_inst):
    for vol in ceph_inst.specs.values():
        vol.ensure_presence()
    ceph_inst.lock()
    yield ceph_inst
    for volume in ceph_inst.opened_volumes:
        volume.unlock(force=True)
        volume.snapshots.purge()
        volume.close()
        rbd.RBD().remove(volume.ioctx, volume.name)


def test_ceph_stop_should_unlock_all_volumes(ceph_with_volumes):
    for volume in ceph_with_volumes.opened_volumes:
        assert volume.lock_status()
    ceph_with_volumes.stop()
    for volume in ceph_with_volumes.opened_volumes:
        assert volume.lock_status() is None


def test_ceph_stop_remove_only_own_locks(ceph_with_volumes):
    """Test case where failed migrations leave inconsistent locking."""
    ceph_with_volumes.volumes["root"].unlock()
    ceph_with_volumes.volumes["root"].rbdimage.lock_exclusive("someotherhost")
    # It unlocks what it can.
    ceph_with_volumes.stop()
    assert ceph_with_volumes.volumes["root"].lock_status()
    assert ceph_with_volumes.volumes["swap"].lock_status() is None
    assert ceph_with_volumes.volumes["tmp"].lock_status() is None


def test_is_unlocked(ceph_with_volumes):
    assert ceph_with_volumes.is_unlocked() is False
    ceph_with_volumes.unlock()
    assert ceph_with_volumes.is_unlocked() is True


def test_multiple_images_raises_error(ceph_inst):
    rbd.RBD().create(ceph_inst.ioctxs["rbd.hdd"], "simplevm.root", 1024)
    rbd.RBD().create(ceph_inst.ioctxs["rbd.ssd"], "simplevm.root", 1024)
    root_spec = ceph_inst.specs["root"]
    assert sorted(root_spec.exists_in_pools()) == ["rbd.hdd", "rbd.ssd"]
    with pytest.raises(RuntimeError):
        root_spec.exists_in_pool()


@pytest.mark.live()
def test_rbd_pool_migration(ceph_inst, patterns) -> None:
    ceph_inst.cfg["tmp_size"] = 500 * 1024 * 1024
    ceph_inst.cfg["swap_size"] = 50 * 1024 * 1024
    ceph_inst.cfg["root_size"] = 50 * 1024 * 1024
    rbd.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.root",
        ceph_inst.cfg["root_size"],
    )
    rbd.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.tmp",
        ceph_inst.cfg["tmp_size"],
    )
    rbd.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.swap",
        ceph_inst.cfg["swap_size"],
    )
    assert ceph_inst.specs["root"].exists_in_pool() == "rbd.ssd"
    assert ceph_inst.specs["swap"].exists_in_pool() == "rbd.ssd"
    assert ceph_inst.specs["tmp"].exists_in_pool() == "rbd.ssd"

    ceph_inst.start()
    ceph_inst.status()

    first_start = patterns.first_start
    first_start.optional(
        """
waiting interval=0 machine=simplevm remaining=4 subsystem=ceph volume=rbd.hdd/simplevm.tmp
sgdisk> Setting name!
sgdisk> partNum is 0
mkfs.xfs>       mkfs.xfs: small data volume, ignoring data volume stripe unit 128 and stripe width 128
"""
    )
    first_start.in_order(
        """
connect-rados machine=simplevm subsystem=ceph

pre-start machine=simplevm subsystem=ceph volume_spec=root
ensure-presence machine=simplevm subsystem=ceph volume_spec=root
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
ensure-size machine=simplevm subsystem=ceph volume_spec=root
start machine=simplevm subsystem=ceph volume_spec=root
start-root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
root-found-in current_pool=rbd.ssd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd args=status --format json rbd.ssd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd>    {"watchers":[{"address":"...:0/...","client":...,"cookie":...}]}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root

migrate-vm-root-disk action=start machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd subsystem=ceph volume=rbd.ssd/simplevm.root
unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd args=migration prepare rbd.ssd/simplevm.root rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=simplevm.root
rbd machine=simplevm returncode=0 subsystem=ceph volume=simplevm.root

pre-start machine=simplevm subsystem=ceph volume_spec=swap
delete-outdated-swap image=simplevm.swap machine=simplevm pool=rbd.ssd subsystem=ceph volume=simplevm.swap
ensure-presence machine=simplevm subsystem=ceph volume_spec=swap
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
ensure-size machine=simplevm subsystem=ceph volume_spec=swap
start machine=simplevm subsystem=ceph volume_spec=swap
start-swap machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd args=map "rbd.hdd/simplevm.swap" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd>    /dev/rbd0
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.swap
mkswap args=-f -L "swap" /dev/rbd/rbd.hdd/simplevm.swap machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
mkswap> Setting up swapspace version 1, size = 50 MiB (52424704 bytes)
mkswap> LABEL=swap, UUID=...-...-...-...-...
mkswap machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd args=unmap "/dev/rbd/rbd.hdd/simplevm.swap" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.swap

pre-start machine=simplevm subsystem=ceph volume_spec=tmp
delete-outdated-tmp image=simplevm.tmp machine=simplevm pool=rbd.ssd subsystem=ceph volume=simplevm.tmp
ensure-presence machine=simplevm subsystem=ceph volume_spec=tmp
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
ensure-size machine=simplevm subsystem=ceph volume_spec=tmp
start machine=simplevm subsystem=ceph volume_spec=tmp
start-tmp machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd args=map "rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd>    /dev/rbd0
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
create-fs machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
sgdisk args=-o "/dev/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
sgdisk> Creating new GPT entries in memory.
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
sgdisk args=-a 8192 -n 1:8192:0 -c "1:tmp" -t 1:8300 "/dev/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
partprobe args=/dev/rbd/rbd.hdd/simplevm.tmp machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
mkfs.xfs args=-q -f -K -L "tmp" /dev/rbd/rbd.hdd/simplevm.tmp-part1 machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
mkfs.xfs machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
seed machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
mount args="/dev/rbd/rbd.hdd/simplevm.tmp-part1" "/mnt/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
guest-properties machine=simplevm properties={'binary_generation': 2} subsystem=ceph volume=rbd.hdd/simplevm.tmp
binary-generation generation=2 machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
umount args="/mnt/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd args=unmap "/dev/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp

rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[{"address":"...:0/...","client":...,"cookie":...}],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"prepared","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=prepared subsystem=ceph volume=rbd.hdd/simplevm.root
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
"""
    )

    assert get_log() == first_start

    assert ceph_inst.specs["root"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["swap"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["tmp"].exists_in_pool() == "rbd.hdd"

    ceph_inst.ensure()
    ceph_inst.status()

    first_ensure = patterns.first_ensure
    first_ensure.in_order(
        """
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[{"address":"...:0/...","client":...,"cookie":...}],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"prepared","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=prepared subsystem=ceph volume=rbd.hdd/simplevm.root

root-migration-execute machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
ceph args=rbd task add migration execute rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
ceph>   {"sequence": ..., "id": "...-...-...-...-...", "message": "Migrating image rbd.ssd/simplevm.root to rbd.ssd/simplevm.root", "refs": {"action": "migrate execute", "pool_name": "rbd.hdd", "pool_namespace": "", "image_name": "simplevm.root", "image_id": "..."}}
ceph machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[{"address":"...:0/...","client":...,"cookie":...}],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"...","state_description":...}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress=...status=... subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
"""
    )

    assert get_log() == first_ensure

    while "status=executed" not in get_log():
        ceph_inst.status()

    ceph_inst.ensure()
    ceph_inst.status()

    commit_ensure = patterns.commit_ensure
    commit_ensure.in_order(
        """
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[{"address":"...:0/...","client":...,"cookie":...}],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"executed","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=executed subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-commit machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=--no-progress migration commit rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[{"address":"...:0/...","client":...,"cookie":...}]}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
"""
    )

    assert get_log() == commit_ensure
