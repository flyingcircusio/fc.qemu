from pathlib import Path

import pytest

from fc.qemu.hazmat.libceph import Image, ImageBusy


@pytest.mark.live
def test_rbd_basic_api(ceph_inst):
    pool = ceph_inst.ioctxs["rbd.ssd"]
    ceph_inst.rbd.create(pool, "test", 1000)
    assert ["test"] == ceph_inst.rbd.list(pool)

    image = Image(pool, "test")
    assert image._info()["name"] == "test"

    assert image.size() == 1000
    image.resize(2000)
    assert image.size() == 2000

    assert image.list_lockers()["lockers"] == []
    image.lock_exclusive("test")
    assert image.list_lockers()["lockers"][0][1] == "test"
    # allow double locking
    image.lock_exclusive("test")
    assert image.list_lockers()["lockers"][0][1] == "test"

    with pytest.raises(ImageBusy):
        image.lock_exclusive("foobar")
    assert image.list_lockers()["lockers"][0][1] == "test"

    image.unlock("test")
    assert image.list_lockers()["lockers"] == []

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
    assert [] == ceph_inst.rbd.list(pool)

    image.close()
    assert image.closed
    with pytest.raises(AssertionError):
        image.size()
