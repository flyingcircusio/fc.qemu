from .ceph import Ceph
import pytest


@pytest.yield_fixture
def ceph_inst():
    cfg = {'resource_group': 'test',
           'rbd_pool': 'rbd.hdd',
           'name': 'simplevm', 'disk': 10}
    ceph = Ceph(cfg)
    ceph.CREATE_VM = 'echo {name}'
    ceph.MKFS_XFS = '-q -f -K'
    ceph.__enter__()
    try:
        yield ceph
    finally:
        ceph.__exit__(None, None, None)
