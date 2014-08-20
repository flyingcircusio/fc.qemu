from .config import CEPH_ID
import collections
import hashlib
import logging


class Lock(collections.namedtuple('Lock', ['image', 'locker_id', 'host'])):

    @property
    def mine(self):
        return self.host == CEPH_ID


class Locks(object):
    """Container object for a collection of locks.

    Note: Locks that are held by nobody do not appear in available. It just
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
        locker_id, host, addr = lockers['lockers'][0]
        lock = Lock(image_name, locker_id, host)
        self.available[image_name] = lock
        if lock.mine:
            self.held[image_name] = lock

    def acquired(self, image_name):
        self.held[image_name] = self.available[image_name]

    def released(self, image_name):
        if image_name not in self.available:
            raise KeyError('unknown lock', image_name)
        if image_name in self.held:
            del self.held[image_name]

    def auth_cookie(self):
        c = hashlib.sha1()
        for l in sorted(self.available.values()):
            c.update(l.locker_id + '\0' + l.host + '\0')
        return c.hexdigest()
