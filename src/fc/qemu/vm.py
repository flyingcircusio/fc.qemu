from .config import Config, CEPH_ID, CEPH_CLUSTER
from .exc import LockError, DestructionError
from .lock import Locks
from .monitor import Monitor
from .timeout import TimeOut
import logging
import rados
import rbd
import subprocess
import time

_log = logging.getLogger(__name__)


class VM(object):

    CONFD_FILE = '/etc/conf.d/kvm.{}'
    INITD_FILE = '/etc/init.d/kvm.{}'

    def __init__(self, name):
        self.name = name
        config = Config.from_file(self.CONFD_FILE.format(name))
        self.rg = config.rg
        self.port = config.monitor_port
        self.locks = None
        self.cookie = None
        self.rados = None
        self.ioctx = None
        self.monitor = Monitor(self.port)

    def __enter__(self):
        self.rados = rados.Rados(
            conffile='/etc/ceph/{}.conf'.format(CEPH_CLUSTER),
            name='client.{}'.format(CEPH_ID))
        self.rados.connect()
        self.ioctx = self.rados.open_ioctx(self.rg)
        self.query_locks()
        self.cookie = self.locks.auth_cookie()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        if _exc_type is None:
            self.migration_errorcode = 0
        else:
            self.migration_errorcode = 1
            try:
                _log.exception('A problem occured trying to migrate the VM. '
                               'Trying to rescue it.',
                               exc_info=(_exc_type, _exc_value, _traceback))
                self.rescue()
            except:
                # Purposeful bare except: try really hard to kill
                # our VM.
                _log.exception('A problem occured trying to rescue the VM '
                               'after a migration failure. Destroying it.')
                self.destroy()

        self.ioctx.close()
        self.rados.shutdown()

    def image_names(self):
        prefix = self.name + '.'
        r = rbd.RBD()
        for img in r.list(self.ioctx):
            if img.startswith(prefix) and not '@' in img:
                yield img

    def images(self):
        for name in self.image_names():
            with rbd.Image(self.ioctx, name) as i:
                yield name, i

    def query_locks(self):
        """Collect all locks for all images of this VM.

        list_lockers returns:
            [{'lockers': [(locker_id, host, address), ...],
              'exclusive': bool,
              'tag': str}, ...]
        """
        self.locks = Locks()
        for name, img in self.images():
            self.locks.add(name, img.list_lockers())

    def acquire_lock(self, image_name):
        with rbd.Image(self.ioctx, image_name) as img:
            try:
                img.lock_exclusive(CEPH_ID)
                self.locks.acquired(image_name)
            except rbd.ImageBusy:
                raise LockError('failed to acquire lock', image_name)
            except rbd.ImageExists:
                # we hold the lock already
                pass

    def release_lock(self, image_name):
        """Release lock.

        Make sure that vm.locks.available is up to date before calling
        this method.
        """
        lock = self.locks.available[image_name]
        if lock.host != CEPH_ID:
            raise LockError('refusing to release lock held by another host',
                            lock)
        with rbd.Image(self.ioctx, image_name) as img:
            try:
                img.break_lock(lock.locker_id, CEPH_ID)
                self.locks.released(image_name)
            except rbd.ImageNotFound:
                # lock has already been released
                pass

    def assert_locks(self):
        if not self.locks.held == self.locks.available:
            raise RuntimeError(
                "I don't own all locks for {}".format(self.name),
                self.locks.held.keys(), self.locks.available.keys())

    def rescue(self):
        """Recover from potentially inconsistent state.

        If the VM is running and we own all locks, then everything is fine.

        If the VM is running and we do not own the locks, then try to acquire
        them or bail out.

        Returns True if we were able to rescue the VM.
        Returns False if the rescue attempt failed and the VM is stopped now.

        """
        _log.warning('trying to recover')
        self.monitor.assert_status('VM status: running')

        for image in set(self.locks.available) - set(self.locks.held):
            try:
                self.acquire_lock(image)
            except LockError:
                pass

        self.assert_locks()

    def destroy(self):
        _log.info('destroying VM')
        # We use this destroy command in "fire-and-forget"-style because
        # sometimes the init script will complain even if we achieve what
        # we want: that the VM isn't running any longer. We check this
        # by contacting the monitor instead.
        self.initd('destroy')
        timeout = TimeOut(5, interval=1, raise_on_timeout=True)
        while timeout.tick():
            status = self.monitor.status()
            if status == '':
                break

        # We could not connect to the monitor, thus the VM is gone.
        self.query_locks()
        for image in list(self.locks.held):
            self.release_lock(image)

    def initd(self, *args):
        subprocess.check_call([
            self.INITD_FILE.format(self.name)] + list(args))
