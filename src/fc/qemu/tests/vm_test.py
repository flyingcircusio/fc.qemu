from ..agent import Agent, swap_size, tmp_size
import os
import pkg_resources
import pytest
import shutil
import subprocess


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
    vm.__exit__(None, None, None)
    os.unlink('/etc/qemu/vm/simplevm.cfg')


def test_simple_vm_lifecycle_start_stop(vm, capsys):
    def status():
        capsys.readouterr()
        vm.status()
        out, err = capsys.readouterr()
        return out

    assert status() == 'offline\n'

    vm.start()

    out, err = capsys.readouterr()
    assert ("""\
create-vm -I test00
rbd --id "admin" map test/test00.tmp
mkfs -q -m 1 -t ext4 "/dev/rbd/test/test00.tmp"
tune2fs -e remount-ro "/dev/rbd/test/test00.tmp"
rbd --id "admin" unmap /dev/rbd/test/test00.tmp
rbd --id "admin" map test/test00.swap
mkswap -f "/dev/rbd/test/test00.swap"
rbd --id "admin" unmap /dev/rbd/test/test00.swap
""" in out)

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
    vm.save()
    vm.ensure()
    assert status() == 'offline\n'


def test_simple_vm_lifecycle_ensure_moving_away(vm, capsys, caplog):
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

    vm.cfg['kvm_host'] = 'somewhereelse'
    vm.save()
    vm.ensure()
    assert status() == 'offline\n'


def test_crashed_vm_clean_restart(vm, capsys, caplog):
    def status():
        capsys.readouterr()
        vm.status()
        out, err = capsys.readouterr()
        return out

    assert status() == """\
offline
"""

    vm.ensure()

    out, err = capsys.readouterr()
    assert 'VM test00 should be running here' in caplog.text()
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


def test_clean_up_crashed_vm(vm):
    vm.ensure()
    assert vm.qemu.is_running() is True
    vm.qemu.proc().kill()
    vm.qemu.proc().wait(2)
    assert vm.ceph.locked_by_me() is True
    vm.cfg['online'] = False
    vm.ensure()
    assert vm.ceph.locked_by_me() is False


def test_vm_swapsize():
    assert swap_size(256) == 1 * 1024**3
    assert swap_size(512) == 1 * 1024**3
    assert swap_size(768) == 1 * 1024**3
    assert swap_size(1024) == 1 * 1024**3
    assert swap_size(2048) == 1 * 1024**3
    assert swap_size(4096) == 2 * 1024**3


def test_vm_tmpsize():
    assert tmp_size(5) == 5120 * 1024**2
    assert tmp_size(10) == 5120 * 1024**2
    assert tmp_size(50) == 5120 * 1024**2
    assert tmp_size(100) == 10240 * 1024**2
    assert tmp_size(200) == 20480 * 1024**2


def test_vm_migration():
    subprocess.check_call('/vagrant/test-migration.sh', shell=True)
