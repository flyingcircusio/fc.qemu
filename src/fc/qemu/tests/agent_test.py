from ..agent import Agent
import mock
import os
import pkg_resources
import psutil
import pytest
import shutil


@pytest.yield_fixture
def simplevm_cfg():
    fixtures = pkg_resources.resource_filename(__name__, 'fixtures')
    shutil.copy(fixtures + '/simplevm.yaml', '/etc/qemu/vm/simplevm.cfg')
    yield 'simplevm'
    os.unlink('/etc/qemu/vm/simplevm.cfg')


def test_builtin_config_template(simplevm_cfg):
    a = Agent(simplevm_cfg)
    a.generate_config()
    # machine type must match Qemu version in virtualbox
    assert 'type = "pc-i440fx-2.7"' in a.qemu.config


def test_userdefined_config_template(simplevm_cfg):
    with open('/etc/qemu/qemu.vm.cfg.in', 'w') as f:
        f.write('# user defined config template\n')
    try:
        a = Agent(simplevm_cfg)
        a.generate_config()
        assert 'user defined config template' in a.qemu.config
    finally:
        os.unlink('/etc/qemu/qemu.vm.cfg.in')


def test_consistency_vm_running(simplevm_cfg):
    a = Agent(simplevm_cfg)
    a.qemu.is_running = mock.Mock(return_value=True)
    a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
    a.ceph.locked_by_me = mock.Mock(return_value=True)
    assert a.state_is_consistent() is True


def test_consistency_vm_not_running(simplevm_cfg):
    a = Agent(simplevm_cfg)
    a.qemu.is_running = mock.Mock(return_value=False)
    a.qemu.proc = mock.Mock(return_value=None)
    a.ceph.locked_by_me = mock.Mock(return_value=False)
    assert a.state_is_consistent() is True


def test_consistency_process_dead(simplevm_cfg):
    a = Agent(simplevm_cfg)
    a.qemu.is_running = mock.Mock(return_value=True)
    a.qemu.proc = mock.Mock(return_value=None)
    a.ceph.locked_by_me = mock.Mock(return_value=True)
    assert a.state_is_consistent() is False


def test_consistency_pidfile_missing(simplevm_cfg):
    a = Agent(simplevm_cfg)
    a.qemu.is_running = mock.Mock(return_value=True)
    a.qemu.proc = mock.Mock(return_value=None)
    a.ceph.locked_by_me = mock.Mock(return_value=True)
    assert a.state_is_consistent() is False


def test_consistency_ceph_lock_missing(simplevm_cfg):
    a = Agent(simplevm_cfg)
    a.qemu.is_running = mock.Mock(return_value=True)
    a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
    a.ceph.locked_by_me = mock.Mock(return_value=False)
    assert a.state_is_consistent() is False


def test_ensure_resize(simplevm_cfg):
    a = Agent(simplevm_cfg)
    a.qemu.is_running = mock.Mock(return_value=True)
    a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
    a.ceph.locked_by_me = mock.Mock(return_value=False)
    assert a.state_is_consistent() is False
