import rados
import rbd
import socket
from .lock import Locks
import logging


logger = logging.getLogger(__name__)

CEPH_CLUSTER = 'ceph'  # XXX
CEPH_ID = socket.gethostname()  # XXX
CEPH_CLIENT = 'client.admin'
# XXX CEPH_CLIENT='client.{}'.format(CEPH_ID)


class Ceph(object):

    def __init__(self, cfg):
        self.cfg = cfg
        self.ceph_conf = '/etc/ceph/{}.conf'.format(CEPH_CLUSTER)

    def __enter__(self):
        self.rados = rados.Rados(conffile=self.ceph_conf, name=CEPH_CLIENT)
        self.rados.connect()
        self.ioctx = self.rados.open_ioctx(self.cfg['resource_group'])
        self.query_locks()

    def __exit__(self, exc_value, exc_type, exc_tb):
        self.ioctx.close()
        self.rados.shutdown()

    def start(self):
        for image in self.image_names():
            self.acquire_lock(image)

    def stop(self):
        for image in self.image_names():
            print "Releasing lock for ", image
            self.release_lock(image)

    def image_names(self):
        prefix = self.cfg['name'] + '.'
        r = rbd.RBD()
        for img in r.list(self.ioctx):
            if img.startswith(prefix) and not '@' in img:
                yield img

    def images(self):
        for name in self.image_names():
            with rbd.Image(self.ioctx, name) as i:
                yield name, i

    # Lock management

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
            except rbd.ImageBusy:
                raise Exception('failed to acquire lock', image_name)
            except rbd.ImageExists:
                # we hold the lock already
                pass
            finally:
                self.query_locks()

    def release_lock(self, image_name):
        """Release lock.
        """
        lock = self.locks.available[image_name]
        if lock.host != CEPH_ID:
            raise Exception('refusing to release lock held by another host',
                            lock)
        with rbd.Image(self.ioctx, image_name) as img:
            try:
                img.break_lock(lock.locker_id, CEPH_ID)
            except rbd.ImageNotFound:
                # lock has already been released
                pass

    def assert_locks(self):
        self.query_locks()
        for image in self.image_names():
            if not image in self.locks.held:
                raise RuntimeError(
                    "I don't own all locks for {}".format(self.name),
                    self.locks.held.keys(), self.locks.available.keys())
