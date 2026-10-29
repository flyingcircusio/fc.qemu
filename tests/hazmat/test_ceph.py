import subprocess
import textwrap
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from fc.qemu.hazmat import libceph
from fc.qemu.hazmat.ceph import LockMode
from fc.qemu.hazmat.libceph import Image as RealImage
from tests.conftest import (
    UPDATE_LOCK_METHOD_NO_IMAGES,
    both_lock_modes,
    get_log,
    rbd_lock_mode,
    update_lock_method,
)
from tests.ellipsis import Ellipsis


def settle_lock_method(ceph_inst):
    """Apply the configured feature state and re-decide the lock mechanism.

    `Ceph.start()` does this between `ensure_presence` and taking any lock.
    Fixtures and tests that drive the volumes directly have to do it
    themselves, otherwise `lock_mode` has no effect and everything stays on
    the rbd path. PL-134255: goes away with the rbd lock path.
    """
    ceph_inst._ensure_image_features()
    ceph_inst._update_lock_method()


# PL-134255: literal expectations for both lock mechanisms, so the two can be
# read side by side. Delete the `*_RBD` constants with the rbd lock path.
MIGRATION_LOCKS_RBD = """\
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
"""
MIGRATION_LOCKS_RADOS = """\
rados-lock machine=simplevm subsystem=ceph
"""

#: `_ensure_image_features` runs after every volume is present. Root is still
#: in the source pool at that point, the others have been recreated in the
#: destination pool by `pre_start`. rbd image features should be enabled in
#: an order which avoids dependent features being enabled implicitly.
MIGRATION_ENABLE_FEATURES_RADOS = """\
enable-rbd-features features=('exclusive-lock',) machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
enable-rbd-features features=('object-map', 'fast-diff') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
enable-rbd-features features=('exclusive-lock',) machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
enable-rbd-features features=('object-map', 'fast-diff') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
enable-rbd-features features=('exclusive-lock',) machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
enable-rbd-features features=('object-map', 'fast-diff') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
enable-rbd-features features=('exclusive-lock',) machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
enable-rbd-features features=('object-map', 'fast-diff') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
"""

#: Root is mid-migration and therefore unlocked; the others are held.
#: `Ceph.status()` emits `rados-lock-status` once, ahead of every volume.
MIGRATION_STATUS_ROOT_RBD = """\
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root"""
MIGRATION_STATUS_ROOT_RADOS = """\
rados-lock-status locker=('client...', '...') machine=simplevm subsystem=ceph
rbd-status machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.root"""

MIGRATION_STATUS_TAIL_RBD = """\
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata"""
MIGRATION_STATUS_TAIL_RADOS = """\
rbd-status machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd-status machine=simplevm present=True subsystem=ceph volume=rbd.hdd/simplevm.cidata"""


#: `Ceph.start()` pre-starts and ensures presence for *every* volume before it
#: settles the image features and takes any lock. Checked as its own
#: `in_order` op, so the per-volume sizing/starting work is not additionally
#: pinned to how these two phases interleave with it.
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


#: `RootSpec.pool_migration` releases the source volume before
#: `rbd migration prepare`. On the rados path `Volume.unlock` returns without
#: logging, because the VM-level lock covers the volume.
MIGRATION_ROOT_UNLOCK_RBD = (
    "unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root"
)
MIGRATION_ROOT_UNLOCK_RADOS = ""


def migration_status(lock_mode, text: str) -> str:
    """Fill the mechanism-dependent slots in a pattern body.

    `.replace` rather than `.format`, because the bodies carry literal JSON
    braces from `rbd --format json` output. A slot may expand to nothing,
    which just leaves a blank line - `pattern_lines` filters those out.
    """
    rbd = lock_mode == "rbd"
    return (
        text.replace(
            "{root_unlock}",
            MIGRATION_ROOT_UNLOCK_RBD if rbd else MIGRATION_ROOT_UNLOCK_RADOS,
        )
        .replace(
            "{status_root}",
            MIGRATION_STATUS_ROOT_RBD if rbd else MIGRATION_STATUS_ROOT_RADOS,
        )
        .replace(
            "{status_tail}",
            MIGRATION_STATUS_TAIL_RBD if rbd else MIGRATION_STATUS_TAIL_RADOS,
        )
    )


@pytest.fixture
def ceph_with_volumes(ceph_inst):
    for vol in ceph_inst.specs.values():
        vol.ensure_presence()
    settle_lock_method(ceph_inst)
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
    for vol in ceph_inst.specs.values():
        vol.ensure_presence()
    settle_lock_method(ceph_inst)
    ceph_inst.lock()
    yield ceph_inst
    ceph_inst.force_unlock()
    for volume in ceph_inst.opened_volumes:
        volume.snapshots.purge()
        volume.close()
        libceph.RBD().remove(volume.ioctx, volume.name)


# --- PL-134255: rbd lock path, remove this section with it ----------------


@rbd_lock_mode
def test_ceph_stop_should_unlock_all_volumes(ceph_with_volumes):
    for volume in ceph_with_volumes.opened_volumes:
        assert volume.lock_status()
    ceph_with_volumes.stop()
    for volume in ceph_with_volumes.opened_volumes:
        assert volume.lock_status() is None


@rbd_lock_mode
def test_ceph_stop_remove_only_own_locks(ceph_with_volumes):
    """Test case where failed migrations leave inconsistent locking."""
    ceph_with_volumes.volumes["root"].unlock()
    ceph_with_volumes.volumes["root"].rbdimage.lock_exclusive("someotherhost")
    # It unlocks what it can.
    ceph_with_volumes.stop()
    assert ceph_with_volumes.volumes["root"].lock_status()
    assert ceph_with_volumes.volumes["swap"].lock_status() is None
    assert ceph_with_volumes.volumes["tmp"].lock_status() is None


# --- end of the rbd-only section ------------------------------------------


def test_ceph_stop_should_release_the_rados_lock(ceph_with_volumes):
    ceph = ceph_with_volumes
    assert ceph.lock_mode == LockMode.RADOS
    assert ceph.rados_lock.lock_status()[1] == ceph.CEPH_LOCK_HOST
    # No volume carries a lock of its own anymore.
    for volume in ceph.opened_volumes:
        assert volume.lock_status() is None
    ceph.stop()
    assert ceph.rados_lock.lock_status() is None


def test_ceph_stop_leaves_a_foreign_rados_lock_alone(ceph_with_volumes):
    """A failed migration can leave the lock with the other host."""
    ceph = ceph_with_volumes
    ceph.assume_locked = False
    ceph.rados_lock.unlock()
    ceph.rados.lock_exclusive(
        ceph.orchestration_pool,
        "simplevm",
        ceph.rados_lock.LOCK_NAME,
        "someotherhost",
        namespace=ceph.rados_lock.NAMESPACE,
    )

    ceph.stop()
    # Not ours, so we keep our hands off it.
    assert ceph.rados_lock.lock_status()[1] == "someotherhost"
    assert ceph.locked_by() == "someotherhost"
    assert ceph.locked_by_me() is False

    # ... but force_unlock breaks it.
    ceph.force_unlock()
    assert ceph.rados_lock.lock_status() is None


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


@both_lock_modes
def test_is_unlocked(ceph_with_volumes):
    assert ceph_with_volumes.is_unlocked() is False
    ceph_with_volumes.unlock()
    assert ceph_with_volumes.is_unlocked() is True


@both_lock_modes
def test_lock_roundtrip_reports_ownership(ceph_with_volumes):
    """Whichever mechanism is active, the observable API is the same."""
    ceph = ceph_with_volumes
    assert ceph.locked_by_me() is True
    assert ceph.locked_by() == ceph.CEPH_LOCK_HOST
    assert ceph.is_unlocked() is False

    ceph.unlock()

    assert ceph.locked_by_me() is False
    assert ceph.locked_by() is None
    assert ceph.is_unlocked() is True


@both_lock_modes
def test_lock_is_idempotent(ceph_with_volumes):
    """Locking again while we hold the lock must not raise."""
    ceph = ceph_with_volumes
    ceph.lock()
    ceph.lock()
    assert ceph.locked_by_me() is True


@both_lock_modes
def test_auth_cookie_changes_with_lock_state(ceph_with_volumes):
    locked = ceph_with_volumes.auth_cookie()
    ceph_with_volumes.unlock()
    assert ceph_with_volumes.auth_cookie() != locked


def test_multiple_images_raises_error(ceph_inst):
    libceph.RBD().create(ceph_inst.ioctxs["rbd.hdd"], "simplevm.root", 1024)
    libceph.RBD().create(ceph_inst.ioctxs["rbd.ssd"], "simplevm.root", 1024)
    root_spec = ceph_inst.specs["root"]
    assert sorted(root_spec.exists_in_pools()) == ["rbd.hdd", "rbd.ssd"]
    with pytest.raises(RuntimeError):
        root_spec.exists_in_pool()


########################################################
# the per-VM rados lock


def foreign_lock(ceph_inst, cookie="someotherhost"):
    """Take the VM's rados lock on behalf of another host."""
    lock = ceph_inst.rados_lock
    ceph_inst.rados.lock_exclusive(
        lock.pool,
        lock.vm_name,
        lock.LOCK_NAME,
        cookie,
        namespace=lock.NAMESPACE,
    )


def test_rados_lock_ensure_is_idempotent(ceph_inst):
    # `Ceph.__enter__` already created the sidecar object, so this is the
    # second and third call.
    ceph_inst.rados_lock.ensure()
    ceph_inst.rados_lock.ensure()
    assert ceph_inst.rados_lock.lock_status() is None


def test_rados_lock_acquire_and_report(ceph_inst):
    lock = ceph_inst.rados_lock
    assert lock.lock_status() is None

    lock.lock()

    assert lock.lock_status()[1] == ceph_inst.CEPH_LOCK_HOST
    assert ceph_inst.assume_locked is True


def test_rados_lock_shortcuts_when_we_already_hold_it(ceph_inst):
    lock = ceph_inst.rados_lock
    lock.lock()
    get_log()

    lock.lock()

    # Took the shortcut: no retries, no second acquisition attempt.
    assert get_log() == "rados-lock machine=simplevm subsystem=ceph"


def test_rados_lock_gives_up_on_a_foreign_lock(ceph_inst):
    foreign_lock(ceph_inst)
    get_log()

    with pytest.raises(libceph.ImageBusy):
        ceph_inst.rados_lock.lock()

    assert get_log() == Ellipsis(
        """\
rados-lock machine=simplevm subsystem=ceph
rados-lock-retry machine=simplevm subsystem=ceph
rados-lock-retry machine=simplevm subsystem=ceph
rados-lock-retry machine=simplevm subsystem=ceph
rados-lock-failed competing=('...', 'someotherhost') machine=simplevm subsystem=ceph"""
    )
    assert ceph_inst.assume_locked is False


def test_rados_unlock_leaves_foreign_locks_alone(ceph_inst):
    foreign_lock(ceph_inst)
    get_log()

    ceph_inst.rados_lock.unlock()

    assert ceph_inst.rados_lock.lock_status()[1] == "someotherhost"
    # Deliberately silent, mirroring `Volume.unlock()`: this is what the tail
    # of every outgoing migration looks like once the destination has taken
    # the lock.
    assert get_log() == ""


def test_rados_unlock_breaks_foreign_locks_when_forced(ceph_inst):
    foreign_lock(ceph_inst)
    get_log()

    ceph_inst.rados_lock.unlock(force=True)

    assert ceph_inst.rados_lock.lock_status() is None
    assert get_log() == "rados-break-lock machine=simplevm subsystem=ceph"


def test_rados_unlock_of_a_missing_lock_is_a_noop(ceph_inst):
    get_log()
    ceph_inst.rados_lock.unlock()
    assert get_log() == ""


def test_rados_lock_refuses_shared_locks(ceph_inst, monkeypatch):
    # Only a shared lock would ever report two lockers, which we do not use.
    monkeypatch.setattr(
        ceph_inst.rados,
        "lock_list_by_name",
        lambda *a, **kw: {
            "lockers": [
                ("client.a", "host1", "127.0.0.1:0/1"),
                ("client.b", "host2", "127.0.0.1:0/2"),
            ]
        },
    )

    with pytest.raises(NotImplementedError):
        ceph_inst.rados_lock.lock_status()


def test_rados_lock_status_is_not_queried_on_the_rbd_path(ceph_inst):
    """Callers have to dispatch; this guards against silent cross-talk."""
    ceph_inst.lock_mode = LockMode.RBD
    with pytest.raises(AssertionError):
        ceph_inst.rados_lock.lock_status()


@pytest.mark.live
def test_rados_lock_contention_against_real_rados(ceph_inst):
    """`VMLockMgr` against the real `rados` CLI.

    Test contention and protection of foreign rados lock against a live cluster.
    This protects against error code drift and other behaviour changes, in
    cases only happening under multi-host lock contention.
    """
    lock = ceph_inst.rados_lock
    # No volumes exist yet, which settles on the rados path either way.
    ceph_inst._update_lock_method()
    assert ceph_inst.lock_mode == LockMode.RADOS

    lock.lock()
    assert lock.lock_status()[1] == ceph_inst.CEPH_LOCK_HOST

    ceph_inst.assume_locked = False
    lock.unlock()
    foreign_lock(ceph_inst)

    # `(16) Device or resource busy` has to arrive as `ImageBusy`, otherwise
    # the retry loop would read a refusal as success.
    with pytest.raises(libceph.ImageBusy):
        lock.lock()
    assert lock.lock_status()[1] == "someotherhost"

    lock.unlock()
    assert lock.lock_status()[1] == "someotherhost"
    lock.unlock(force=True)
    assert lock.lock_status() is None


########################################################
# choosing between the two lock mechanisms


def set_features(ceph_inst, **features_by_suffix):
    for suffix, features in features_by_suffix.items():
        volume = ceph_inst.volumes[suffix]
        volume.ioctx.rbd_images[volume.name]["features"] = list(features)


@pytest.fixture
def ceph_with_present_volumes(ceph_inst):
    """Volumes exist, but nothing has settled the lock method yet."""
    for spec in ceph_inst.specs.values():
        spec.ensure_presence()
    return ceph_inst


def test_lock_method_rados_when_all_volumes_have_the_feature(
    ceph_with_present_volumes,
):
    ceph = ceph_with_present_volumes
    for suffix in ceph.volumes:
        set_features(ceph, **{suffix: ["layering", "exclusive-lock"]})

    ceph._update_lock_method()

    assert ceph.lock_mode == LockMode.RADOS


def test_lock_method_rbd_when_no_volume_has_the_feature(
    ceph_with_present_volumes,
):
    ceph = ceph_with_present_volumes
    for suffix in ceph.volumes:
        set_features(ceph, **{suffix: ["layering"]})

    ceph._update_lock_method()

    assert ceph.lock_mode == LockMode.RBD


def test_lock_method_mixed_falls_back_to_rbd_locks(ceph_with_present_volumes):
    """The transient state while the cluster raises rbd_default_features.

    `pre_start` recreates swap/tmp/cidata with the feature while an older root
    clone still lacks it. The two flags are strict complements, so this does
    *not* run both mechanisms in parallel: a single volume without the feature
    puts the whole VM on the rbd path.
    """
    ceph = ceph_with_present_volumes
    # set up a mixed set of features on different volumes
    set_features(
        ceph,
        root=["layering"],
        swap=["layering", "exclusive-lock"],
        tmp=["layering", "exclusive-lock", "fast-diff", "object-map"],
        cidata=["layering", "exclusive-lock", "fast-diff", "object-map"],
    )

    ceph._update_lock_method()

    assert ceph.lock_mode == LockMode.RBD

    # Only the rbd locks get taken:
    # A single volume without the feature puts the whole VM back on the rbd
    # path. That is the conservative choice - an older fc.qemu looks at
    # exactly those locks - and it means `rados_lock` stays untouched, down to
    # `lock_status()` refusing to answer at all.
    ceph.lock()
    assert ceph.volumes["root"].lock_status()[1] == ceph.CEPH_LOCK_HOST
    ceph.unlock()
    assert ceph.volumes["root"].lock_status() is None


def test_lock_method_ignores_the_migrating_feature(ceph_with_present_volumes):
    """`rbd migration prepare` adds `migrating` to the feature list."""
    ceph = ceph_with_present_volumes
    for suffix in ceph.volumes:
        set_features(
            ceph,
            **{suffix: ["layering", "exclusive-lock", "migrating"]},
        )

    ceph._update_lock_method()

    assert ceph.lock_mode == LockMode.RADOS

    for suffix in ceph.volumes:
        set_features(ceph, **{suffix: ["layering", "migrating"]})

    ceph._update_lock_method()

    assert ceph.lock_mode == LockMode.RBD


def test_lock_method_without_volumes_selects_rados(ceph_inst):
    """A VM whose images do not exist yet: `all([])`/`any([])` pick rados.

    Not the state `Agent.ensure` sees - every branch of `Agent.ensure_` that
    consults a lock attaches the volumes first, so `require_inmigrate` and
    friends always decide from real image features. Unattached, this method is
    not called at all and the flags stay at their `Ceph.__init__` default.

    Reachable in production: a VM whose images do not exist yet. The spec
    placeholders keep `self.volumes` truthy, so `_update_lock_method`'s
    `assert self.volumes` passes while `opened_volumes` is empty - that assert
    does not imply attachment.
    """
    assert list(ceph_inst.opened_volumes) == []

    ceph_inst._update_lock_method()

    assert ceph_inst.lock_mode == LockMode.RADOS
    # Must not trip the `assert self.lock_mode == LockMode.RBD` in the legacy branch.
    assert ceph_inst.is_unlocked() is True
    assert ceph_inst.locked_by_me() is False
    assert ceph_inst.locked_by() is None


########################################################
# enabling the exclusive-lock feature at cold boot
# PL-134255: this whole section goes away with the rbd lock path.


@rbd_lock_mode
def test_exclusive_lock_is_not_enabled_by_default(ceph_with_present_volumes):
    """The shipped configuration must not change any VM's behaviour."""
    ceph = ceph_with_present_volumes
    assert ceph.ensure_exclusive_lock is False
    get_log()

    ceph._ensure_image_features()

    assert get_log() == ""
    for volume in ceph.opened_volumes:
        assert volume.rbdimage.features == ["layering"]

    ceph._update_lock_method()
    assert ceph.lock_mode == LockMode.RBD


def test_exclusive_lock_gets_enabled_when_configured(
    ceph_with_present_volumes,
):
    ceph = ceph_with_present_volumes
    ceph.ensure_exclusive_lock = True
    get_log()

    ceph._ensure_image_features()

    # Two lines per volume, not three: enabling `fast-diff` makes ceph pull in
    # `object-map` as well, so the pre-check skips it.
    assert (
        get_log()
        == textwrap.dedent("""\
    enable-rbd-features features=('exclusive-lock',) machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
    enable-rbd-features features=('object-map', 'fast-diff') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
    enable-rbd-features features=('exclusive-lock',) machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
    enable-rbd-features features=('object-map', 'fast-diff') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
    enable-rbd-features features=('exclusive-lock',) machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
    enable-rbd-features features=('object-map', 'fast-diff') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
    enable-rbd-features features=('exclusive-lock',) machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
    enable-rbd-features features=('object-map', 'fast-diff') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata""")
    )
    # Which features ended up on the image, regardless of how `rbd info`
    # happens to order them.
    for volume in ceph.opened_volumes:
        assert {
            f for fs in ceph.exclusive_lock_required_features for f in fs
        } <= set(volume.rbdimage.features)

    # The decision has to flip within the same start().
    ceph._update_lock_method()
    assert ceph.lock_mode == LockMode.RADOS


def test_exclusive_lock_is_only_enabled_once(ceph_with_present_volumes):
    """`rbd feature enable` fails with (22) if the feature is already on, so
    the pre-check is load-bearing rather than cosmetic."""
    ceph = ceph_with_present_volumes
    ceph.ensure_exclusive_lock = True
    ceph._ensure_image_features()
    get_log()

    # A second cold boot of an already upgraded VM.
    ceph._ensure_image_features()

    assert get_log() == ""


def test_enable_feature_is_not_idempotent(ceph_with_present_volumes):
    """Pins the behaviour the pre-check exists for, as measured on Pacific."""
    image = ceph_with_present_volumes.volumes["root"].rbdimage
    image.enable_feature("exclusive-lock")

    with pytest.raises(subprocess.CalledProcessError) as e:
        image.enable_feature("exclusive-lock")

    assert e.value.returncode == 22
    assert "already enabled" in e.value.output


def test_enable_feature_invalidates_the_feature_cache():
    """`_update_lock_method` reads `features` right after the enable, so a
    stale cache would make the mechanism choice lag a whole start behind.

    Exercises the real `libceph.Image`, because that is where the cache lives
    - `ImageMock.features` reads live state and could never catch this.
    """
    features = ["layering"]
    info_calls = []

    def rbd_(*args, **kw):
        if args[0] == "feature":
            features.append(args[3])
            return ""
        info_calls.append(args)
        return {"features": list(features)}

    # `RealImage` is bound at import time, before `ceph_mock` swaps
    # `libceph.Image` out for `ImageMock`.
    image = RealImage.__new__(RealImage)
    image.closed = False
    image._name = "rbd.hdd/simplevm.root"
    image.ioctx = Mock()
    image.ioctx.rados.rbd_ = rbd_

    assert image.features == ["layering"]
    # Second read is served from the cache, not from another `rbd info`.
    assert image.features == ["layering"]
    assert len(info_calls) == 1

    image.enable_feature("exclusive-lock")

    assert image.features == ["layering", "exclusive-lock"]
    assert len(info_calls) == 2


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
@both_lock_modes
def test_rbd_pool_migration(ceph_inst, patterns, lock_mode) -> None:
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
        migration_status(
            lock_mode,
            """
ensure-size machine=simplevm subsystem=ceph volume_spec=root
start machine=simplevm subsystem=ceph volume_spec=root
start-root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
root-found-in current_pool=rbd.ssd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd args=status --format json rbd.ssd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd>    {"watchers":[]}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root

migrate-vm-root-disk action=start machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd subsystem=ceph volume=rbd.ssd/simplevm.root
{root_unlock}
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
{status_root}
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"prepared","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=prepared subsystem=ceph volume=rbd.hdd/simplevm.root
{status_tail}
""",
        )
    )

    first_start.in_order(UPDATE_LOCK_METHOD_NO_IMAGES * 2)
    first_start.in_order(MIGRATION_PHASE_PRESENCE)
    # PL-134255: whichever mechanism is active, the other must stay silent.
    rbd = lock_mode == "rbd"
    other_mode = "rados" if rbd else "rbd"
    first_start.in_order(
        update_lock_method(lock_mode)
        + (MIGRATION_LOCKS_RBD if rbd else MIGRATION_LOCKS_RADOS)
    )
    first_start.refused(
        update_lock_method(other_mode)
        + (MIGRATION_LOCKS_RADOS if rbd else MIGRATION_LOCKS_RBD)
    )
    if lock_mode != "rbd":
        first_start.in_order(MIGRATION_ENABLE_FEATURES_RADOS)

    assert get_log() == first_start

    assert ceph_inst.specs["root"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["swap"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["tmp"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["cidata"].exists_in_pool() == "rbd.hdd"

    ceph_inst.ensure()
    ceph_inst.status()

    first_ensure = patterns.first_ensure
    first_ensure.in_order(
        migration_status(
            lock_mode,
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

{status_root}
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":...,"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"...","state_description":...}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress=...status=... subsystem=ceph volume=rbd.hdd/simplevm.root

{status_tail}
""",
        )
    )

    assert get_log() == first_ensure

    while "status=executed" not in get_log():
        ceph_inst.status()

    ceph_inst.ensure()
    ceph_inst.status()

    commit_ensure = patterns.commit_ensure
    commit_ensure.in_order(
        migration_status(
            lock_mode,
            """
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"executed","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=executed subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-commit machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=--no-progress migration commit rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

{status_root}
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[]}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

{status_tail}
""",
        )
    )

    assert get_log() == commit_ensure
