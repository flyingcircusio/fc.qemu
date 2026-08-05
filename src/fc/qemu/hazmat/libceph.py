"""
A reimplementation of the Ceph librados/librbd bindings to avoid having to
compile against them.

This helps us to be more version-neutral.

"""

import errno
import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, TypedDict

from structlog import BoundLogger

from fc.qemu import util
from fc.qemu.typing import SnapshotInfo


class LockerInfo(TypedDict):
    lockers: list[tuple[str, str, str]]


class ImageNotFound(Exception):
    pass


class ImageBusy(Exception):
    pass


class ImageExists(Exception):
    pass


class Rados:
    POOLS_CACHE: list[str] = []  # mutable on purpose as a global cache.

    def __init__(self, conffile: str, name: str, log: BoundLogger):
        self.conffile = conffile
        self.name = name
        self.log = log.bind(subsystem="libceph")
        self._ioctx: dict[str, Ioctx] = {}

    def open_ioctx(self, pool: str) -> "Ioctx":
        if pool not in self._ioctx:
            self._ioctx[pool] = Ioctx(self, pool)
        return self._ioctx[pool]

    def ceph_(self, *args: str, use_json: bool = True) -> Any:
        shargs = shlex.join(args)
        format_arg = "--format json" if use_json else ""
        result = util.cmd(
            f"ceph -c {self.conffile} --name {self.name} {format_arg} {shargs}",
            log=self.log,
            log_error_verbose=False,
        )
        if use_json:
            result = json.loads(result)
        return result

    def rbd_(self, *args: str, use_json: bool = True) -> Any:
        shargs = shlex.join(args)
        format_arg = "--format json" if use_json else ""
        result = util.cmd(
            f"rbd -c {self.conffile} --name {self.name} {format_arg} {shargs}",
            log=self.log,
            log_error_verbose=False,
        )
        if use_json:
            result = json.loads(result)
        return result

    def list_pools(self):
        # This is a hot-spot, cache it globally so this helps both for
        # multiple calls on a single instances as well as for mass operations
        # on multiple VMs. Pools are *very* slow moving and we invalidate
        # the cache by restarting the process all the time anyway.
        if not self.POOLS_CACHE:
            pools = self.ceph_("osd", "lspools")
            self.POOLS_CACHE.extend([p["poolname"] for p in pools])
        return self.POOLS_CACHE


class Ioctx:
    """Access to a pool."""

    def __init__(self, rados: Rados, name: str):
        self.rados = rados
        self.name = name

    def close(self):
        pass


class RBD:
    def create(self, ioctx: Ioctx, name: str, size: int):
        ioctx.rados.rbd_(
            "create",
            f"{ioctx.name}/{name}",
            "--size",
            f"{size}B",
            use_json=False,
        )

    def remove(self, ioctx: Ioctx, name: str):
        ioctx.rados.rbd_("rm", f"{ioctx.name}/{name}", use_json=False)


class Image:
    def __init__(self, ioctx: Ioctx, name: str, snapname: str | None = None):
        self.ioctx = ioctx
        self.rbd = RBD()
        self.name = name
        self.snapname = snapname
        self.closed = False

        self.mapped_device = None

        self._name = f"{self.ioctx.name}/{self.name}"
        if self.snapname:
            self._name += f"@{self.snapname}"

        try:
            # Not using _info because we want to check the image
            # and not the snapshot (if this is a snapshot handle)
            self.ioctx.rados.rbd_("info", f"{self.ioctx.name}/{self.name}")
        except subprocess.CalledProcessError as e:
            stdout = e.stdout.strip()
            if (
                stdout
                == f"rbd: error opening image {self.name}: (2) No such file or directory"
            ):
                raise ImageNotFound(self.name)
            raise

    def _info(self):
        assert not self.closed
        return self.ioctx.rados.rbd_("info", self._name)

    def size(self):
        assert not self.closed
        return self._info()["size"]

    def resize(self, size: int):
        assert not self.closed
        self.ioctx.rados.rbd_(
            "resize", self._name, "--size", f"{size}B", use_json=False
        )

    def lock_exclusive(self, cookie: str):
        assert not self.closed
        try:
            self.ioctx.rados.rbd_(
                "lock", "add", self._name, cookie, use_json=False
            )
        except Exception:
            for lock in self.ioctx.rados.rbd_("lock", "list", self._name):
                if lock["id"] == cookie:
                    # XXX slight issue here - can't identify whether it's an
                    # exclusive lock, but I'm going to run with it for now.
                    break
            else:
                raise ImageBusy(errno.EBUSY, "Image is busy")

    def list_lockers(self) -> LockerInfo:
        assert not self.closed
        # Emulate the librbd format
        lockers: LockerInfo = {"lockers": []}
        for locker in self.ioctx.rados.rbd_("lock", "list", self._name):
            lockers["lockers"].append(
                (locker["locker"], locker["id"], locker["address"])
            )
        return lockers

    def list_snaps(self) -> list[SnapshotInfo]:
        assert not self.closed
        assert "@" not in self._name
        return self.ioctx.rados.rbd_("snap", "list", self._name)

    def create_snap(self, snapname: str):
        assert not self.closed
        assert "@" not in self._name
        self.ioctx.rados.rbd_(
            "snap", "create", f"{self._name}@{snapname}", use_json=False
        )

    def remove_snap(self, snapname: str):
        assert not self.closed
        assert "@" not in self._name
        self.ioctx.rados.rbd_(
            "snap", "rm", f"{self._name}@{snapname}", use_json=False
        )

    def unlock(self, cookie: str):
        assert not self.closed
        # This is a tiny bit fishy - because we can't really know whether this
        # was our lock the whole "locker" handling is ... weird.
        for lock in self.ioctx.rados.rbd_("lock", "list", self._name):
            if lock["id"] == cookie:
                break
        else:
            raise ImageBusy(errno.EBUSY, "Lock cookie not found")
        self.ioctx.rados.rbd_(
            "lock", "rm", self._name, cookie, lock["locker"], use_json=False
        )

    def map(self):
        assert not self.closed
        if not self.mapped_device:
            self.ioctx.rados.rbd_("map", self._name, use_json=False)
            self.mapped_device = Path("/dev/rbd") / self._name
            while not self.mapped_device.exists():
                time.sleep(0.1)  # pragma: no cover
        return self.mapped_device

    def unmap(self):
        assert not self.closed
        if not self.mapped_device:
            return
        self.ioctx.rados.rbd_("unmap", str(self.mapped_device), use_json=False)
        self.mapped_device = None

    def close(self):
        self.closed = True
