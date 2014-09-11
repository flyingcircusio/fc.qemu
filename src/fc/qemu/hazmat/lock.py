import fc.qemu.hazmat.ceph
import collections
import hashlib


class Lock(collections.namedtuple('Lock', ['image', 'client_id', 'lock_id'])):

    @property
    def mine(self):
        return self.lock_id == fc.qemu.hazmat.ceph.CEPH_LOCK_HOST


class Locks(object):
    """Container object for a collection of locks.

    Note: Locks that are held by nobody do not appear in `available`. It just
    means that *someone* locked it.

    """

    def __init__(self):
        self.available = {}
        self.held = {}

    def add(self, image_name, lockers):
        if not lockers:
            return
        if not len(lockers['lockers']) == 1:
            raise NotImplementedError("I'm not prepared for shared locks")
        client_id, lock_id, addr = lockers['lockers'][0]
        lock = Lock(image_name, client_id, lock_id)
        self.available[image_name] = lock
        if lock.mine:
            self.held[image_name] = lock

    def auth_cookie(self):
        c = hashlib.sha1()
        for l in sorted(self.available.values()):
            c.update(l.locker_id + '\0' + l.lock_id + '\0')
        return c.hexdigest()
