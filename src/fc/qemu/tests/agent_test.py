import os
import shutil

import mock
import pkg_resources
import psutil
import pytest

from ..agent import Agent
from ..exc import VMStateInconsistent


@pytest.yield_fixture
def simplevm_cfg(tmpdir):
    fixtures = pkg_resources.resource_filename(__name__, "fixtures")
    shutil.copy(
        fixtures + "/simplevm.yaml", str(tmpdir / "/etc/qemu/vm/simplevm.cfg")
    )
    yield "simplevm"


@pytest.mark.live
def test_builtin_config_template(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.generate_config()
    # machine type must match Qemu version in virtualbox
    assert 'type = "pc-i440fx-4.1"' in a.qemu.config


@pytest.mark.live
def test_userdefined_config_template(tmpdir, simplevm_cfg):
    with open(str(tmpdir / "/etc/qemu/qemu.vm.cfg.in"), "w") as f:
        f.write("# user defined config template\n")
    a = Agent(simplevm_cfg)
    with a:
        a.generate_config()
    assert "user defined config template" in a.qemu.config


@pytest.mark.live
def test_consistency_vm_running(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
        a.ceph.locked_by_me = mock.Mock(return_value=True)
        a.raise_if_inconsistent()


@pytest.mark.live
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
def test_consistency_pidfile_missing(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=None)
        a.ceph.locked_by_me = mock.Mock(return_value=True)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()


@pytest.mark.live
def test_consistency_ceph_lock_missing(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
        a.ceph.locked_by_me = mock.Mock(return_value=False)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()


@pytest.mark.live
def test_ensure_resize(simplevm_cfg):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
        a.ceph.locked_by_me = mock.Mock(return_value=False)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()
