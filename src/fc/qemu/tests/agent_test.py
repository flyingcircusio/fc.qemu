import os
import shutil

import mock
import pkg_resources
import psutil
import pytest

from ..agent import Agent
from ..exc import VMStateInconsistent


@pytest.fixture
def cleanup_files():
    files = []
    yield files
    for f in files:
        if os.path.exists(f):
            os.unlink(f)


@pytest.fixture
def simplevm_cfg(cleanup_files):
    fixtures = pkg_resources.resource_filename(__name__, "fixtures")
    shutil.copy(fixtures + "/simplevm.yaml", "/etc/qemu/vm/simplevm.cfg")
    cleanup_files.append("/etc/qemu/vm/simplevm.cfg")
    yield "simplevm"


@pytest.mark.live
def test_builtin_config_template(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.generate_config()
    # machine type must match Qemu version in virtualbox
    assert 'type = "pc-i440fx-4.1"' in a.qemu.config


@pytest.mark.live
def test_userdefined_config_template(simplevm_cfg, cleanup_files):
    with open("/etc/qemu/qemu.vm.cfg.in", "w") as f:
        f.write("# user defined config template\n")
    cleanup_files.append("/etc/qemu/qemu.vm.cfg.in")
    a = Agent(simplevm_cfg)
    with a:
        a.generate_config()
    assert "user defined config template" in a.qemu.config


@pytest.mark.live
@pytest.mark.timeout(60)
def test_consistency_vm_running(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
        a.ceph.locked_by_me = mock.Mock(return_value=True)
        a.raise_if_inconsistent()


@pytest.mark.live
@pytest.mark.timeout(60)
def test_consistency_vm_not_running(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=False)
        a.qemu.proc = mock.Mock(return_value=None)
        a.ceph.locked_by_me = mock.Mock(return_value=False)
        a.raise_if_inconsistent()


@pytest.mark.live
def test_consistency_process_dead(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=None)
        a.ceph.locked_by_me = mock.Mock(return_value=True)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()


@pytest.mark.live
@pytest.mark.timeout(60)
def test_consistency_pidfile_missing(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=None)
        a.ceph.locked_by_me = mock.Mock(return_value=True)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()


@pytest.mark.live
@pytest.mark.timeout(60)
def test_consistency_ceph_lock_missing(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
        a.ceph.locked_by_me = mock.Mock(return_value=False)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()


@pytest.mark.live
@pytest.mark.timeout(60)
def test_ensure_inconsistent_state_detected(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
        a.ceph.locked_by_me = mock.Mock(return_value=False)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()
