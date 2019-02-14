from ..hazmat.qemu import Qemu
import os
import psutil
import pytest
import subprocess
import tempfile
import time


@pytest.yield_fixture
def qemu_with_pidfile():
    # The fixture uses a very long name which catches a special case where
    # the proc name is limited to 16 bytes and then we failed to match
    # the running VM. Parametrizing fixtures doesn't work the way I want
    # so I did it this way ...
    try:
        os.unlink('/run/qemu.testvmwithverylongname.pid')
    except OSError:
        pass
    proc = subprocess.Popen(
        ['qemu-system-x86_64',
         '-name', 'testvmwithverylongname,process=kvm.testvmwithverylongname',
         '-nodefaults', '-pidfile', '/run/qemu.testvmwithverylongname.pid'])
    while not os.path.exists('/run/qemu.testvmwithverylongname.pid'):
         time.sleep(0.01)
    q = Qemu(dict(name='testvmwithverylongname', id=1234))
    try:
        yield q
    finally:
        proc.kill()


def test_proc_running(qemu_with_pidfile):
    assert isinstance(qemu_with_pidfile.proc(), psutil.Process)


def test_proc_not_running(qemu_with_pidfile):
    with open(qemu_with_pidfile.pidfile, 'w') as p:
        p.write('0\n')
    assert qemu_with_pidfile.proc() is None


def test_proc_wrong_process(qemu_with_pidfile):
    with open(qemu_with_pidfile.pidfile, 'w') as p:
        p.write('1\n')
    assert qemu_with_pidfile.proc() is None


def test_proc_no_pidfile(qemu_with_pidfile):
    os.unlink(qemu_with_pidfile.pidfile)
    assert qemu_with_pidfile.proc() is None


def test_proc_empty_pidfile(qemu_with_pidfile):
    open(qemu_with_pidfile.pidfile, 'w').close()
    assert qemu_with_pidfile.proc() is None


def test_proc_pidfile_with_garbage(qemu_with_pidfile):
    with open(qemu_with_pidfile.pidfile, 'a') as f:
        f.write('trailing line\n')
    assert isinstance(qemu_with_pidfile.proc(), psutil.Process)


def test_disk_cache_mode_default_writeback():
    q = Qemu(dict(name='testvm', id=1234))
    assert q.disk_cache_mode == "writeback"


def test_disk_cache_mode_default_writeback2():
    q = Qemu(dict(name='testvm', id=1234, qemu=dict()))
    assert q.disk_cache_mode == "writeback"


def test_disk_cache_mode_enc_enabled():
    q = Qemu(dict(name='testvm', id=1234, qemu=dict(write_back_cache=True)))
    assert q.disk_cache_mode == "writeback"


def test_disk_cache_mode_enc_disabled():
    q = Qemu(dict(name='testvm', id=1234, qemu=dict(write_back_cache=False)))
    assert q.disk_cache_mode == "none"
