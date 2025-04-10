import shutil
from pathlib import Path

import mock
import psutil
import pytest

import fc.qemu.util as util
from fc.qemu.agent import Agent, iproute2_json
from fc.qemu.exc import VMStateInconsistent
from fc.qemu.hazmat.qemu import Qemu, detect_current_machine_type


def named_vm_cfg(name, monkeypatch):
    fixtures = Path(__file__).parent / "fixtures"
    source = fixtures / f"{name}.yaml"
    # The Qemu prefix gets adjusted automatically in the synhetic_root
    # auto-use fixture that checks whether this is a live test or not.
    dest = Qemu.prefix / f"etc/qemu/vm/{name}.cfg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, dest)
    yield name
    a = Agent(name)
    a.system_config_template.unlink(missing_ok=True)


@pytest.fixture
def simplevm_cfg(monkeypatch):
    yield from named_vm_cfg("simplevm", monkeypatch)


@pytest.fixture
def simplepubvm_cfg(monkeypatch):
    yield from named_vm_cfg("simplepubvm", monkeypatch)


def test_builtin_config_template(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a:
        a.ceph.start()
        a.generate_config()
    # machine type must match Qemu version
    current_machine_type = detect_current_machine_type(a.machine_type)
    assert current_machine_type.count("-") == 2
    assert f'type = "{current_machine_type}"' in a.qemu.config


def test_userdefined_config_template(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a.system_config_template.open("w") as f:
        f.write("# user defined config template\n")
    with a:
        a.ceph.start()
        a.generate_config()
    assert "user defined config template" in a.qemu.config


def test_config_template_netscripts(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    with a:
        a.ceph.start()
        a.generate_config()
    assert 'script = "/etc/kvm/kvm-ifup"' in a.qemu.config
    assert 'downscript = "/etc/kvm/kvm-ifdown"' in a.qemu.config


def test_config_template_vrf_netscripts(simplepubvm_cfg, ceph_inst):
    a = Agent(simplepubvm_cfg)
    with a:
        a.ceph.start()
        a.generate_config()
    assert 'script = "/etc/kvm/kvm-ifup-vrf"' in a.qemu.config
    assert 'downscript = "/etc/kvm/kvm-ifdown-vrf"' in a.qemu.config


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


@pytest.mark.live
def test_maintenance():
    with pytest.raises(SystemExit, match="0"):
        Agent.maintenance_enter()
    Agent.maintenance_leave()


def test_iproute2_json_loopback():
    """Basic functional test of iproute2 JSON output handling."""
    data = iproute2_json(util.log, ["address", "show", "lo"])
    assert data == [
        {
            "ifindex": 1,
            "ifname": "lo",
            "flags": ["LOOPBACK", "UP", "LOWER_UP"],
            "mtu": 65536,
            "qdisc": "noqueue",
            "operstate": "UNKNOWN",
            "group": "default",
            "txqlen": 1000,
            "link_type": "loopback",
            "address": "00:00:00:00:00:00",
            "broadcast": "00:00:00:00:00:00",
            "addr_info": [
                {
                    "family": "inet",
                    "local": "127.0.0.1",
                    "prefixlen": 8,
                    "scope": "host",
                    "label": "lo",
                    "valid_life_time": 4294967295,
                    "preferred_life_time": 4294967295,
                },
                {
                    "family": "inet6",
                    "local": "::1",
                    "prefixlen": 128,
                    "scope": "host",
                    "noprefixroute": True,
                    "valid_life_time": 4294967295,
                    "preferred_life_time": 4294967295,
                },
            ],
        }
    ]
