from pathlib import Path
from subprocess import CalledProcessError

import pytest

from fc.qemu import util
from fc.qemu.hazmat.libceph import (
    Image,
    ImageBusy,
    ImageNotFound,
    NameResolutionError,
    Rados,
)


@pytest.mark.live
def test_rbd_basic_api(ceph_inst):
    pool = ceph_inst.ioctxs["rbd.ssd"]

    with pytest.raises(ImageNotFound):
        Image(pool, "test")

    ceph_inst.rbd.create(pool, "test", 1000)

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

    image.close()
    assert image.closed
    with pytest.raises(AssertionError):
        image.size()


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


ceph_lock_name_resolution_error = """\
server name not found: cartman07.sto.whq.ipv4.gocept.net (Temporary failure in name resolution)
unable to parse addrs in 'cartman07.sto.whq.ipv4.gocept.net,cartman08.sto.whq.ipv4.gocept.net,cartman10.sto.whq.ipv4.gocept.net,cartman11.sto.whq.ipv4.gocept.net'
rbd: couldn't connect to the cluster!
2025-06-06 01:16:55.074 7f71a9aff080 -1 monclient: get_monmap_and_config cannot identify monitors to contact"""


def test_name_resolution_errors_converted():
    call_error_name_resolution = CalledProcessError(
        22, "rbd lock rbd.ssd/test.root", output=ceph_lock_name_resolution_error
    )
    with pytest.raises(NameResolutionError):
        Rados._check_cmd_error(call_error_name_resolution)

    call_error_other = CalledProcessError(
        100, "rbd lock rbd.ssd/test.root", output="Some other error occured"
    )
    with pytest.raises(CalledProcessError):
        Rados._check_cmd_error(call_error_other)
