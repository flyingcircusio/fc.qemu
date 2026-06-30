import os
import shutil
from pathlib import Path

import mock
import psutil
import pytest

import fc.qemu.util as util
from fc.qemu.agent import Agent, iproute2_json
from fc.qemu.exc import EnvironmentChanged, VMStateInconsistent
from fc.qemu.hazmat.qemu import (
    Qemu,
    detect_current_machine_type,
    get_running_qemu_processes,
)


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
def simplevm_linktype_cfg(monkeypatch):
    yield from named_vm_cfg("simplevmlinktype", monkeypatch)


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


def test_config_template_netscripts_linktype(simplevm_linktype_cfg, ceph_inst):
    a = Agent(simplevm_linktype_cfg)
    with a:
        a.ceph.start()
        a.generate_config()
    assert 'script = "/etc/kvm/kvm-ifup-vrf"' in a.qemu.config
    assert 'downscript = "/etc/kvm/kvm-ifdown-vrf"' in a.qemu.config


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


def test_ensure_environment_changed(simplevm_cfg, ceph_inst):
    a = Agent(simplevm_cfg)
    a.ensure_ = mock.Mock(side_effect=[EnvironmentChanged(), None])
    with a:
        a.ensure()

    from tests.conftest import get_log

    assert (
        get_log()
        == """\
check-staging-config machine=simplevm result=none
running-ensure generation=0 machine=simplevm
check-staging-config machine=simplevm result=none
running-ensure generation=0 machine=simplevm
check-staging-config machine=simplevm result=none
changes-settled machine=simplevm"""
    )


@pytest.mark.live
def test_maintenance():
    with pytest.raises(SystemExit, match="0"):
        Agent.maintenance_enter()
    Agent.maintenance_leave()


@pytest.mark.live
def test_live_count_running_qemu_processes(vm, vm_with_pub):
    ####
    # WARNING!
    # If this test ever fails with new platform or qemu versions, keep in mind
    # the migration scenarios of old VM processes when adjusting the process
    # match heuristics! Still need to match old process patterns!
    ###

    # Each running VM has a real qemu process plus a python
    # `supervised-qemu` parent; the helper must count only the qemu
    # processes regardless of whether qemu rewrites argv[0] to its
    # absolute path (qemu 6.x) or leaves it as the bare basename
    # (qemu 10.x). PL-130990, PL-134257.
    assert len(get_running_qemu_processes()) == 0
    vm.start()
    assert len(get_running_qemu_processes()) == 1
    vm_with_pub.start()
    assert len(get_running_qemu_processes()) == 2
    vm_with_pub.stop()
    assert len(get_running_qemu_processes()) == 1
    vm.stop()
    assert len(get_running_qemu_processes()) == 0


def test_ensure_lock_contention_returns_ex_tempfail(
    simplevm_cfg, ceph_inst, monkeypatch
):
    """Test that fc-qemu ensure returns EX_TEMPFAIL (75) when lock is held.

    This is an end-to-end test from main.py:main() to verify that when
    another process holds the VM lock, the ensure command exits with
    EX_TEMPFAIL, allowing supervisor or other retry mechanisms to function.
    """
    import fcntl
    import sys

    import fc.qemu.logging
    import fc.qemu.main
    from tests.conftest import get_log

    # Mock system-level functions that aren't available in test environment
    monkeypatch.setattr("fc.qemu.main.ensure_separate_cgroup", lambda: None)

    # Mock init_logging to use test's log file instead of /var/log/fc-qemu.log
    def mock_init_logging(verbose, console_target=sys.stdout):
        # Keep test's structlog configuration - don't reconfigure
        pass

    monkeypatch.setattr("fc.qemu.logging.init_logging", mock_init_logging)

    # Configure logging to show the main-exit event
    util.test_log_options["show_events"] = ["exit"]

    # Create agent and determine lock file path
    agent = Agent(simplevm_cfg)
    lock_file = agent.lock_file

    # Ensure lock file exists
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    if not lock_file.exists():
        lock_file.touch()

    # Acquire the lock in non-blocking mode to simulate another process holding it
    lock_fd = os.open(lock_file, os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        # Mock sys.argv to simulate: fc-qemu ensure simplevm
        monkeypatch.setattr(sys, "argv", ["fc-qemu", "ensure", simplevm_cfg])

        # Call main() and expect it to exit with EX_TEMPFAIL
        with pytest.raises(SystemExit) as exc_info:
            fc.qemu.main.main()

        # Verify the exit code is EX_TEMPFAIL (75)
        assert exc_info.value.code == os.EX_TEMPFAIL, (
            f"Expected exit code {os.EX_TEMPFAIL} (EX_TEMPFAIL), "
            f"but got {exc_info.value.code}"
        )

        # Verify that main-exit log message was emitted with correct exit code
        log_output = get_log()
        assert "exit" in log_output, (
            f"Expected 'exit' log message to be emitted, but got: {log_output}"
        )
        assert "status=75" in log_output, (
            f"Expected 'exitcode=75' in log message, but got: {log_output}"
        )

    finally:
        # Release the lock
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    # second test, piggybacking here due to the huge mock setup that I don't want to refactor right now.

    def maintenance_enter_weird_sysexit():
        sys.exit(42)

    monkeypatch.setattr(
        Agent, "maintenance_enter", maintenance_enter_weird_sysexit
    )

    # Call main() and expect it to exit with the specified "weird" exit code
    monkeypatch.setattr(sys, "argv", ["fc-qemu", "maintenance", "enter"])

    with pytest.raises(SystemExit) as exc_info:
        fc.qemu.main.main()

    assert exc_info.value.code == 42

    # Verify that main-exit log message was emitted with correct exit code
    log_output = get_log()  # only returns logs since previous invocation
    assert "exit" in log_output
    assert "status=42" in log_output


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


def test_id_to_name(capsys, simplevm_cfg, simplepubvm_cfg):
    assert Agent.id_to_name("2345") == 0
    e = capsys.readouterr()
    assert e.out.strip() == "simplevm"

    assert Agent.id_to_name("3456") == 0
    e = capsys.readouterr()
    assert e.out.strip() == "simplepubvm"

    assert Agent.id_to_name("9999") == 1
    e = capsys.readouterr()
    assert e.out.strip() == ""
    assert e.err.strip() == "No VM with id 9999 known."
