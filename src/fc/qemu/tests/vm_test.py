from ..agent import Agent, swap_size, tmp_size
import os
import pkg_resources
import pytest
import shutil
import subprocess
import traceback
import sys


@pytest.yield_fixture
def clean_environment():
    def clean():
        subprocess.call('pkill -f qemu', shell=True)
        subprocess.call('rbd rm test/test00.swap', shell=True)
        subprocess.call('rbd rm test/test00.root', shell=True)
        subprocess.call('rbd rm test/test00.tmp', shell=True)
    clean()
    yield
    clean()


@pytest.yield_fixture
def vm(clean_environment):
    fixtures = pkg_resources.resource_filename(__name__, 'fixtures')
    shutil.copy(fixtures + '/simplevm.yaml', '/etc/qemu/vm/simplevm.cfg')
    vm = Agent('simplevm')
    vm.timeout_graceful = 1
    vm.__enter__()
    yield vm
    exc_info = sys.exc_info()
    vm.__exit__(*exc_info)
    if len(exc_info):
        print(traceback.print_tb(exc_info[2]))
    os.unlink('/etc/qemu/vm/simplevm.cfg')


def test_simple_vm_lifecycle_start_stop(vm, capfd):

    def status():
        capfd.readouterr()
        vm.status()
        out, err = capfd.readouterr()
        return out

    assert status() == 'offline\n'

    vm.start()
    out, err = capfd.readouterr()
    assert """\
/usr/local/sbin/create-vm -I test00
rbd -c "/etc/ceph/ceph.conf" --id "admin" map "test/test00.tmp"
sgdisk -o "/dev/rbd/test/test00.tmp"
Creating new GPT entries.
The operation has completed successfully.
sgdisk -a 8192 -n 1:8192:0 -c "1:tmp" -t 1:8300 "/dev/rbd/test/test00.tmp"
The operation has completed successfully.
partprobe
mkfs.xfs -q -f -L "tmp" "/dev/rbd/test/test00.tmp-part1"
mount "/dev/rbd/test/test00.tmp-part1" "/mnt/rbd/test/test00.tmp"
umount "/mnt/rbd/test/test00.tmp"
rbd -c "/etc/ceph/ceph.conf" --id "admin" unmap "/dev/rbd/test/test00.tmp"
rbd -c "/etc/ceph/ceph.conf" --id "admin" map "test/test00.swap"
mkswap -f -L "swap" "/dev/rbd/test/test00.swap"
""" in out

    assert """\
rbd -c "/etc/ceph/ceph.conf" --id "admin" unmap "/dev/rbd/test/test00.swap"
""" in out

    assert status() == """\
online
lock: test00.root@host1
lock: test00.swap@host1
lock: test00.tmp@host1
"""

    vm.stop()
    assert status() == 'offline\n'


def test_simple_vm_lifecycle_ensure_going_offline(vm, capsys, caplog):
    def status():
        capsys.readouterr()
        vm.status()
        out, err = capsys.readouterr()
        return out

    assert status() == 'offline\n'

    vm.ensure()

    out, err = capsys.readouterr()
    assert 'VM test00 should be running here' in caplog.text()
    assert status() == """\
online
lock: test00.root@host1
lock: test00.swap@host1
lock: test00.tmp@host1
"""

    vm.cfg['online'] = False
    vm.save_enc()
    vm.ensure()
    assert status() == 'offline\n'


def test_vm_not_running_here(vm, capsys):
    def status():
        capsys.readouterr()
        vm.status()
        out, err = capsys.readouterr()
        return out

    assert status() == 'offline\n'

    vm.cfg['kvm_host'] = 'otherhost'
    vm.ensure()
    assert status() == 'offline\n'


def test_crashed_vm_clean_restart(vm, capsys):
    def status():
        capsys.readouterr()
        vm.status()
        out, _err = capsys.readouterr()
        return out

    assert status() == 'offline\n'

    vm.ensure()

    assert status() == """\
online
lock: test00.root@host1
lock: test00.swap@host1
lock: test00.tmp@host1
"""

    vm.qemu.proc().kill()
    vm.qemu.proc().wait(2)
    assert status() == """\
offline
lock: test00.root@host1
lock: test00.swap@host1
lock: test00.tmp@host1
"""

    vm.ensure()
    assert status() == """\
online
lock: test00.root@host1
lock: test00.swap@host1
lock: test00.tmp@host1
"""

    vm.stop()
    assert status() == 'offline\n'


def test_do_not_clean_up_crashed_vm_that_doesnt_get_restarted(vm):
    vm.ensure()
    assert vm.qemu.is_running() is True
    vm.qemu.proc().kill()
    vm.qemu.proc().wait(2)
    assert vm.ceph.locked_by_me() is True
    vm.cfg['online'] = False
    vm.ensure()
    # We don't really know what's going on here, so, yeah, don't touch it.
    assert vm.ceph.locked_by_me() is True


def test_vm_swapsize():
    assert swap_size(256) == 1 * 1024 ** 3
    assert swap_size(512) == 1 * 1024 ** 3
    assert swap_size(768) == 1 * 1024 ** 3
    assert swap_size(1024) == 1 * 1024 ** 3
    assert swap_size(2048) == 1 * 1024 ** 3
    assert swap_size(4096) == 2 * 1024 ** 3


def test_vm_tmpsize():
    assert tmp_size(5) == 5120 * 1024 ** 2
    assert tmp_size(10) == 5120 * 1024 ** 2
    assert tmp_size(50) == 5120 * 1024 ** 2
    assert tmp_size(100) == 10240 * 1024 ** 2
    assert tmp_size(200) == 20480 * 1024 ** 2


def test_vm_migration():
    subprocess.check_call('./test-migration.sh', shell=True)
