import logging
import rados
import rbd
import subprocess

logger = logging.getLogger(__name__)

# Those settings are overwritten by the system config from /etc/qemu/fc-
# agent.conf. The default values in main() support testing.

CEPH_CLUSTER = None
CEPH_LOCK_HOST = None
CEPH_CLIENT = 'admin'


def cmd(cmdline):
    print cmdline
    subprocess.check_call(cmdline, shell=True)


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

    def exists(self):
        return self.name in self.rbd.list(self.ioctx)

    def ensure_presence(self):
        if self.name in self.rbd.list(self.ioctx):
            return
        self.rbd.create(self.ioctx, self.name, 1024)

    def ensure_size(self, size):
        if self.image.size() == size:
            return
        self.image.resize(size)

    def lock(self):
        retry = 3
        while retry:
            try:
                self.image.lock_exclusive(CEPH_LOCK_HOST)
            except (rbd.ImageBusy, rbd.ImageExists):
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
                raise
        raise rbd.ImageBusy(
            'Could not acquire lock - tried multiple times. '
            'Someone seems to be racing me.')

    def lock_status(self):
        """Return None if not locked and the lock_id if it is."""
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

    def unlock(self):
        locked_by = self.lock_status()
        if not locked_by:
            return
        client_id, lock_id = locked_by
        if lock_id != CEPH_LOCK_HOST:
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
            cmd('mkfs -q -m 1 -t ext4 "/dev/rbd/{}"'.format(self.fullname))
            cmd('tune2fs -e remount-ro "/dev/rbd/{}"'.format(self.fullname))
        finally:
            self.unmap()

    def map(self):
        if self.mapped:
            return
        cmd('rbd --id "{}" map {}'.format(CEPH_CLIENT, self.fullname))
        self.mapped = True

    def unmap(self):
        if not self.mapped:
            return
        cmd('rbd --id "{}" unmap /dev/rbd/{}'.format(CEPH_CLIENT,
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
        self.ioctx = self.rados.open_ioctx(self.cfg['resource_group'])

        self.root = Volume(self.ioctx, self.cfg['name'] + '.root')
        self.swap = Volume(self.ioctx, self.cfg['name'] + '.swap')
        self.tmp = Volume(self.ioctx, self.cfg['name'] + '.tmp')

        self.volumes = [self.root, self.swap, self.tmp]

    def __exit__(self, exc_value, exc_type, exc_tb):
        self.ioctx.close()
        self.rados.shutdown()

    def start(self):
        self.ensure_root_volume()
        self.ensure_tmp_volume()
        self.ensure_swap_volume()

    def ensure_root_volume(self):
        if not self.root.exists():
            cmd('create-vm {}'.format(self.cfg['name']))
        self.root.lock()

    def ensure_swap_volume(self):
        self.swap.ensure_presence()
        self.swap.lock()
        self.swap.ensure_size(self.cfg['swap_size'])
        self.swap.mkswap()

    def ensure_tmp_volume(self):
        self.tmp.ensure_presence()
        self.tmp.lock()
        self.tmp.ensure_size(self.cfg['tmp_size'])
        self.tmp.mkfs()

    def locks(self):
        for vol in self.volumes:
            status = vol.lock_status()
            if not status:
                continue
            yield vol.name, status[1]

    def stop(self):
        for vol in self.volumes:
            vol.unlock()
