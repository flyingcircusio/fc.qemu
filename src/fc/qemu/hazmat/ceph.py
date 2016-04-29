"""High-level handling of Ceph volumes.

We expect Ceph Python bindings to be present in the system site packages.
"""

from ..sysconfig import sysconfig
from ..util import cmd
from .volume import Volume
import hashlib
import logging
import rados
import rbd

logger = logging.getLogger(__name__)


class Ceph(object):

    # Attributes on this class can be overriden in a controlled fashion
    # from the sysconfig module. See __init__(). The defaults are here to
    # support testing.

    CREATE_VM = None

    def __init__(self, cfg):
        # Update configuration values from system or test config.
        self.__dict__.update(sysconfig.ceph)

        self.cfg = cfg
        self.rados = None
        self.ioctx = None
        self.root = None
        self.swap = None
        self.tmp = None
        self.volumes = []

    def __enter__(self):
        # Not sure whether it makes sense that we configure the client ID
        # without 'client.': qemu doesn't want to see this, whereas the
        # Rados binding does ... :/
        self.rados = rados.Rados(
            conffile=self.CEPH_CONF,
            name='client.' + self.CEPH_CLIENT)
        self.rados.connect()

        pool = self.cfg['rbd_pool'].encode('ascii')
        self.ioctx = self.rados.open_ioctx(pool)

        volume_prefix = self.cfg['name'].encode('ascii')
        self.root = Volume(self, volume_prefix + '.root', 'root')
        self.swap = Volume(self, volume_prefix + '.swap', 'swap')
        self.tmp = Volume(self, volume_prefix + '.tmp', 'tmp')

        self.volumes = [self.root, self.swap, self.tmp]

    def __exit__(self, exc_value, exc_type, exc_tb):
        self.ioctx.close()
        self.rados.shutdown()

    def start(self, enc_data=None):
        self.ensure_root_volume()
        self.ensure_tmp_volume(enc_data)
        self.ensure_swap_volume()

    def stop(self):
        self.unlock()

    def ensure_root_volume(self):
        if not self.root.exists():
            cmd(self.CREATE_VM.format(**self.cfg))
        self.root.lock()

    def ensure_swap_volume(self):
        self.swap.ensure_presence(self.cfg['swap_size'])
        self.swap.lock()
        self.swap.ensure_size(self.cfg['swap_size'])
        with self.swap.mapped():
            self.swap.mkswap()

    def ensure_tmp_volume(self, enc_data):
        self.tmp.ensure_presence(self.cfg['tmp_size'])
        self.tmp.lock()
        self.tmp.ensure_size(self.cfg['tmp_size'])
        with self.tmp.mapped():
            self.tmp.mkfs()
            logger.debug('%s: seeding ENC data', self.tmp.name)
            self.tmp.seed_enc(enc_data)

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
            return all(v.lock_status()[1] == self.CEPH_LOCK_HOST
                       for v in self.volumes)
        except TypeError:  # status[1] not accessible
            return False

    def lock(self):
        for vol in self.volumes:
            vol.lock()

    def unlock(self):
        """Remove all of *our* volume locks.

        This leaves other hosts' locks in place.
        """
        for vol in self.volumes:
            try:
                vol.unlock()
            except (rbd.ImageBusy, rbd.ConnectionShutdown):
                pass

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
