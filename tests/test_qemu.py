import os
import subprocess
import time

import psutil
import pytest

from fc.qemu.hazmat.qemu import Qemu


@pytest.fixture
def qemu_with_pid_file(tmp_path):
    # The fixture uses a very long name which catches a special case where
    # the proc name is limited to 16 bytes and then we failed to match
    # the running VM. Parametrizing fixtures doesn't work the way I want
    # so I did it this way ...
    pid_file = tmp_path / "run/qemu.testvmwithverylongname.pid"
    pid_file.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [
            "qemu-system-x86_64",
            "-name",
            "testvmwithverylongname,process=kvm.testvmwithverylongname",
            "-nodefaults",
            "-pidfile",
            str(pid_file),
        ]
    )
    while not pid_file.exists():
        time.sleep(0.01)
    q = Qemu(dict(name="testvmwithverylongname", id=1234))
    while not q.proc():
        time.sleep(0.01)
    try:
        yield q
    finally:
        proc.kill()


def test_proc_running(qemu_with_pid_file):
    assert isinstance(qemu_with_pid_file.proc(), psutil.Process)


def test_proc_not_running(qemu_with_pid_file):
    with qemu_with_pid_file.pid_file.open("w") as p:
        p.write("0\n")
    assert qemu_with_pid_file.proc() is None


def test_proc_wrong_process(qemu_with_pid_file):
    with qemu_with_pid_file.pid_file.open("w") as p:
        p.write("1\n")
    assert qemu_with_pid_file.proc() is None


def test_proc_no_pid_file(qemu_with_pid_file):
    os.unlink(qemu_with_pid_file.pid_file)
    assert qemu_with_pid_file.proc() is None


def test_proc_empty_pid_file(qemu_with_pid_file):
    # Empty out the file
    qemu_with_pid_file.pid_file.open("w").close()
    assert qemu_with_pid_file.proc() is None


def test_proc_pid_file_with_garbage(qemu_with_pid_file):
    """pid files are allowed to contain trailing lines with garbage,
    process retrieval must still work then."""
    with qemu_with_pid_file.pid_file.open("a") as f:
        f.write("trailing line\n")
    assert isinstance(qemu_with_pid_file.proc(), psutil.Process)
