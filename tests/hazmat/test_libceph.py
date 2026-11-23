import subprocess
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import MagicMock, Mock

import pytest

from fc.qemu import util
from fc.qemu.hazmat.libceph import Image, ImageBusy, ImageNotFound


@pytest.mark.live
def test_rbd_basic_api(ceph_inst):
    pool = ceph_inst.ioctxs["rbd.ssd"]

    with pytest.raises(ImageNotFound):
        Image(pool, "test")

    ceph_inst.rbd.create(pool, "test", 1000)

    image = Image(pool, "test")

    assert image.info.size == 1000
    image.resize(2000)
    assert image.info.size == 2000

    assert image.list_lockers() == []
    image.lock_exclusive("test")
    assert image.list_lockers()[0].id == "test"
    assert image.list_lockers()[0].host == "test"
    # allow double locking
    image.lock_exclusive("test")
    assert image.list_lockers()[0].id == "test"

    with pytest.raises(ImageBusy):
        image.lock_exclusive("foobar")
    assert image.list_lockers()[0].id == "test"

    image.unlock("test")
    assert image.list_lockers() == []

    with pytest.raises(ImageBusy):
        image.unlock("foobar")

    device = image.map()
    assert Path(device).exists()
    image.map()
    image.unmap()
    assert not Path(device).exists()
    image.unmap()

    assert image.list_snaps() == []
    image.create_snap("foo")
    snaps = image.list_snaps()
    assert len(snaps) == 1
    snap = snaps[0]
    assert set(snap) == {"id", "name", "protected", "size", "timestamp"}
    snap.pop("id")
    snap.pop("timestamp")
    assert snap == {
        "name": "foo",
        "protected": "false",
        "size": 2000,
    }

    snap_image = Image(pool, "test", "foo")
    snap_image.map()
    snap_image.unmap()

    image.remove_snap("foo")

    assert image.list_snaps() == []

    ceph_inst.rbd.remove(pool, "test")

    image.close()
    assert image.closed
    with pytest.raises(AssertionError):
        image.list_lockers()


@pytest.mark.live
def test_rbd_unexpected_output_does_not_cause_image_not_found(
    ceph_inst, monkeypatch
):
    pool = ceph_inst.ioctxs["rbd.ssd"]

    def failing_cmd(*args, **kw):
        raise CalledProcessError(returncode=1, cmd="foo", output="foobar")

    monkeypatch.setattr(util, "cmd", failing_cmd)
    with pytest.raises(CalledProcessError):
        Image(pool, "test")


@pytest.mark.live
def test_rbd_unexpected_exception_does_not_cause_image_not_found(
    ceph_inst, monkeypatch
):
    pool = ceph_inst.ioctxs["rbd.ssd"]

    def failing_cmd(*args, **kw):
        raise KeyError()

    monkeypatch.setattr(util, "cmd", failing_cmd)
    with pytest.raises(KeyError):
        Image(pool, "test")


def fake_image(size=1024, features=(), flags=(), snapname=None):
    """A real `Image` over a mocked ioctx, reporting what we tell it to.

    `reported` is answered afresh on every `rbd info`, so mutating it is how a
    test says "the image changed underneath us", and
    `img.ioctx.rados.rbd_.call_count` tells a cache hit from a re-read.

    We cannot re-use `IoctxMock` from conftest because that is way too
    abstract and has no proper way of integrating a real(ish) Image object.
    """
    reported = {"size": size, "features": list(features), "flags": list(flags)}
    ioctxmock = MagicMock()
    ioctxmock.name = "rbd.hdd"
    ioctxmock.rados.conffile = "/etc/ceph/ceph.conf"
    ioctxmock.rados.name = "client.admin"
    ioctxmock.rados.rbd_ = Mock(side_effect=lambda *a, **kw: dict(reported))
    return Image(ioctxmock, "testimg", snapname), reported


def test_refresh_drops_the_info_cache():
    """`info` is cached, so whoever changes the image has to say so.

    `Agent.ensure_online_disk_size` resizes the root disk through qemu, which
    cannot invalidate this cache by itself - without the explicit refresh the
    next `Volume.size` keeps reporting the pre-resize size.
    """
    img, reported = fake_image(size=1024)

    assert img.info.size == 1024
    reported["size"] = 2048
    assert img.info.size == 1024, "second read must come from the cache"

    img.refresh()

    assert img.info.size == 2048


def test_refresh_tolerates_a_cold_cache():
    """A plain `del image.info` would raise here.

    `cached_property` defines no `__delete__`, so `del` falls through to
    removing the instance-dict entry - which does not exist until something
    has read `info` once.
    """
    img, _ = fake_image()

    img.refresh()
    img.refresh()

    assert img.info.size == 1024


def object_map_image(flags=("object map invalid",)):
    """`fake_image` with the features that make those flags possible."""
    return fake_image(
        features=("exclusive-lock", "object-map", "fast-diff"),
        flags=flags,
        snapname="snap1",
    )


def test_ensure_object_map_continues_at_timeout(monkeypatch):
    """A rebuild that outlives our timeout still counts as started.

    When qemu holds the exclusive lock, it performs the rebuild and keeps
    going after we stop waiting - so a timeout is the expected outcome for a
    running VM, not a failure.
    """
    img, _ = object_map_image()
    runner = Mock(side_effect=[subprocess.TimeoutExpired("foo", 10)])
    monkeypatch.setattr("subprocess.run", runner)

    assert img.ensure_object_map()

    args, kw = runner.call_args
    assert args[0] == (
        "rbd", "-c", "/etc/ceph/ceph.conf", "--name", "client.admin",
        "object-map", "rebuild", "rbd.hdd/testimg@snap1",
    )  # fmt: skip
    assert kw["timeout"] == Image.OBJECT_MAP_TIMEOUT


def test_ensure_object_map_survives_a_failed_rebuild(monkeypatch):
    """A rebuild we cannot even start must not abort the whole `ensure`.

    It is reported and retried on the next run - the VM keeps working either
    way, the map is only an optimisation.
    """
    img, _ = object_map_image()
    runner = Mock(
        side_effect=[
            subprocess.CalledProcessError(
                returncode=1, cmd="rbd", output="nope", stderr="nope"
            )
        ]
    )
    monkeypatch.setattr("subprocess.run", runner)

    assert img.ensure_object_map()


def test_ensure_object_map_rereads_the_flags_after_a_rebuild(monkeypatch):
    """A rebuild invalidates what we know about the image.

    `VolumeSpecification.ensure_object_map` runs on every `ensure()`, so a
    cached "object map invalid" would make us kick off the rebuild again and
    again for as long as this object lives.
    """
    img, reported = object_map_image()
    monkeypatch.setattr("subprocess.run", Mock())

    assert img.ensure_object_map() is True
    reads = img.ioctx.rados.rbd_.call_count

    # The cluster has finished the rebuild in the meantime.
    reported["flags"] = []

    assert img.ensure_object_map() is False
    assert img.ioctx.rados.rbd_.call_count > reads, "flags came from the cache"


def test_ensure_object_map_skips_a_valid_map(monkeypatch):
    """The flag is the only trigger: no flag, no (expensive) rebuild."""
    img, _ = object_map_image(flags=("unrelated flag",))
    runner = Mock(side_effect=AssertionError("must not rebuild"))
    monkeypatch.setattr("subprocess.run", runner)

    assert img.ensure_object_map() is False
    assert not runner.called
