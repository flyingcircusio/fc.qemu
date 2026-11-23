import subprocess
from unittest.mock import Mock, patch

import pytest
import yaml

from fc.qemu.hazmat import libceph
from fc.qemu.hazmat.libceph import Image as RealImage
from tests.conftest import (
    EXCLUSIVE_LOCK_VOLUMES,
    RBD_LOCK_VOLUMES,
    get_log,
)
from tests.ellipsis import Ellipsis

#: `Ceph.start()` takes the per-VM rados lock and, because cidata stays
#: layering-only, one advisory rbd lock. root/swap/tmp carry `exclusive-lock`
#: and are covered by the rados lock alone.
MIGRATION_LOCKS = """\
rados-lock machine=simplevm subsystem=ceph
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
"""

#: `ensure_features` runs once every volume is present. Root is still in the
#: source pool at that point, the others have been recreated in the
#: destination pool by `pre_start`. `object-map` pulls `fast-diff` in with it,
#: so there are two lines per volume and none at all for cidata. The `rbd
#: feature enable` calls themselves log under `subsystem=libceph`, which
#: `reset_structlog` hides.
MIGRATION_ENABLE_FEATURES = """\
enable-rbd-feature feature=exclusive-lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
enable-rbd-feature feature=object-map machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
enable-rbd-feature feature=exclusive-lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
enable-rbd-feature feature=object-map machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
enable-rbd-feature feature=exclusive-lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
enable-rbd-feature feature=object-map machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
"""

#: `Ceph.start()` pre-starts, ensures presence and settles the features for
#: *every* volume before it locks, sizes and starts any of them. Checked as its
#: own `in_order` op, so the per-volume work is not additionally pinned to how
#: these phases interleave with it.
MIGRATION_PHASE_PRESENCE = """\
pre-start machine=simplevm subsystem=ceph volume_spec=root
ensure-presence machine=simplevm subsystem=ceph volume_spec=root
pre-start machine=simplevm subsystem=ceph volume_spec=swap
delete-outdated-swap image=simplevm.swap machine=simplevm pool=rbd.ssd subsystem=ceph volume=simplevm.swap
ensure-presence machine=simplevm subsystem=ceph volume_spec=swap
pre-start machine=simplevm subsystem=ceph volume_spec=tmp
delete-outdated-tmp image=simplevm.tmp machine=simplevm pool=rbd.ssd subsystem=ceph volume=simplevm.tmp
ensure-presence machine=simplevm subsystem=ceph volume_spec=tmp
pre-start machine=simplevm subsystem=ceph volume_spec=cidata
delete-outdated-cloud-init image=simplevm.cidata machine=simplevm pool=rbd.ssd subsystem=ceph volume=simplevm.cidata
ensure-presence machine=simplevm subsystem=ceph volume_spec=cidata
"""


@pytest.fixture
def ceph_with_volumes(ceph_inst):
    """All four volumes present, features settled, locks held.

    Mirrors what `Ceph.start()` does before it sizes and starts the volumes,
    minus the filesystem work.
    """
    for spec in ceph_inst.specs.values():
        spec.ensure_presence()
        spec.ensure_features(online=False)
    ceph_inst.lock()
    yield ceph_inst
    ceph_inst.force_unlock()
    for volume in ceph_inst.opened_volumes:
        volume.snapshots.purge()
        volume.close()
        libceph.RBD().remove(volume.ioctx, volume.name)


@pytest.fixture
def ceph_with_volumes_ci(ceph_inst_cloudinit_enc):
    ceph_inst = ceph_inst_cloudinit_enc
    for spec in ceph_inst.specs.values():
        spec.ensure_presence()
        spec.ensure_features(online=False)
    ceph_inst.lock()
    yield ceph_inst
    ceph_inst.force_unlock()
    for volume in ceph_inst.opened_volumes:
        volume.snapshots.purge()
        volume.close()
        libceph.RBD().remove(volume.ioctx, volume.name)


########################################################
# which lock ends up on which volume


def test_locks_are_split_between_cidata_and_the_rest(ceph_with_volumes):
    """The invariant the whole PL-134255 rollout rests on.

    root/swap/tmp get `exclusive-lock` - so qemu arbitrates access to them and
    the per-VM rados lock expresses the assignment - while cidata deliberately
    stays layering-only and keeps a plain advisory rbd lock. That lock is what
    a host running a pre-PL-134255 fc.qemu looks at, and it is the only thing
    stopping such a host from taking a VM that lives here.
    """
    ceph = ceph_with_volumes

    assert ceph.vm_lock.locker().host == ceph.CEPH_LOCK_HOST

    for suffix in EXCLUSIVE_LOCK_VOLUMES:
        volume = ceph.volumes[suffix]
        assert volume.features >= {"exclusive-lock", "object-map", "fast-diff"}
        assert volume.lock_status() is None, (
            f"{volume.fullname} must not carry an rbd lock"
        )

    for suffix in RBD_LOCK_VOLUMES:
        volume = ceph.volumes[suffix]
        assert "exclusive-lock" not in volume.features
        assert volume.lock_status().host == ceph.CEPH_LOCK_HOST


def upgrade_features(ceph, *, online=True):
    """The feature half of `Ceph.ensure()`, for every volume.

    Driven per spec rather than through `Ceph.ensure()`, which also chases the
    root pool migration and would shell out to a real `rbd status`.
    """
    for spec in ceph.specs.values():
        spec.ensure_features(online=online)


def test_online_upgrade_releases_the_legacy_rbd_locks(legacy_images):
    """Upgrading fc.qemu moves a *running* VM over, via `Ceph.ensure()`.

    The images arrive layering-only and rbd-locked, which is all a
    pre-PL-134255 host leaves behind. Enabling `exclusive-lock` makes
    `Volume.unlock()` a no-op, so unless the lock is dropped first it would
    stay on the image forever - and `Ceph.is_unlocked()` consults the volume
    locks, so it would never report an unlocked VM again.
    """
    ceph = legacy_images
    # Taking the rados lock first is what `Agent.ensure_online_local` does,
    # so that the volumes are never left without any advisory lock.
    ceph.lock()

    upgrade_features(ceph)

    for suffix in EXCLUSIVE_LOCK_VOLUMES:
        volume = ceph.volumes[suffix]
        assert volume.features >= {"exclusive-lock", "object-map", "fast-diff"}
        assert volume.lock_status() is None, (
            f"{volume.fullname} kept its pre-upgrade rbd lock"
        )
    # cidata is the fence for old hosts, so it keeps the rbd lock - but it is
    # ours now, not the old host's.
    cidata = ceph.volumes["cidata"]
    assert "exclusive-lock" not in cidata.features
    assert cidata.lock_status().host == ceph.CEPH_LOCK_HOST

    assert ceph.locked_by_me() is True


def test_online_upgrade_announces_what_it_changes(legacy_images):
    """Enabling features on a running VM is worth seeing in its log.

    The `unlock` has to come first: that is the pre-upgrade lock going away,
    and the ordering is what makes it possible at all. `object-map` pulls
    `fast-diff` in with it, so there are two lines per volume rather than
    three, and none for cidata.
    """
    ceph = legacy_images
    ceph.lock()
    get_log()

    upgrade_features(ceph)

    assert (
        get_log()
        == """\
enable-rbd-feature feature=exclusive-lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
unlock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
enable-rbd-feature feature=object-map machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
enable-rbd-feature feature=exclusive-lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
unlock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
enable-rbd-feature feature=object-map machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
enable-rbd-feature feature=exclusive-lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
unlock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
enable-rbd-feature feature=object-map machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp"""
    )


def test_online_upgrade_is_idempotent(legacy_images):
    """The second pass has nothing left to do and stays silent."""
    ceph = legacy_images
    ceph.lock()
    upgrade_features(ceph)
    get_log()

    upgrade_features(ceph)

    assert get_log() == ""


# --- the per-volume rbd lock, as it survives on cidata --------------------


def test_ceph_stop_releases_the_cidata_lock(ceph_with_volumes):
    ceph = ceph_with_volumes
    assert ceph.volumes["cidata"].lock_status()

    ceph.stop()

    for volume in ceph.opened_volumes:
        assert volume.lock_status() is None
    assert ceph.vm_lock.locker() is None


def test_ceph_stop_removes_only_own_locks(ceph_with_volumes):
    """Test case where failed migrations leave inconsistent locking."""
    ceph = ceph_with_volumes
    cidata = ceph.volumes["cidata"]
    cidata.unlock()
    cidata.rbdimage.lock_exclusive("someotherhost")

    # It unlocks what it can.
    ceph.stop()

    assert cidata.lock_status().host == "someotherhost"
    assert ceph.vm_lock.locker() is None


def test_ceph_stop_should_release_the_rados_lock(ceph_with_volumes):
    ceph = ceph_with_volumes
    assert ceph.vm_lock.locker().host == ceph.CEPH_LOCK_HOST

    ceph.stop()

    assert ceph.vm_lock.locker() is None


def test_ceph_stop_leaves_a_foreign_rados_lock_alone(ceph_with_volumes):
    """A failed migration can leave the lock with the other host."""
    ceph = ceph_with_volumes
    ceph.vm_lock.unlock()
    ceph.rados.lock_exclusive(
        ceph.orchestration_pool,
        "simplevm",
        ceph.vm_lock.LOCK_NAME,
        "someotherhost",
        namespace=ceph.vm_lock.NAMESPACE,
    )

    ceph.stop()
    # Not ours, so we keep our hands off it.
    assert ceph.vm_lock.locker().host == "someotherhost"
    assert ceph.locked_by() == "someotherhost"
    assert ceph.locked_by_me() is False

    # ... but force_unlock breaks it.
    ceph.force_unlock()
    assert ceph.vm_lock.locker() is None


@pytest.mark.live
def test_ceph_exclusive_lock_can_be_taken_twice_with_same_cookie(ceph_inst):
    """Test case where failed migrations leave inconsistent locking."""
    ceph = ceph_inst
    pool = ceph.ioctxs["rbd.ssd"]
    libceph.RBD().create(pool, "test", 1024)
    img = libceph.Image(pool, "test")
    img.lock_exclusive("cookie-1")
    img.lock_exclusive("cookie-1")
    with pytest.raises(libceph.ImageBusy):
        img.lock_exclusive("cookie-2")


def test_is_unlocked(ceph_with_volumes):
    assert ceph_with_volumes.is_unlocked() is False
    ceph_with_volumes.unlock()
    assert ceph_with_volumes.is_unlocked() is True


def test_is_unlocked_sees_a_leftover_volume_lock(ceph_with_volumes):
    """Both mechanisms count: the rados lock *and* any volume lock."""
    ceph = ceph_with_volumes
    ceph.unlock()
    assert ceph.is_unlocked() is True

    ceph.volumes["cidata"].rbdimage.lock_exclusive("someotherhost")

    assert ceph.is_unlocked() is False


def test_lock_roundtrip_reports_ownership(ceph_with_volumes):
    ceph = ceph_with_volumes
    assert ceph.locked_by_me() is True
    assert ceph.locked_by() == ceph.CEPH_LOCK_HOST
    assert ceph.is_unlocked() is False

    ceph.unlock()

    assert ceph.locked_by_me() is False
    assert ceph.locked_by() is None
    assert ceph.is_unlocked() is True


def test_lock_is_idempotent(ceph_with_volumes):
    """Locking again while we hold the lock must not raise."""
    ceph = ceph_with_volumes
    ceph.lock()
    ceph.lock()
    assert ceph.locked_by_me() is True


def test_multiple_images_raises_error(ceph_inst):
    libceph.RBD().create(ceph_inst.ioctxs["rbd.hdd"], "simplevm.root", 1024)
    libceph.RBD().create(ceph_inst.ioctxs["rbd.ssd"], "simplevm.root", 1024)
    root_spec = ceph_inst.specs["root"]
    assert sorted(root_spec.exists_in_pools()) == ["rbd.hdd", "rbd.ssd"]
    with pytest.raises(RuntimeError):
        root_spec.exists_in_pool()


########################################################
# the migration auth cookie


def test_auth_cookie_changes_with_lock_state(ceph_with_volumes):
    locked = ceph_with_volumes.auth_cookie()
    ceph_with_volumes.unlock()
    assert ceph_with_volumes.auth_cookie() != locked


def test_auth_cookie_distinguishes_lock_holders(ceph_with_volumes):
    """The cookie has to identify *who* holds the lock.

    It authenticates migration requests, so a cookie that is the same for
    every locked VM on every host would authenticate nothing.
    """
    ceph = ceph_with_volumes
    ours = ceph.auth_cookie()

    ceph.vm_lock.unlock()
    foreign_lock(ceph)

    assert ceph.auth_cookie() != ours


def test_auth_cookie_falls_back_to_rbd_locks(legacy_images):
    """Inmigrating from a host that only knows rbd locks.

    Without a rados lock the cookie is derived from the root/swap/tmp rbd
    locks, in that fixed order - which is what lets an old fc.qemu and a new
    one agree on a cookie for the same VM. Deprecated, but load-bearing for
    the rollout.
    """
    ceph = legacy_images
    assert ceph.vm_lock.locker() is None

    legacy = ceph.auth_cookie()

    # Stable as long as the locks are.
    assert ceph.auth_cookie() == legacy
    # ... and sensitive to them.
    ceph.volumes["swap"].unlock(force=True)
    assert ceph.auth_cookie() != legacy


########################################################
# the per-VM rados lock


def foreign_lock(ceph_inst, cookie="someotherhost"):
    """Take the VM's rados lock on behalf of another host."""
    lock = ceph_inst.vm_lock
    ceph_inst.rados.lock_exclusive(
        lock.pool,
        lock.vm_name,
        lock.LOCK_NAME,
        cookie,
        namespace=lock.NAMESPACE,
    )


def test_rados_lock_object_creation_is_idempotent(ceph_inst):
    """`locker()` creates the sidecar object gratuitously, every time."""
    assert ceph_inst.vm_lock.locker() is None
    assert ceph_inst.vm_lock.locker() is None
    assert ceph_inst.vm_lock.locker() is None


def test_rados_lock_acquire_and_report(ceph_inst):
    lock = ceph_inst.vm_lock
    assert lock.locker() is None

    lock.lock()

    assert lock.locker().host == ceph_inst.CEPH_LOCK_HOST


def test_rados_lock_shortcuts_when_we_already_hold_it(ceph_inst):
    lock = ceph_inst.vm_lock
    lock.lock()
    get_log()

    lock.lock()

    # Took the shortcut: no retries, no second acquisition attempt.
    assert get_log() == "rados-lock machine=simplevm subsystem=ceph"


def test_rados_lock_gives_up_on_a_foreign_lock(ceph_inst):
    foreign_lock(ceph_inst)
    get_log()

    with pytest.raises(libceph.ImageBusy):
        ceph_inst.vm_lock.lock()

    assert get_log() == Ellipsis(
        """\
rados-lock machine=simplevm subsystem=ceph
rados-lock-retry machine=simplevm subsystem=ceph
rados-lock-retry machine=simplevm subsystem=ceph
rados-lock-retry machine=simplevm subsystem=ceph
rados-lock-failed competing=...cookie='someotherhost'... machine=simplevm subsystem=ceph"""
    )


def test_rados_unlock_leaves_foreign_locks_alone(ceph_inst):
    foreign_lock(ceph_inst)
    get_log()

    ceph_inst.vm_lock.unlock()

    assert ceph_inst.vm_lock.locker().host == "someotherhost"
    # Deliberately silent, mirroring `Volume.unlock()`: this is what the tail
    # of every outgoing migration looks like once the destination has taken
    # the lock.
    assert get_log() == ""


def test_rados_unlock_breaks_foreign_locks_when_forced(ceph_inst):
    foreign_lock(ceph_inst)
    get_log()

    ceph_inst.vm_lock.unlock(force=True)

    assert ceph_inst.vm_lock.locker() is None
    assert get_log() == "rados-break-lock machine=simplevm subsystem=ceph"


def test_rados_unlock_of_a_missing_lock_is_a_noop(ceph_inst):
    get_log()
    ceph_inst.vm_lock.unlock()
    assert get_log() == ""


def test_rados_lock_refuses_shared_locks(ceph_inst, monkeypatch):
    # Only a shared lock would ever report two lockers, which we do not use.
    monkeypatch.setattr(
        ceph_inst.rados,
        "lock_info",
        lambda *a, **kw: libceph.RadosLockInfo(
            name="kvm",
            type="shared",
            tag="",
            lockers=[
                libceph.RadosLocker(
                    name="client.a", cookie="host1", addr="127.0.0.1:0/1"
                ),
                libceph.RadosLocker(
                    name="client.b", cookie="host2", addr="127.0.0.1:0/2"
                ),
            ],
        ),
    )

    with pytest.raises(NotImplementedError):
        ceph_inst.vm_lock.locker()


@pytest.mark.live
def test_rados_lock_contention_against_real_rados(ceph_inst):
    """`VMLockManager` against the real `rados` CLI.

    Test contention and protection of foreign rados lock against a live
    cluster. This protects against error code drift and other behaviour
    changes, in cases only happening under multi-host lock contention.
    """
    lock = ceph_inst.vm_lock

    lock.lock()
    assert lock.locker().host == ceph_inst.CEPH_LOCK_HOST

    lock.unlock()
    foreign_lock(ceph_inst)

    # `(16) Device or resource busy` has to arrive as `ImageBusy`, otherwise
    # the retry loop would read a refusal as success.
    with pytest.raises(libceph.ImageBusy):
        lock.lock()
    assert lock.locker().host == "someotherhost"

    lock.unlock()
    assert lock.locker().host == "someotherhost"
    lock.unlock(force=True)
    assert lock.locker() is None


########################################################
# enabling the expected rbd image features


def test_features_are_only_enabled_once(ceph_inst):
    """`rbd feature enable` fails with (22) if the feature is already on, so
    the pre-check in `Image.ensure_feature` is load-bearing rather than
    cosmetic."""
    for spec in ceph_inst.specs.values():
        spec.ensure_presence()
        spec.ensure_features(online=False)
    get_log()

    # A second start of an already upgraded VM.
    for spec in ceph_inst.specs.values():
        spec.ensure_features(online=False)

    assert get_log() == ""


def test_cidata_keeps_the_legacy_feature_set(ceph_inst):
    """cidata must stay readable - and lockable - for old hosts."""
    spec = ceph_inst.specs["cidata"]
    spec.ensure_presence()
    spec.ensure_features(online=False)

    assert spec.volume.features == {"layering"}


def test_ensure_feature_invalidates_the_info_cache():
    """`ensure_features` enables several features in a row, so a stale cache
    would make the second one see the pre-enable feature set and try to enable
    something ceph already turned on implicitly.

    Exercises the real `libceph.Image`, because that is where the cache lives -
    `ImageMock.info` reads live state and could never catch this.
    """
    features = ["layering"]
    info_calls = []

    def rbd_(*args, **kw):
        if args[0] == "feature":
            features.append(args[3])
            return ""
        info_calls.append(args)
        return {"size": 1024, "features": list(features), "flags": []}

    # `RealImage` is bound at import time, before `ceph_mock` swaps
    # `libceph.Image` out for `ImageMock`.
    image = RealImage.__new__(RealImage)
    image.closed = False
    image._name = "rbd.hdd/simplevm.root"
    image.ioctx = Mock()
    image.ioctx.rados.rbd_ = rbd_

    assert image.info.features == {"layering"}
    # Second read is served from the cache, not from another `rbd info`.
    assert image.info.features == {"layering"}
    assert len(info_calls) == 1

    image.ensure_feature("exclusive-lock")

    assert image.info.features == {"layering", "exclusive-lock"}
    assert len(info_calls) == 2


########################################################
# rebuilding the object map
#
# These drive the mock's flag state directly: the subject is the traversal in
# `VolumeSpecification.ensure_object_map` - which images it looks at, and how
# many per run - while what `rbd object-map rebuild` itself does is covered in
# `tests/hazmat/test_libceph.py`.


OBJECT_MAP_INVALID = "object map invalid"


def object_map_flags(volume, snapname=None) -> list[str]:
    name = volume.name if snapname is None else f"{volume.name}@{snapname}"
    return volume.ioctx.rbd_images[name]["flags"]


def set_object_map(volume, snapname=None, *, valid: bool):
    flags = object_map_flags(volume, snapname)
    if valid and OBJECT_MAP_INVALID in flags:
        flags.remove(OBJECT_MAP_INVALID)
    elif not valid and OBJECT_MAP_INVALID not in flags:
        flags.append(OBJECT_MAP_INVALID)


@pytest.fixture
def root_spec(ceph_with_volumes):
    """The root spec, with the object map enabling left it with settled.

    A real cluster rebuilds in the background once asked, so a converged VM is
    the normal state and the interesting cases are set up from here.
    """
    spec = ceph_with_volumes.specs["root"]
    set_object_map(spec.volume, valid=True)
    return spec


def test_enabling_object_map_invalidates_it(ceph_inst):
    """Why `ensure_object_map` exists at all.

    Enabling object-map on an image that already holds data leaves the map
    unbuilt until someone rebuilds it. Read through `info` so this says the
    same thing against a real cluster.
    """
    spec = ceph_inst.specs["root"]
    spec.ensure_presence()
    assert OBJECT_MAP_INVALID not in spec.volume.rbdimage.info.flags

    spec.ensure_features(online=False)

    assert OBJECT_MAP_INVALID in spec.volume.rbdimage.info.flags


def test_object_map_rebuild_of_the_head_image(root_spec):
    set_object_map(root_spec.volume, valid=False)

    root_spec.ensure_object_map()

    assert object_map_flags(root_spec.volume) == []


def test_object_map_rebuild_is_skipped_when_settled(root_spec):
    set_object_map(root_spec.volume, valid=True)
    get_log()

    root_spec.ensure_object_map()

    assert get_log() == ""


def test_fc_seed(ceph_with_volumes_ci):
    ceph = ceph_with_volumes_ci
    cidata_spec = ceph.specs["cidata"]
    tmp_spec = ceph.specs["tmp"]
    cidata_spec.start()
    tmp_spec.start()

    with cidata_spec.volume.mounted() as target:
        fc_data = target / "fc-data"
        assert (fc_data / "enc.json").exists()
        assert (fc_data / "qemu-guest-properties-booted").exists()
        assert not (fc_data / "qemu-binary-generation-booted").exists()

    with tmp_spec.volume.mounted() as target:
        fc_data = target / "fc-data"
        assert (fc_data / "enc.json").exists()
        assert (fc_data / "qemu-guest-properties-booted").exists()
        assert (fc_data / "qemu-binary-generation-booted").exists()


def test_cloud_init_seed_simple(ceph_inst_cloudinit_enc):
    ceph = ceph_inst_cloudinit_enc
    libceph.RBD().create(
        ceph.ioctxs["rbd.ssd"],
        "simplevm.cidata",
        ceph.cfg["cidata_size"],
    )

    cidata_spec = ceph.specs["cidata"]
    cidata_spec.ensure_presence()
    with patch(
        "fc.qemu.directory.connect", autospec=True
    ) as directory_connect_mock:
        directory_mock = Mock()
        directory_connect_mock.return_value = directory_mock
        directory_mock.list_users.side_effect = lambda _rg: [
            {
                "class": "human",
                "email_addresses": ["test@example.com"],
                "gid": 100,
                "home_directory": "/home/test",
                "id": 1000,
                "login_shell": "/bin/zsh",
                "name": "Test Benutzer",
                "password": "{CRYPT}$6$rounds=656000$foobar",
                "permissions": {"test": ["login", "sudo-srv"]},
                "ssh_pubkey": [
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO+C/OaWGUbNrf45RYxzgxzX2OZBPLH9VararPYDuorg"
                ],
                "uid": "test",
            }
        ]
        cidata_spec.start()
        directory_mock.list_users.assert_called_once_with("test")
    with cidata_spec.volume.mounted() as target:
        metadata_file = target / "meta-data"
        assert metadata_file.read_text() == "instance-id: simplevm\n"
        userdata_file = target / "user-data"
        userdata_content = userdata_file.read_text()
        assert userdata_content.startswith("#cloud-config\n")
        userdata_content_parsed = yaml.safe_load(userdata_content)
        assert userdata_content_parsed == {
            "allow_public_ssh_keys": True,
            "ssh_pwauth": False,
            "disable_root": False,
            "package_update": True,
            "package_upgrade": True,
            "packages": ["qemu-guest-agent"],
            "hostname": "simplevm",
            "updates": {
                "network": {
                    "when": [
                        "boot-new-instance",
                        "boot",
                    ],
                },
            },
            "users": [{"name": "root"}],
            "write_files": [
                {
                    "content": "AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys_fc\n",
                    "path": "/etc/ssh/sshd_config.d/10-cloud-init-fc.conf",
                    "permissions": "0644",
                },
                {
                    "content": """\
### managed by Flying Circus - do not edit! ###
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO+C/OaWGUbNrf45RYxzgxzX2OZBPLH9VararPYDuorg
""",
                    "path": "/root/.ssh/authorized_keys_fc",
                    "permissions": "0600",
                },
            ],
            "runcmd": [
                "systemctl enable --now qemu-guest-agent",
                "systemctl restart ssh",
            ],
        }
        network_config_file = target / "network-config"
        network_config_content = network_config_file.read_text()
        network_config_content_parsed = yaml.safe_load(network_config_content)
        assert network_config_content_parsed == {
            "config": [
                {
                    "accept-ra": False,
                    "mac_address": "02:00:00:02:1d:e4",
                    "name": "ethpub",
                    "subnets": [
                        {
                            "address": "203.0.113.10/24",
                            "dns_nameservers": ["9.9.9.9", "8.8.8.8"],
                            "gateway": "293.0.113.1",
                            "type": "static",
                        },
                        {
                            "address": "2001:db8:500:2::5/64",
                            "dns_nameservers": [
                                "2620:fe::fe",
                                "2001:4860:4860::8888",
                            ],
                            "gateway": "2001:db8:500:2::1",
                            "type": "static6",
                        },
                    ],
                    "type": "physical",
                }
            ],
            "version": 1,
        }


def test_cloud_init_seed_routed_pub(ceph_inst_cloudinit_enc):
    ceph = ceph_inst_cloudinit_enc
    ceph.enc["parameters"]["interfaces"]["pub"]["routed"] = True
    libceph.RBD().create(
        ceph.ioctxs["rbd.ssd"],
        "simplevm.cidata",
        ceph.cfg["cidata_size"],
    )

    cidata_spec = ceph.specs["cidata"]
    cidata_spec.ensure_presence()
    cidata_spec.start()
    with cidata_spec.volume.mounted() as target:
        network_config_file = target / "network-config"
        network_config_content = network_config_file.read_text()
        network_config_content_parsed = yaml.safe_load(network_config_content)
        assert network_config_content_parsed == {
            "config": [
                {
                    "accept-ra": False,
                    "mac_address": "02:00:00:02:1d:e4",
                    "name": "ethpub",
                    "subnets": [
                        {
                            "address": "203.0.113.10/32",
                            "dns_nameservers": ["169.254.83.168"],
                            "gateway": "169.254.83.168",
                            "type": "static",
                        },
                        {
                            "address": "2001:db8:500:2::5/128",
                            "dns_nameservers": ["2001:db8:500:2::1"],
                            "gateway": "fe80::1",
                            "type": "static6",
                        },
                    ],
                    "type": "physical",
                }
            ],
            "version": 1,
        }


@pytest.mark.live
def test_rbd_pool_migration(ceph_inst, patterns) -> None:
    ceph_inst.cfg["tmp_size"] = 500 * 1024 * 1024
    ceph_inst.cfg["swap_size"] = 50 * 1024 * 1024
    ceph_inst.cfg["root_size"] = 50 * 1024 * 1024
    ceph_inst.cfg["cidata_size"] = 10 * 1024 * 1024
    libceph.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.root",
        ceph_inst.cfg["root_size"],
    )
    libceph.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.tmp",
        ceph_inst.cfg["tmp_size"],
    )
    libceph.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.swap",
        ceph_inst.cfg["swap_size"],
    )
    libceph.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.cidata",
        ceph_inst.cfg["cidata_size"],
    )
    assert ceph_inst.specs["root"].exists_in_pool() == "rbd.ssd"
    assert ceph_inst.specs["swap"].exists_in_pool() == "rbd.ssd"
    assert ceph_inst.specs["tmp"].exists_in_pool() == "rbd.ssd"
    assert ceph_inst.specs["cidata"].exists_in_pool() == "rbd.ssd"

    ceph_inst.start()
    ceph_inst.status()

    first_start = patterns.first_start
    first_start.optional(
        """
waiting interval=0 machine=simplevm remaining=... subsystem=ceph volume=rbd.hdd/simplevm.tmp
waiting interval=0 machine=simplevm remaining=... subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk> Setting name!
sgdisk> partNum is 0
mkfs.xfs>       mkfs.xfs: small data volume, ignoring data volume stripe unit ... and stripe width ...
found-partition ...
udevadm args=settle machine=simplevm subsystem=ceph volume=rbd.hdd/...
udevadm machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/...
blkid args=/dev/rbd/rbd.hdd/simplevm...-part1 machine=simplevm subsystem=ceph volume=rbd.hdd/...
blkid>  /dev/rbd/rbd.hdd/simplevm...-part1: ...
blkid machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/...
"""
    )
    first_start.in_order(
        """
ensure-size machine=simplevm subsystem=ceph volume_spec=root
start machine=simplevm subsystem=ceph volume_spec=root
start-root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
root-found-in current_pool=rbd.ssd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd args=status --format json rbd.ssd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd>    {"watchers":[]}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root

migrate-vm-root-disk action=start machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd subsystem=ceph volume=rbd.ssd/simplevm.root
rbd args=migration prepare rbd.ssd/simplevm.root rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=simplevm.root
rbd machine=simplevm returncode=0 subsystem=ceph volume=simplevm.root

ensure-size machine=simplevm subsystem=ceph volume_spec=swap
start machine=simplevm subsystem=ceph volume_spec=swap
start-swap machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
mkswap args=-f -L "swap" /dev/rbd/rbd.hdd/simplevm.swap machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
mkswap> Setting up swapspace version 1, size = 50 MiB (52424704 bytes)
mkswap> LABEL=swap, UUID=...-...-...-...-...
mkswap machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.swap

ensure-size machine=simplevm subsystem=ceph volume_spec=tmp
start machine=simplevm subsystem=ceph volume_spec=tmp
start-tmp machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
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
seed-fc machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
partprobe args=/dev/rbd/rbd.hdd/simplevm.tmp machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
mount args="/dev/rbd/rbd.hdd/simplevm.tmp-part1" "/mnt/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
guest-properties machine=simplevm properties={'binary_generation': 2} subsystem=ceph volume=rbd.hdd/simplevm.tmp
binary-generation generation=2 machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
umount args="/mnt/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
ensure-size machine=simplevm subsystem=ceph volume_spec=cidata
start machine=simplevm subsystem=ceph volume_spec=cidata
start-cloud-init machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
create-fs machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk args=-o "/dev/rbd/rbd.hdd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk> Creating new GPT entries in memory.
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk args=-n 1:: -c "1:cidata" -t 1:8300 "/dev/rbd/rbd.hdd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
partprobe args=/dev/rbd/rbd.hdd/simplevm.cidata machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
mkfs.vfat args=-n "cidata" /dev/rbd/rbd.hdd/simplevm.cidata-part1 machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
mkfs.vfat>      mkfs.fat: Warning: lowercase labels might not work properly on some systems
mkfs.vfat>      mkfs.fat 4.2 (2021-01-31)
mkfs.vfat machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
seed-fc machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
partprobe args=/dev/rbd/rbd.hdd/simplevm.cidata machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
mount args="/dev/rbd/rbd.hdd/simplevm.cidata-part1" "/mnt/rbd/rbd.hdd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
guest-properties machine=simplevm properties={'binary_generation': 2} subsystem=ceph volume=rbd.hdd/simplevm.cidata
umount args="/mnt/rbd/rbd.hdd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
vm-lock-status locker=host1 machine=simplevm subsystem=ceph
rbd-status locker=None machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"prepared","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=prepared subsystem=ceph volume=rbd.hdd/simplevm.root
rbd-status locker=None machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=None machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd-status locker=host1 machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.cidata
"""
    )

    first_start.in_order(MIGRATION_PHASE_PRESENCE)
    first_start.in_order(MIGRATION_LOCKS)
    first_start.in_order(MIGRATION_ENABLE_FEATURES)
    # root/swap/tmp are covered by the VM-level rados lock, so they must never
    # be rbd-locked - and the pre-upgrade lock has to be gone, not just
    # unused.
    first_start.refused(
        """
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
"""
    )

    assert get_log() == first_start

    assert ceph_inst.specs["root"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["swap"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["tmp"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["cidata"].exists_in_pool() == "rbd.hdd"

    # Against a real cluster, not just the mock: the images that moved pools
    # came out the other side with the expected feature set, and the lock
    # split is the one `test_locks_are_split_between_cidata_and_the_rest`
    # describes.
    for suffix in EXCLUSIVE_LOCK_VOLUMES:
        volume = ceph_inst.volumes[suffix]
        assert volume.features >= {"exclusive-lock", "object-map", "fast-diff"}
        assert volume.lock_status() is None
    cidata = ceph_inst.volumes["cidata"]
    assert "exclusive-lock" not in cidata.features
    assert cidata.lock_status().host == ceph_inst.CEPH_LOCK_HOST

    ceph_inst.ensure()
    ceph_inst.status()

    first_ensure = patterns.first_ensure
    first_ensure.in_order(
        """
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"prepared","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=prepared subsystem=ceph volume=rbd.hdd/simplevm.root

root-migration-execute machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
ceph args=rbd task add migration execute rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root"""
        +
        # XXX: Excecuting a Nautilus ceph-client on a Pacific cluster results in the following bug:
        # `ceph rbd task add migration execute` output does not show the source image name in the "message", resulting in
        # "Migrating image rbd.ssd/ to rbd.ssd/simplevm.root"
        # FIXME: permitting this mismatch for now, need to verify in dev cluster that migrations are not broken and this is just a display error
        # XXX: The destination pool is an ellipsis because due to a ceph bug it is displayed incorrectly. We only patch it from Pacific on.
        """
ceph>   {"sequence": ..., "id": "...-...-...-...-...", "message": "Migrating image rbd.ssd/... to rbd.../simplevm.root", "refs": {"action": "migrate execute", "pool_name": "rbd.hdd", "pool_namespace": "", "image_name": "simplevm.root", "image_id": "..."}}
ceph machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

vm-lock-status locker=host1 machine=simplevm subsystem=ceph
rbd-status locker=None machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":...,"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"...","state_description":...}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress=...status=... subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=None machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=None machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd-status locker=host1 machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.cidata
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
rbd>    {"watchers":[],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"executed","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=executed subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-commit machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=--no-progress migration commit rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

vm-lock-status locker=host1 machine=simplevm subsystem=ceph
rbd-status locker=None machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[]}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=None machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=None machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd-status locker=host1 machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.cidata
"""
    )

    assert get_log() == commit_ensure
