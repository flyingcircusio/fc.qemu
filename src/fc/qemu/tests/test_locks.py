from ..lock import Locks
from ..config import CEPH_ID
import pytest


class TestLocks(object):

    @staticmethod
    def fake_locker(host, locker_id=0):
        return {
            'lockers': [('client.{}'.format(locker_id), host,
                         '172.20.4.9:0/1016808')],
            'exclusive': True, 'tag': ''
        }

    def test_add(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker('myhost'))
        assert len(l.available) == 1
        assert l.available.keys() == ['test.root']

    def test_acquired(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker('myhost'))
        assert len(l.held) == 0
        l.acquired('test.root')
        assert l.available == l.held

    def test_auto_add_own_locks(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker(CEPH_ID))
        assert l.available == l.held

    def test_shared_locks_should_fail(self):
        l = Locks()
        lockers = TestLocks.fake_locker('myhost')
        lockers['lockers'].append(('client.1234', 'otherhost',
                                   '172.20.4.9:0/1016808'))
        with pytest.raises(NotImplementedError):
            l.add('test.root', lockers)

    def test_aquired_should_be_idempotent(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker('myhost'))
        l.acquired('test.root')
        l.acquired('test.root')
        assert l.available == l.held

    def test_aquire_unknown_lock_should_fail(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker(CEPH_ID))
        with pytest.raises(KeyError):
            l.acquired('foo.swap')

    def test_released(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker(CEPH_ID))
        assert l.available == l.held
        l.released('test.root')
        assert len(l.held) == 0

    def test_released_should_be_idempotent(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker(CEPH_ID))
        l.released('test.root')
        l.released('test.root')
        assert len(l.held) == 0

    def test_release_unknown_lock_should_fail(self):
        l = Locks()
        with pytest.raises(KeyError):
            l.released('neverheardofthis')

    def test_auth_cookie(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker('myhost'))
        assert '8825a54876145869c3ccecea6844058db263032b' == l.auth_cookie()

    def test_auth_cookie_should_not_change_on_aquired(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker('myhost'))
        old_cookie = l.auth_cookie()
        l.acquired('test.root')
        assert l.auth_cookie() == old_cookie

    def test_auth_cookie_should_remain_stable_regardless_of_lock_order(self):
        l = Locks()
        l.add('test.root', TestLocks.fake_locker('myhost', 1))
        l.add('test.swap', TestLocks.fake_locker('myhost', 2))
        cookie1 = l.auth_cookie()
        l = Locks()
        l.add('test.swap', TestLocks.fake_locker('myhost', 2))
        l.add('test.root', TestLocks.fake_locker('myhost', 1))
        assert l.auth_cookie() == cookie1

