from .lock import Locks
import logging
import rados
import rbd

logger = logging.getLogger(__name__)

# Those settings are overwritten by the system config from /etc/qemu/fc-
# agent.conf. The default values in main() support testing.

CEPH_CLUSTER = None
CEPH_LOCK_HOST = None
CEPH_CLIENT = 'admin'


class Ceph(object):

    def __init__(self, cfg):
        self.cfg = cfg
        self.ceph_conf = '/etc/ceph/{}.conf'.format(CEPH_CLUSTER)

    def __enter__(self):
        self.rados = rados.Rados(
            conffile=self.ceph_conf,
            name='client.'+CEPH_CLIENT)
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
            print "Releasing lock for", image
            self.release_lock(image)
        self.query_locks()

    def image_names(self):
        prefix = self.cfg['name'] + '.'
        r = rbd.RBD()
        for img in r.list(self.ioctx):
            if img.startswith(prefix) and '@' not in img:
                yield img

    def images(self):
        for name in self.image_names():
            with rbd.Image(self.ioctx, name) as i:
                yield name, i

    # Lock management

    def query_locks(self):
        """Collect all locks for all images of this VM.

        list_lockers returns:
            [{'lockers': [(client_id, lock_id, address), ...],
              'exclusive': bool,
              'tag': str}, ...]
        """
        self.locks = Locks()
        for name, img in self.images():
            self.locks.add(name, img.list_lockers())

    def acquire_lock(self, image_name):
        if image_name in self.locks.held:
            return
        with rbd.Image(self.ioctx, image_name) as img:
            try:
                img.lock_exclusive(CEPH_LOCK_HOST)
            except rbd.ImageBusy:
                raise Exception('failed to acquire lock', image_name)
            except rbd.ImageExists:
                # we hold the lock already
                pass
            finally:
                self.query_locks()

    def release_lock(self, image_name, force=False):
        """Release lock.
        """
        lock = self.locks.available[image_name]
        if not lock.mine and not force:
            raise Exception('refusing to release lock held by another host',
                            lock)
        with rbd.Image(self.ioctx, image_name) as img:
            try:
                # We cannot unlock as the client names aren't predictable
                # and we verify that we have the right to break the lock
                # anyway.
                img.break_lock(lock.client_id, lock.lock_id)
            except rbd.ImageNotFound:
                # lock has already been released
                pass

    def assert_locks(self):
        self.query_locks()
        for image in self.image_names():
            if image not in self.locks.held:
                raise RuntimeError(
                    "I don't own all locks for {}".format(self.name),
                    self.locks.held.keys(), self.locks.available.keys())
