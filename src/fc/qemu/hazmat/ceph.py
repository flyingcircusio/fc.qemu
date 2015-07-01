"""Low-level Ceph interface.

We expect Ceph Python bindings to be present in the system site packages.
"""

from __future__ import print_function
import hashlib
import logging
import rados
import rbd
import subprocess
import time

logger = logging.getLogger(__name__)

# Those settings are overwritten by the system config from /etc/qemu/fc-
# agent.conf. The default values in main() support testing.

CEPH_CLUSTER = None
CEPH_LOCK_HOST = None
CEPH_CLIENT = 'admin'
CREATE_VM = None
SHRINK_VM = None


def cmd(cmdline):
    """Execute cmdline with stdin closed to avoid questions on terminal"""
    print(cmdline)
    with open('/dev/null') as null:
        subprocess.check_call(cmdline, shell=True, stdin=null)


class Volume(object):

    mapped = False

    def __init__(self, ioctx, name):
        self.ioctx = ioctx
        self.name = name
        self.rbd = rbd.RBD()

    @property
    def fullname(self):
        return self.ioctx.name + '/' + self.name

    @property
    def image(self):
        return rbd.Image(self.ioctx, self.name)

    @property
    def size(self):
        """Image size in Bytes."""
        return self.image.size()

    def exists(self):
        return self.name in self.rbd.list(self.ioctx)

    def ensure_presence(self, size=1024):
        if self.name in self.rbd.list(self.ioctx):
            return
        self.rbd.create(self.ioctx, self.name, size)

    def ensure_size(self, size):
        if self.size == size:
            return
        self.image.resize(size)

    def lock(self):
        logger.debug('Trying to assume lock for %s', self.fullname)
        retry = 3
        while retry:
            try:
                self.image.lock_exclusive(CEPH_LOCK_HOST)
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
                if status[1] == CEPH_LOCK_HOST:
                    # That's locked for us already. We just re-use
                    # the existing lock.
                    return
                # Its locked for some other client.
                logger.error(
                    'Failed assuming lock. Giving up. Competing lock: {}'.format(status))
                raise
        raise rbd.ImageBusy(
            'Could not acquire lock - tried multiple times. '
            'Someone seems to be racing me.')

    def lock_status(self):
        """Return None if not locked and (client_id, lock_id) if it is."""
        try:
            lockers = self.image.list_lockers()
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
        locked_by = self.lock_status()
        if not locked_by:
            return
        client_id, lock_id = locked_by
        if not force and lock_id != CEPH_LOCK_HOST:
            raise rbd.ImageBusy("Can not break lock for {} held by host {}."
                                .format(self.name, lock_id))
        self.image.break_lock(client_id, lock_id)

    def mkswap(self):
        self.map()
        try:
            cmd('mkswap -f "/dev/rbd/{}"'.format(self.fullname))
        finally:
            self.unmap()

    def mkfs(self):
        self.map()
        try:
            cmd('mkfs -F -q -m 1 -t ext4 "/dev/rbd/{}"'.format(self.fullname))
            cmd('tune2fs -e remount-ro "/dev/rbd/{}"'.format(self.fullname))
        finally:
            self.unmap()

    def map(self):
        if self.mapped:
            return
        cmd('rbd --id "{}" map "{}"'.format(CEPH_CLIENT, self.fullname))
        time.sleep(0.1)
        self.mapped = True

    def unmap(self):
        if not self.mapped:
            return
        cmd('rbd --id "{}" unmap "/dev/rbd/{}"'.format(CEPH_CLIENT,
                                                       self.fullname))
        self.mapped = False


class Ceph(object):

    def __init__(self, cfg):
        self.cfg = cfg
        self.ceph_conf = '/etc/ceph/{}.conf'.format(CEPH_CLUSTER)

    def __enter__(self):
        self.rados = rados.Rados(
            conffile=self.ceph_conf,
            name='client.' + CEPH_CLIENT)
        self.rados.connect()

        pool = self.cfg['resource_group'].encode('ascii')
        self.ioctx = self.rados.open_ioctx(pool)

        volume_prefix = self.cfg['name'].encode('ascii')
        self.root = Volume(self.ioctx, volume_prefix + '.root')
        self.swap = Volume(self.ioctx, volume_prefix + '.swap')
        self.tmp = Volume(self.ioctx, volume_prefix + '.tmp')

        self.volumes = [self.root, self.swap, self.tmp]

    def __exit__(self, exc_value, exc_type, exc_tb):
        self.ioctx.close()
        self.rados.shutdown()

    def start(self):
        self.ensure_root_volume()
        self.ensure_tmp_volume()
        self.ensure_swap_volume()

    def stop(self):
        self.unlock()

    def shrink_root(self):
        # Note: we trust the called script to lock and unlock
        # the root image.
        target_size = self.cfg['disk'] * 1024**3
        if self.root.size <= target_size:
            return
        try:
            cmd(SHRINK_VM.format(image=self.root.name, **self.cfg))
        except subprocess.CalledProcessError:
            raise RuntimeError(
                'unrecoverable error while shrinking root volume',
                self.cfg['name'])

    def ensure_root_volume(self):
        if not self.root.exists():
            cmd(CREATE_VM.format(**self.cfg))
        self.shrink_root()
        self.root.lock()

    def ensure_swap_volume(self):
        self.swap.ensure_presence(self.cfg['swap_size'])
        self.swap.lock()
        self.swap.ensure_size(self.cfg['swap_size'])
        self.swap.mkswap()

    def ensure_tmp_volume(self):
        self.tmp.ensure_presence(self.cfg['tmp_size'])
        self.tmp.lock()
        self.tmp.ensure_size(self.cfg['tmp_size'])
        self.tmp.mkfs()

    def locks(self):
        for vol in self.volumes:
            status = vol.lock_status()
            if not status:
                continue
            yield vol.name, status[1]

    def is_unlocked(self):
        """Returns True if no volume is locked."""
        return all(not vol.lock_status() for vol in self.volumes)

    def locked_by_me(self):
        """Returns True if CEPH_LOCK_HOST holds locks for all volumes."""
        try:
            return all(v.lock_status()[1] == CEPH_LOCK_HOST
                       for v in self.volumes)
        except TypeError:  # status[1] not accessible
            return False

    def lock(self):
        for vol in self.volumes:
            vol.lock()

    def unlock(self):
        for vol in self.volumes:
            vol.unlock()

    def force_unlock(self):
        for vol in self.volumes:
            vol.unlock(force=True)

    def auth_cookie(self):
        """This is a cookie that can be used to validate that a party
        has access to Ceph.

        Used to authenticate migration requests.

        """
        c = hashlib.sha1()
        for vol in self.volumes:
            status = [vol.name]
            lock = vol.lock_status()
            if lock:
                status.extend(lock)
            c.update('\0'.join(status) + '\0')
        return c.hexdigest()
