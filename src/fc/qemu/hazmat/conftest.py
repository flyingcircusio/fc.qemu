import errno
import os

import pytest
import rados
import rbd

from . import volume
from .ceph import Ceph


class RadosMock(object):
    def __init__(self, conffile, name):
        self.conffile = conffile
        self.name = name
        self._ioctx = {}
        self.__connected__ = False

    def connect(self):
        assert not self.__connected__
        self.__connected__ = True

    def open_ioctx(self, pool):
        if pool not in self._ioctx:
            self._ioctx[pool] = IoctxMock(pool)
        return self._ioctx[pool]

    def shutdown(self):
        assert self.__connected__
        self.__connected__ = False


class IoctxMock(object):
    """Mock access to a pool."""

    def __init__(self, name: str):
        # the rados implementation takes the name as a str, but later returns
        # that attribute as bytes
        self.name = name.encode("ascii")
        self.rbd_images = {}
        self._snapids = 0

    def _rbd_create(self, name, size):
        assert name not in self.rbd_images
        self.rbd_images[name] = dict(size=size, lock=None)

    def _rbd_create_snap(self, name, snapname):
        self._snapids += 1
        fullname = name + "@" + snapname
        assert fullname not in self.rbd_images
        self.rbd_images[fullname] = snap = self.rbd_images[name].copy()
        snap["lock"] = None
        snap["snapid"] = self._snapids

    def _rbd_remove(self, name):
        # XXX prohibit while snapshots exist and if locked/opened
        if name in self.rbd_images:
            del self.rbd_images[name]

    def _rbd_remove_snap(self, name, snapname):
        fullname = name + "@" + snapname
        if fullname in self.rbd_images:
            del self.rbd_images[fullname]

    def close(self):
        pass


class RBDMock(object):
    def list(self, ioctx):
        return list(ioctx.rbd_images.keys())

    def create(self, ioctx, name, size):
        ioctx._rbd_create(name, size)

    def remove(self, ioctx, name):
        ioctx._rbd_remove(name)


class ImageMock(object):
    def __init__(self, ioctx, name, snapname=None):
        self.ioctx = ioctx
        self.name = name
        self.snapname = snapname
        self.closed = False

        self._name = self.name
        if self.snapname:
            self._name += "@" + self.snapname

        if self._name not in ioctx.rbd_images:
            raise rbd.ImageNotFound(self.name)

    def size(self):
        assert not self.closed
        return self.ioctx.rbd_images[self.name]["size"]

    def resize(self, size):
        assert not self.closed
        self.ioctx.rbd_images[self.name]["size"] = size

    def lock_exclusive(self, cookie):
        assert not self.closed
        lock = self.ioctx.rbd_images[self.name]["lock"]
        if lock is None:
            self.ioctx.rbd_images[self.name]["lock"] = {
                "tag": None,
                "exclusive": True,
                "lockers": [("client.xyz", cookie, "127.0.0.1:9999")],
            }
            return
        else:
            assert lock["exclusive"]
            if not lock["lockers"][0][1] == cookie:
                raise rbd.ImageBusy(errno.EBUSY, "Image is busy")
            return
        raise RuntimeError("unsupported mock path")

    def lock_shared(self, cookie, tag):
        assert not self.closed
        lock = self.ioctx.rbd_images[self.name]["lock"]
        if lock is None:
            self.ioctx.rbd_images[self.name]["lock"] = {
                "tag": tag,
                "exclusive": False,
                "lockers": [("client.xyz", cookie, "127.0.0.1:9999")],
            }
            return
        else:
            if lock["exclusive"]:
                raise rbd.ImageBusy("already exclusively locked")
            if lock["tag"] != tag:
                raise rbd.ImageBusy("wrong tag")
            for l_client, l_cookie, l_addr in list(lock["lockers"]):
                if l_cookie != cookie:
                    lock["lockers"].append(
                        ("client.xyz", cookie, "127.0.0.1:9999")
                    )
                else:
                    raise rbd.ImageExists()
            # XXX we every only calls this from the same host so we never
            # actually get multiple lockers, just valid noops.
            return
        raise RuntimeError("unsupported mock path")

    def list_lockers(self):
        assert not self.closed
        if self.name not in self.ioctx.rbd_images:
            raise rbd.ImageNotFound(self.name)
        lock = self.ioctx.rbd_images[self.name]["lock"]
        return [] if lock is None else lock

    def list_snaps(self):
        assert not self.closed
        result = []
        for image, data in list(self.ioctx.rbd_images.items()):
            if image.startswith(self.name + "@"):
                snap = {
                    "id": data["snapid"],
                    "size": data["size"],
                    "name": image.split("@")[1],
                }
                result.append(snap)
        return result

    def create_snap(self, snapname):
        self.ioctx._rbd_create_snap(self.name, snapname)

    def remove_snap(self, snapname):
        self.ioctx._rbd_remove_snap(self.name, snapname)

    def unlock(self, cookie):
        assert not self.closed
        lock = self.ioctx.rbd_images[self.name]["lock"]
        if lock:
            assert lock["lockers"][0][1] == cookie
            self.ioctx.rbd_images[self.name]["lock"] = None

    def break_lock(self, client, cookie):
        assert not self.closed
        lock = self.ioctx.rbd_images[self.name]["lock"]
        if lock:
            assert lock["lockers"][0][0] == client
            assert lock["lockers"][0][1] == cookie
        self.ioctx.rbd_images[self.name]["lock"] = None

    def close(self):
        self.closed = True


@pytest.fixture
def ceph_mock(request, monkeypatch, tmpdir):
    is_live = request.node.get_closest_marker("live")
    if is_live is not None:
        # This is a live test. Do not mock things.
        return

    def image_map(self):
        if self.device:
            return
        self.device = str(tmpdir / self.fullname.replace("/", "-"))

        raw = self.device + ".raw"
        if not os.path.exists(raw):
            with open(raw, "wb") as f:
                f.seek(self.size)
                f.write(b"\0")
                f.close()
        os.symlink(raw, self.device)

        # create an implicit first partition as we can't really do the
        # partprobe dance.
        raw = self.part1dev + ".raw"
        with open(raw, "wb") as f:
            f.seek(self.size)
            f.write(b"\0")
            f.close()
        os.symlink(raw, self.part1dev)

    def image_unmap(self):
        if self.device is None:
            return
        os.unlink(self.device)
        os.unlink(self.part1dev)
        self.device = None

    monkeypatch.setattr(rados, "Rados", RadosMock)
    monkeypatch.setattr(rbd, "RBD", RBDMock)
    monkeypatch.setattr(rbd, "Image", ImageMock)
    monkeypatch.setattr(volume.Image, "map", image_map)
    monkeypatch.setattr(volume.Image, "unmap", image_unmap)


@pytest.fixture
def ceph_inst(ceph_mock):
    cfg = {
        "resource_group": "test",
        "rbd_pool": "rbd.hdd",
        "name": "simplevm",
        "disk": 10,
    }
    ceph = Ceph(cfg)
    ceph.CREATE_VM = "echo {name}"
    ceph.MKFS_XFS = "-q -f -K"
    ceph.__enter__()
    try:
        yield ceph
    finally:
        ceph.__exit__(None, None, None)
