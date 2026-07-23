from unittest.mock import Mock

import pytest

from fc.qemu.hazmat.qemu import (
    Qemu,
    get_running_qemu_processes,
    is_qemu_proc,
)


def test_write_file_expects_bytes(guest_agent):
    qemu = Qemu({"name": "vm00", "id": 2345})
    qemu.guestagent = guest_agent
    with pytest.raises(TypeError):
        qemu.write_file("/tmp/foo", '"asdf"')


def test_write_file_no_error(guest_agent):
    # We do't have access to a real guest agent here
    # but we saw errors even encoding the data to the socket.
    qemu = Qemu({"name": "vm00", "id": 2345})
    # the emulated answers of the guest agent:

    guest_agent._client_stub.responses = [
        # sync ID, hard-coded in fixture
        '{"return": 87643}',
        # emulated non-empty result of executions:
        # guest-file-open
        '{"return": "file-handle-1"}',
        # guest-file-write
        '{"return": "qwer"}',
        # guest-file-close
        '{"return": "zuio"}',
    ]

    qemu.guestagent = guest_agent

    qemu.write_file("/tmp/foo", b'"asdf"')
    print(guest_agent.client.messages_sent)

    assert guest_agent.client.messages_sent == [
        b'{"execute": "guest-fsfreeze-thaw"}',
        b'{"execute": "guest-sync", "arguments": {"id": 87643}}',
        b'{"execute": "guest-file-open", "arguments": {"path": "/tmp/foo", "mode": "w"}}',
        b'{"execute": "guest-file-write", "arguments": {"handle": "file-handle-1", "buf-b64": "ImFzZGYi\\n"}}',
        b'{"execute": "guest-file-close", "arguments": {"handle": "file-handle-1"}}',
    ]


# taken from qemu-10.1
QEMU_MACHINE_HELP = """\
Supported machines are:
microvm              microvm (i386)
nitro-enclave        AWS Nitro Enclave
pc-i440fx-9.2        Standard PC (i440FX + PIIX, 1996)
pc-i440fx-9.1        Standard PC (i440FX + PIIX, 1996)
pc-i440fx-9.0        Standard PC (i440FX + PIIX, 1996)
pc-i440fx-8.2        Standard PC (i440FX + PIIX, 1996)
pc-i440fx-8.1        Standard PC (i440FX + PIIX, 1996)
pc-i440fx-8.0        Standard PC (i440FX + PIIX, 1996)
pc-i440fx-7.2        Standard PC (i440FX + PIIX, 1996)
pc-i440fx-7.1        Standard PC (i440FX + PIIX, 1996) (deprecated)
pc-i440fx-7.0        Standard PC (i440FX + PIIX, 1996) (deprecated)
pc-i440fx-6.2        Standard PC (i440FX + PIIX, 1996) (deprecated)
pc-i440fx-6.1        Standard PC (i440FX + PIIX, 1996) (deprecated)
pc-i440fx-6.0        Standard PC (i440FX + PIIX, 1996) (deprecated)
pc-i440fx-5.2        Standard PC (i440FX + PIIX, 1996) (deprecated)
pc-i440fx-5.1        Standard PC (i440FX + PIIX, 1996) (deprecated)
pc-i440fx-5.0        Standard PC (i440FX + PIIX, 1996) (deprecated)
pc-i440fx-4.2        Standard PC (i440FX + PIIX, 1996) (deprecated)
pc                   Standard PC (i440FX + PIIX, 1996) (alias of pc-i440fx-10.1)
pc-i440fx-10.1       Standard PC (i440FX + PIIX, 1996) (default)
pc-i440fx-10.0       Standard PC (i440FX + PIIX, 1996)
pc-q35-9.2           Standard PC (Q35 + ICH9, 2009)
pc-q35-9.1           Standard PC (Q35 + ICH9, 2009)
pc-q35-9.0           Standard PC (Q35 + ICH9, 2009)
pc-q35-8.2           Standard PC (Q35 + ICH9, 2009)
pc-q35-8.1           Standard PC (Q35 + ICH9, 2009)
pc-q35-8.0           Standard PC (Q35 + ICH9, 2009)
pc-q35-7.2           Standard PC (Q35 + ICH9, 2009)
pc-q35-7.1           Standard PC (Q35 + ICH9, 2009) (deprecated)
pc-q35-7.0           Standard PC (Q35 + ICH9, 2009) (deprecated)
pc-q35-6.2           Standard PC (Q35 + ICH9, 2009) (deprecated)
pc-q35-6.1           Standard PC (Q35 + ICH9, 2009) (deprecated)
pc-q35-6.0           Standard PC (Q35 + ICH9, 2009) (deprecated)
pc-q35-5.2           Standard PC (Q35 + ICH9, 2009) (deprecated)
pc-q35-5.1           Standard PC (Q35 + ICH9, 2009) (deprecated)
pc-q35-5.0           Standard PC (Q35 + ICH9, 2009) (deprecated)
pc-q35-4.2           Standard PC (Q35 + ICH9, 2009) (deprecated)
q35                  Standard PC (Q35 + ICH9, 2009) (alias of pc-q35-10.1)
pc-q35-10.1          Standard PC (Q35 + ICH9, 2009)
pc-q35-10.0          Standard PC (Q35 + ICH9, 2009)
isapc                ISA-only PC
none                 empty machine
x-remote             Experimental remote machine
"""


@pytest.fixture
def mock_machine_help(monkeypatch):
    monkeypatch.setattr(
        "fc.qemu.hazmat.qemu.subprocess.check_output",
        lambda *a, **kw: QEMU_MACHINE_HELP,
    )


def test_detect_current_machine_type_version_prefix(mock_machine_help):
    from fc.qemu.hazmat.qemu import detect_current_machine_type

    assert detect_current_machine_type("pc-i440fx") == "pc-i440fx-10.1"


def test_detect_current_machine_type_exact_version(mock_machine_help):
    from fc.qemu.hazmat.qemu import detect_current_machine_type

    assert detect_current_machine_type("pc-i440fx-6.1") == "pc-i440fx-6.1"


def test_detect_current_machine_type_q35(mock_machine_help):
    from fc.qemu.hazmat.qemu import detect_current_machine_type

    assert detect_current_machine_type("pc-q35") == "pc-q35-10.1"


def test_detect_current_machine_type_not_found(mock_machine_help):
    from fc.qemu.hazmat.qemu import detect_current_machine_type

    with pytest.raises(KeyError):
        detect_current_machine_type("nonexistent")


# excerpt from a real system, can be obtained via
# import psutil; import pprint; pprint.pprint(list(map(lambda p: p.as_dict(["name", "exe", "cmdline"]), psutil.process_iter(["pid", "name", "exe", "cmdline"]))))

QEMU10_PROCS = [
    {
        "cmdline": [
            "/nix/store/60m4rxhg2fldqaak400c0lry96ijrzqn-python3-3.13.13/bin/python3.13",
            "/nix/store/kxr6dfmbzbnyi8vyxm1g5m33j51c4fbc-python3.13-fc.qemu-1.8dev/bin/.supervised-qemu-wrapped",
            "qemu-system-x86_64 -nodefaults -only-migratable -cpu "
            "Nehalem-v2,spec-ctrl,ssbd,enforce -name "
            "slurmtest13,process=kvm.slurmtest13 -run-with "
            "chroot=/srv/vm/slurmtest13 -run-with user=nobody -serial "
            "file:/var/log/vm/slurmtest13.log -display vnc=127.0.0.1:6891 "
            "-pidfile /run/qemu.slurmtest13.pid -vga std -m 3072 -readconfig "
            "/run/qemu.slurmtest13.cfg -incoming tcp:172.20.4.110:6891 -D "
            "/var/log/vm/slurmtest13.qemu.internal.log",
            "slurmtest13",
            "/var/log/vm/slurmtest13.supervisor.log",
        ],
        "exe": "/nix/store/60m4rxhg2fldqaak400c0lry96ijrzqn-python3-3.13.13/bin/python3.13",
        "name": ".supervised-qem",
    },
    {
        "cmdline": [
            "qemu-system-x86_64",
            "-nodefaults",
            "-only-migratable",
            "-cpu",
            "Nehalem-v2,spec-ctrl,ssbd,enforce",
            "-name",
            "slurmtest13,process=kvm.slurmtest13",
            "-run-with",
            "chroot=/srv/vm/slurmtest13",
            "-run-with",
            "user=nobody",
            "-serial",
            "file:/var/log/vm/slurmtest13.log",
            "-display",
            "vnc=127.0.0.1:6891",
            "-pidfile",
            "/run/qemu.slurmtest13.pid",
            "-vga",
            "std",
            "-m",
            "3072",
            "-readconfig",
            "/run/qemu.slurmtest13.cfg",
            "-incoming",
            "tcp:172.20.4.110:6891",
            "-D",
            "/var/log/vm/slurmtest13.qemu.internal.log",
        ],
        "exe": "/nix/store/391smsk8nghkg17g5dd5dn2cdb6lkhlm-qemu-10.2.2/bin/.qemu-system-x86_64-wrapped",
        "name": "kvm.slurmtest13",
    },
    {"cmdline": [], "exe": "", "name": "kvm-pit/764111"},
    {"cmdline": [], "exe": "", "name": "kworker/4:2H-kblockd"},
    {
        "cmdline": [
            "/nix/store/60m4rxhg2fldqaak400c0lry96ijrzqn-python3-3.13.13/bin/python3.13",
            "/nix/store/37cnh63ah3ly5vlb89l65xk74w2xb85i-python3.13-fc.qemu-1.8dev/bin/.fc-qemu-wrapped",
            "maintenance",
            "enter",
        ],
        "exe": "/nix/store/60m4rxhg2fldqaak400c0lry96ijrzqn-python3-3.13.13/bin/python3.13",
        "name": ".fc-qemu-wrappe",
    },
]


def test_get_running_qemu_processes(monkeypatch):

    processes = [Mock(as_dict=Mock(return_value=p)) for p in QEMU10_PROCS]
    monkeypatch.setattr("psutil.process_iter", lambda *a, **kw: processes)

    assert len(get_running_qemu_processes()) == 1


def test_is_qemu_proc():
    assert is_qemu_proc(
        "kvm.somevm",
        "qemu-system-x86_64",
        "/nix/store/asdf-qemu/bin/qemu-system-x86_64",
    )
    assert not is_qemu_proc("", [], "")
    assert not is_qemu_proc(".fc-qemu-wrappe", ["python3.13"], "python3.13")
    assert not is_qemu_proc("kvm-pit", [], "")
    assert is_qemu_proc("kvm.somevm", ["somecmdline"], "")
    # qemu-6.0 specific behaviour
    assert is_qemu_proc("", ["/nix/store/asdf-qemu/bin/qemu-system-x86_64"], "")
    assert is_qemu_proc(
        "", ["somecmdline"], "/nix/store/asdf-qemu/bin/qemu-system-x86_64"
    )
    # qemu-10.0+ specific behaviour
    assert is_qemu_proc("", ["qemu-system-x86_64"], "")
    assert is_qemu_proc(
        "",
        ["somecmdline"],
        "/nix/store/asdf-qemu-10.2.2/bin/.qemu-system-x86_64-wrapped",
    )
