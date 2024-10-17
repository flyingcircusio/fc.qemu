import os
import shutil
from pathlib import Path

import mock
import pkg_resources
import psutil
import pytest

from fc.qemu.agent import Agent
from fc.qemu.exc import VMStateInconsistent
from fc.qemu.hazmat.qemu import Qemu, detect_current_machine_type


@pytest.fixture
def simplevm_cfg(monkeypatch):
    fixtures = Path(pkg_resources.resource_filename(__name__, "fixtures"))
    source = fixtures / "simplevm.yaml"
    # The Qemu prefix gets adjusted automatically in the synhetic_root
    # auto-use fixture that checks whether this is a live test or not.
    dest = Qemu.prefix / "etc/qemu/vm/simplevm.cfg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, dest)
    yield "simplevm"
    a = Agent("simplevm")
    a.system_config_template.unlink(missing_ok=True)


@pytest.mark.live
def test_builtin_config_template(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a:
        a.ceph.start()
        a.generate_config()
    # machine type must match Qemu version
    current_machine_type = detect_current_machine_type(a.machine_type)
    assert current_machine_type.count("-") == 2
    assert f'type = "{current_machine_type}"' in a.qemu.config


@pytest.mark.live
def test_userdefined_config_template(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a.system_config_template.open("w") as f:
        f.write("# user defined config template\n")
    with a:
        a.ceph.start()
        a.generate_config()
    assert "user defined config template" in a.qemu.config


def test_consistency_vm_running(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
        a.ceph.locked_by_me = mock.Mock(return_value=True)
        a.raise_if_inconsistent()


def test_consistency_vm_not_running(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=False)
        a.qemu.proc = mock.Mock(return_value=None)
        a.ceph.locked_by_me = mock.Mock(return_value=False)
        a.raise_if_inconsistent()


def test_consistency_process_dead(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=None)
        a.ceph.locked_by_me = mock.Mock(return_value=True)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()


def test_consistency_pid_file_missing(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=None)
        a.ceph.locked_by_me = mock.Mock(return_value=True)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()


def test_consistency_ceph_lock_missing(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
        a.ceph.locked_by_me = mock.Mock(return_value=False)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()


def test_ensure_inconsistent_state_detected(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a:
        a.qemu.is_running = mock.Mock(return_value=True)
        a.qemu.proc = mock.Mock(return_value=psutil.Process(1))
        a.ceph.locked_by_me = mock.Mock(return_value=False)
        with pytest.raises(VMStateInconsistent):
            a.raise_if_inconsistent()


@pytest.mark.timeout(60)
@pytest.mark.live
def test_maintenance():
    with pytest.raises(SystemExit, match="0"):
        Agent.maintenance_enter()
    Agent.maintenance_leave()
