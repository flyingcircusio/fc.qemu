"""Low-level handling of Ceph volumes.

This module contains the Volume, Snapshot, and Image classes. Volume
represents an RBD volume that is not a snapshot. Image is an abstract
base class for both volumes and snapshots.
"""

from ..util import remove_empty_dirs, cmd
import contextlib
import json
import logging
import os
import os.path as p
import rbd
import time

logger = logging.getLogger(__name__)


class Image(object):
    """Abstract base class for all images (volumes and snapshots)."""

    device = None
    mountpoint = None

    def __init__(self, ceph, name):
        self.ceph = ceph
        self.ioctx = ceph.ioctx
        self.name = name
        self.rbd = rbd.RBD()

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
        return self.device + '-part1'

    def map(self):
        if self.device is not None:
            return
        cmd('rbd -c "{}" --id "{}" map "{}"'.format(
            self.ceph.CEPH_CONF, self.ceph.CEPH_CLIENT, self.fullname))
        time.sleep(0.1)
        self.device = '/dev/rbd/' + self.fullname

    def unmap(self):
        if self.device is None:
            return
        cmd('rbd -c "{}" --id "{}" unmap "{}"'.format(
            self.ceph.CEPH_CONF, self.ceph.CEPH_CLIENT, self.device))
        self.device = None

    @contextlib.contextmanager
    def mapped(self):
        """Maps the image to a block device and yields the device name."""
        self.map()
        try:
            yield self.device
        finally:
            self.unmap()

    def mount(self):
        if self.mountpoint is not None:
            return
        if not self.device:
            raise RuntimeError('image must be mapped before mounting',
                               self.fullname)
        mountpoint = '/mnt/rbd/{}'.format(self.fullname)
        try:
            os.makedirs(mountpoint)
        except OSError:  # pragma: no cover
            pass
        cmd('mount "{}" "{}"'.format(self.part1dev, mountpoint))
        self.mountpoint = mountpoint

    def umount(self):
        if self.mountpoint is None:
            return
        cmd('umount "{}"'.format(self.mountpoint))
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

    def __iter__(self):
        """Iterate over all existing snapshots."""
        return (Snapshot(self.vol, s['name'], s['id'], s['size'])
                for s in self._list_snaps())

    def __len__(self):
        return len(list(self._list_snaps()))

    def __getitem__(self, key):
        for s in self._list_snaps():
            if key == s['name']:
                return Snapshot(self.vol, s['name'], s['id'], s['size'])
        raise KeyError(key)

    def _list_snaps(self):
        try:
            return self.vol.rbdimage.list_snaps()
        except rbd.ImageNotFound:
            return []

    def purge(self):
        """Remove all snapshots."""
        for snapshot in self:
            logger.info('purge: removing snapshot %s', snapshot.fullname)
            snapshot.remove()

    def create(self, snapname):
        self.vol.rbdimage.create_snap(snapname)
        logger.info('created snapshot %s@%s', self.vol, snapname)


class Snapshot(Image):
    """Single snapshot of a Volume."""

    def __init__(self, volume, snapname, id, snapsize):
        super(Snapshot, self).__init__(volume.ceph, volume.name)
        self.vol = volume
        self.snapname = snapname
        self.id = id
        self.size = snapsize

    @property
    def rbdimage(self):
        return rbd.Image(self.ioctx, self.name, self.snapname)

    @property
    def fullname(self):
        return self.ioctx.name + '/' + self.name + '@' + self.snapname

    def remove(self):
        """Destroy myself."""
        self.vol.rbdimage.remove_snap(self.snapname)
        logger.info('removed snapshot %s', self)


class Volume(Image):
    """RBD Volume interface.

    A volume has a name (shown in `rbd ls`) and a filesystem label. The
    latter is only used for mkfs/mkswap. A volume may have a set of
    snapshots accessible via the `snapshots` container object.
    """

    MKFS_CMD = {
        'xfs': ('mkfs.xfs {options} -L "{label}" "{device}"'),
        'ext4': ('mkfs.ext4 {options} -L "{label}" "{device}" '
                 '&& tune2fs -e remount-ro "{device}"')
    }

    def __init__(self, ceph, name, label):
        super(Volume, self).__init__(ceph, name)
        self.label = label
        self.snapshots = Snapshots(self)

    @property
    def rbdimage(self):
        return rbd.Image(self.ioctx, self.name)

    @property
    def fullname(self):
        return self.ioctx.name + '/' + self.name

    @property
    def size(self):
        """Image size in Bytes."""
        return self.rbdimage.size()

    def exists(self):
        return self.name in self.rbd.list(self.ioctx)

    def ensure_presence(self, size=1024):
        if self.name in self.rbd.list(self.ioctx):
            return
        self.rbd.create(self.ioctx, self.name, size)

    def ensure_size(self, size):
        if self.size == size:
            return
        self.rbdimage.resize(size)

    def lock(self):
        logger.debug('Assuming lock for %s', self.fullname)
        retry = 3
        while retry:
            try:
                self.rbdimage.lock_exclusive(self.ceph.CEPH_LOCK_HOST)
            except rbd.ImageExists:
                # This client and cookie already locked this. This is
                # definitely fine.
                return
            except rbd.ImageBusy:
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
                logger.error(
                    'Failed assuming lock. Giving up. '
                    'Competing lock: {}'.format(status))
                raise
        raise rbd.ImageBusy(
            'Could not acquire lock - tried multiple times. '
            'Someone seems to be racing me.')

    def lock_status(self):
        """Return None if not locked and (client_id, lock_id) if it is."""
        try:
            lockers = self.rbdimage.list_lockers()
        except rbd.ImageNotFound:
            return None
        if not lockers:
            return
        # For some reasons list_lockers may just return an empty list instead
        # of a dict. Say what?
        lockers = lockers['lockers']
        if not len(lockers) == 1:
            raise NotImplementedError("I'm not prepared for shared locks")
        client_id, lock_id, addr = lockers[0]
        return client_id, lock_id

    def unlock(self, force=False):
        logger.debug('Unlocking %s', self.fullname)
        locked_by = self.lock_status()
        if not locked_by:
            logger.debug('%s was not locked', self.fullname)
            return
        client_id, lock_id = locked_by
        if not force and lock_id != self.ceph.CEPH_LOCK_HOST:
            raise rbd.ImageBusy("Can not break lock for {} held by host {}."
                                .format(self.name, lock_id))
        logger.debug('Executing break_lock for %s', self.fullname)
        self.rbdimage.break_lock(client_id, lock_id)

    def mkswap(self):
        """Creates a swap partition. Requires the volume to be mappped."""
        assert self.device, 'volume must be mapped first'
        cmd('mkswap -f -L "{}" "{}"'.format(self.label, self.device))

    def mkfs(self, fstype='xfs', gptbios=False):
        assert self.device, 'volume must be mapped first'
        cmd('sgdisk -o "{}"'.format(self.device))
        cmd('sgdisk -a 8192 -n 1:8192:0 -c "1:{}" -t 1:8300 '
            '"{}"'.format(self.label, self.device))
        if gptbios:
            cmd('sgdisk -n 2:2048:+1M -c 2:gptbios -t 2:EF02 "{}"'.format(
                self.device))
        cmd('partprobe')
        while not p.exists(self.part1dev):  # pragma: no cover
            time.sleep(0.1)
        options = getattr(self.ceph, 'MKFS_' + fstype.upper())
        cmd(self.MKFS_CMD[fstype].format(
            options=options, device=self.part1dev, label=self.label))

    def seed_enc(self, data):
        with self.mounted() as target:
            os.chmod(target, 0o1777)
            fc_data = p.join(target, 'fc-data')
            os.mkdir(fc_data)
            os.chmod(fc_data, 0o750)
            with open(p.join(fc_data, 'enc.json'), 'w') as f:
                os.fchmod(f.fileno(), 0o640)
                json.dump(data, f)
                f.write('\n')
