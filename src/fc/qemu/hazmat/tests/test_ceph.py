from ..ceph import Ceph, Volume
from rbd import ImageNotFound, ImageBusy
import os.path
import pytest
import subprocess


@pytest.yield_fixture
def ceph():
    cfg = {'resource_group': 'test', 'name': 'test00'}
    ceph = Ceph(cfg)
    ceph.__enter__()
    yield ceph
    ceph.__exit__(None, None, None)


@pytest.yield_fixture
def volume(ceph):
    subprocess.call('rbd rm test/othervolume', shell=True)
    volume = Volume(ceph.ioctx, 'othervolume')
    yield volume
    lock = volume.lock_status()
    if lock is not None:
        volume.image.break_lock(*lock)
    subprocess.call('rbd rm test/othervolume', shell=True)


def test_volume_presence(volume):
    assert volume.fullname == 'test/othervolume'
    assert not volume.exists()
    with pytest.raises(ImageNotFound):
        volume.image
    volume.ensure_presence()
    assert volume.image
    # Check that ensure_presence is fine with being called multiple times.
    volume.ensure_presence()


def test_volume_size(volume):
    volume.ensure_presence()
    assert volume.image
    assert volume.image.size() == 1024
    volume.ensure_size(2048)
    assert volume.image.size() == 2048
    # Call ensure multiple times to help triggering caching code paths.
    volume.ensure_size(2048)
    assert volume.image.size() == 2048


def test_volume_shared_lock_protection(volume):
    volume.ensure_presence()
    volume.image.lock_shared('localhost', 'a')
    volume.image.lock_shared('remotehost', 'a')
    with pytest.raises(NotImplementedError):
        volume.lock_status()
    lockers = volume.image.list_lockers()
    for client, cookie, _ in lockers['lockers']:
        volume.image.break_lock(client, cookie)


def test_volume_locking(volume):
    # Non-existing volumes report None as locking status but do not raise
    # an exception.
    assert not volume.exists()
    assert volume.lock_status() is None
    volume.ensure_presence()
    assert volume.lock_status() is None
    volume.lock()
    assert volume.lock_status()[1] == 'localhost'
    # We want to smoothen out that some other process has locked the same image
    # for the same tag already and assume that this is another incarnation of
    # us - for that we have our own lock.
    volume.lock()
    assert volume.lock_status()[1] == 'localhost'
    volume.unlock()
    assert volume.lock_status() is None
    # We can call unlock twice if it isn't locked.
    volume.unlock()

    volume.image.lock_exclusive('someotherhost')
    with pytest.raises(ImageBusy):
        volume.lock()
    with pytest.raises(ImageBusy):
        # Can not unlock locks that someone else holds.
        volume.unlock()


def test_volume_mkswap(volume):
    volume.ensure_presence()
    volume.ensure_size(5*1024**2)
    volume.mkswap()


def test_volume_mkfs(volume):
    volume.ensure_presence()
    volume.ensure_size(5*1024**2)
    volume.mkfs()


def test_volume_map_unmap(volume):
    volume.ensure_presence()
    volume.map()
    assert os.path.exists('/dev/rbd/test/othervolume')
    volume.map()
    assert os.path.exists('/dev/rbd/test/othervolume')
    volume.unmap()
    assert not os.path.exists('/dev/rbd/test/othervolume')
    volume.unmap()
    assert not os.path.exists('/dev/rbd/test/othervolume')
