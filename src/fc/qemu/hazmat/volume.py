"""Low-level handling of Ceph volumes.

This module contains the Volume, Snapshot, and Image classes. Volume
represents an RBD volume that is not a snapshot. Image is an abstract
base class for both volumes and snapshots.
"""

import contextlib
import time
from pathlib import Path
from typing import Optional

import fc.qemu.hazmat.libceph as libceph

from ..timeout import TimeOut
from ..util import cmd, remove_empty_dirs


class Image(object):
    """Abstract base class for all images (volumes and snapshots)."""

    device: Optional[Path] = None
    mountpoint: Optional[Path] = None

    _image = None

    def __init__(self, ceph, ioctx, name):
        self.ceph = ceph
        self.ioctx = ioctx
        self.name = name
        self.log = ceph.log.bind(image=self.name)
        self.rbd = libceph.RBD()

    def __str__(self):
        return self.fullname

    @property
    def rbdimage(self):  # pragma: no cover
        raise NotImplementedError

    @property
    def fullname(self):  # pragma: no cover
        raise NotImplementedError

    @property
    def part1dev(self):
        if not self.device:
            return None
        return self.device.with_name(self.device.name + "-part1")

    def wait_for_part1dev(self):
        timeout = TimeOut(5, interval=0.1, raise_on_timeout=True, log=self.log)
        while timeout.tick():
            if self.part1dev.exists():
                break

    def map(self):
        if self.device is not None:
            return
        self.cmd(f'rbd map "{self.fullname}"')
        device = Path("/dev/rbd") / self.fullname
        while not device.exists():
            time.sleep(0.1)
        self.device = device

    def unmap(self):
        if self.device is None:
            return
        self.cmd(f'rbd unmap "{self.device}"')
        self.device = None

    @contextlib.contextmanager
    def mapped(self):
        """Maps the image to a block device and yields the device name."""
        if self.device:
            # Re-entrant version - do not map/unmap
            yield self.device
            return
        # Non-reentrant version: actually do the work
        self.map()
        try:
            yield self.device
        finally:
            self.unmap()

    def mount(self):
        if self.mountpoint is not None:
            return
        if not self.device:
            raise RuntimeError(
                "image must be mapped before mounting", self.fullname
            )
        mountpoint = Path("/mnt/rbd") / self.fullname
        mountpoint.mkdir(parents=True, exist_ok=True)
        self.wait_for_part1dev()
        self.cmd(f'mount "{self.part1dev}" "{mountpoint}"')
        self.mountpoint = mountpoint

    def umount(self):
        if self.mountpoint is None:
            return
        self.cmd(f'umount "{self.mountpoint}"')
        remove_empty_dirs(self.mountpoint)
        self.mountpoint = None

    @contextlib.contextmanager
    def mounted(self):
        """Mounts the image and yields mountpoint."""
        must_unmap = False
        if not self.device:
            self.map()
            must_unmap = True
        self.mount()
        try:
            yield self.mountpoint
        finally:
            self.umount()
            if must_unmap:
                self.unmap()


class Snapshots(object):
    """Container for all snapshots of a Volume."""

    def __init__(self, volume):
        self.vol = volume
        self.log = self.vol.log

    def __iter__(self):
        """Iterate over all existing snapshots."""
        return (
            Snapshot(self.vol, s["name"], s["id"], s["size"])
            for s in self._list_snaps()
        )

    def __len__(self):
        return len(list(self._list_snaps()))

    def __getitem__(self, key):
        for s in self._list_snaps():
            if key == s["name"]:
                return Snapshot(self.vol, s["name"], s["id"], s["size"])
        raise KeyError(key)

    def _list_snaps(self):
        try:
            return self.vol.rbdimage.list_snaps()
        except libceph.ImageNotFound:
            return []

    def purge(self):
        """Remove all snapshots."""
        for snapshot in self:
            self.log.info(
                "remove-snapshot",
                volum=self.vol.fullname,
                snapshot=snapshot.name,
            )
            snapshot.remove()

    def create(self, snapname):
        self.log.info(
            "create-snapshot", volume=self.vol.fullname, snapshot=snapname
        )
        self.vol.rbdimage.create_snap(snapname)


class Snapshot(Image):
    """Single snapshot of a Volume."""

    def __init__(self, volume, snapname, id, snapsize):
        super(Snapshot, self).__init__(volume.ceph, volume.ioctx, volume.name)
        self.vol = volume
        self.snapname = snapname
        self.id = id
        self.size = snapsize
        self.log = volume.log.bind(snapshot=snapname)
        self.cmd = lambda cmdline: cmd(cmdline, self.log)

    @property
    def rbdimage(self):
        if self._image is None:
            self._image = libceph.Image(self.ioctx, self.name, self.snapname)
        return self._image

    @property
    def fullname(self):
        return self.ioctx.name + "/" + self.name + "@" + self.snapname

    def remove(self):
        """Destroy myself."""
        self.log.info("remove-snapshot")
        self.vol.rbdimage.remove_snap(self.snapname)


class Volume(Image):
    """RBD volume interface."""

    # ENC parameters which should be seeded at boot-time into the VM
    ENC_SEED_PARAMETERS = ["cpu_model", "rbd_pool"]

    def __init__(self, ceph, ioctx, name):
        super(Volume, self).__init__(ceph, ioctx, name)
        self.log = ceph.log.bind(volume=self.fullname)
        self.cmd = lambda cmdline: cmd(cmdline, log=self.log)
        self.snapshots = Snapshots(self)
        self.locked_by_me = False
        self._image = None

    @property
    def rbdimage(self):
        if not self._image:
            self._image = libceph.Image(self.ioctx, self.name)
        return self._image

    def close(self):
        if self._image:
            self._image.close()
        self.ceph._clean_volume(self)

    @property
    def fullname(self):
        return self.ioctx.name + "/" + self.name

    @property
    def size(self):
        """Image size in Bytes."""
        return self.rbdimage.size()

    def ensure_size(self, size):
        # The existing size must be considered as a minimum, because we can't
        # just reduce images that already exist and are bigger and expect them
        # to work properly.
        if self.size >= size:
            return
        self.rbdimage.resize(size)

    def lock(self):
        self.log.info("lock")
        retry = 3
        while retry:
            try:
                self.rbdimage.lock_exclusive(self.ceph.CEPH_LOCK_HOST)
                self.locked_by_me = True
                return
            except libceph.ImageExists:
                # This client and cookie already locked this. This is
                # definitely fine.
                return
            except libceph.ImageBusy:
                # Maybe the same client but different cookie. We're fine with
                # different cookies - ignore this. Must be same client, though.
                status = self.lock_status()
                if status is None:
                    # Someone had the lock but released it in between.
                    # Lets try again
                    retry -= 1
                    continue
                if status[1] == self.ceph.CEPH_LOCK_HOST:
                    # That's locked for us already. We just re-use
                    # the existing lock.
                    return
                # Its locked for some other client.
                self.log.error("assume-lock-failed", competing=status)
                raise
        raise libceph.ImageBusy(
            "Could not acquire lock - tried multiple times. "
            "Someone seems to be racing me."
        )

    def lock_status(self):
        """Return None if not locked and (client_id, lock_id) if it is."""
        try:
            lockers = self.rbdimage.list_lockers()
        except libceph.ImageNotFound:
            return None
        if not lockers:
            return None
        # For some reasons list_lockers may just return an empty list instead
        # of a dict. Say what?
        lockers = lockers["lockers"]
        if not lockers:
            return None
        if not len(lockers) == 1:
            raise NotImplementedError("I'm not prepared for shared locks")
        client_id, lock_id, addr = lockers[0]
        return client_id, lock_id

    def unlock(self, force=False):
        locked_by = self.lock_status()
        if not locked_by:
            return
        client_id, lock_id = locked_by

        if client_id == "client.?":
            self.log.warn("buggy-lock", action="continuing")

        if self.locked_by_me or lock_id == self.ceph.CEPH_LOCK_HOST:
            self.log.info("unlock")
            self.rbdimage.unlock(lock_id)
            self.locked_by_me = False
            return

        # We do not own this lock: need to explicitly ask for breaking.
        elif force:
            self.log.info("break-lock")
            self.rbdimage.unlock(lock_id)
