from ..agent import Agent
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
    assert out == """\
create-vm test00
rbd --id "admin" map test/test00.tmp
mkfs -q -m 1 -t ext4 "/dev/rbd/test/test00.tmp"
tune2fs -e remount-ro "/dev/rbd/test/test00.tmp"
rbd --id "admin" unmap /dev/rbd/test/test00.tmp
rbd --id "admin" map test/test00.swap
mkswap -f "/dev/rbd/test/test00.swap"
rbd --id "admin" unmap /dev/rbd/test/test00.swap
"""

    assert status() == """\
online
lock: test00.root@localhost
lock: test00.swap@localhost
lock: test00.tmp@localhost
"""

    vm.stop()
    assert status() == 'offline\n'

    vm.delete()
    assert status() == 'offline\n'


def test_simple_vm_lifecycle_ensure_going_offline(vm, capsys):
    def status():
        capsys.readouterr()
        vm.status()
        out, err = capsys.readouterr()
        return out

    assert status() == 'offline\n'

    vm.ensure()

    out, err = capsys.readouterr()
    assert out == """\
VM should be running here.
create-vm test00
rbd --id "admin" map test/test00.tmp
mkfs -q -m 1 -t ext4 "/dev/rbd/test/test00.tmp"
tune2fs -e remount-ro "/dev/rbd/test/test00.tmp"
rbd --id "admin" unmap /dev/rbd/test/test00.tmp
rbd --id "admin" map test/test00.swap
mkswap -f "/dev/rbd/test/test00.swap"
rbd --id "admin" unmap /dev/rbd/test/test00.swap
resizing disk for VM to 5 GiB
"""

    assert status() == """\
online
lock: test00.root@localhost
lock: test00.swap@localhost
lock: test00.tmp@localhost
"""

    vm.cfg['online'] = False
    vm.save()
    vm.ensure()
    assert status() == 'offline\n'

    vm.delete()
    assert status() == 'offline\n'


def test_simple_vm_lifecycle_ensure_moving_away(vm, capsys):
    def status():
        capsys.readouterr()
        vm.status()
        out, err = capsys.readouterr()
        return out

    assert status() == 'offline\n'

    vm.ensure()

    out, err = capsys.readouterr()
    assert out == """\
VM should be running here.
create-vm test00
rbd --id "admin" map test/test00.tmp
mkfs -q -m 1 -t ext4 "/dev/rbd/test/test00.tmp"
tune2fs -e remount-ro "/dev/rbd/test/test00.tmp"
rbd --id "admin" unmap /dev/rbd/test/test00.tmp
rbd --id "admin" map test/test00.swap
mkswap -f "/dev/rbd/test/test00.swap"
rbd --id "admin" unmap /dev/rbd/test/test00.swap
resizing disk for VM to 5 GiB
"""

    assert status() == """\
online
lock: test00.root@localhost
lock: test00.swap@localhost
lock: test00.tmp@localhost
"""

    vm.cfg['kvm_host'] = 'somewhereelse'
    vm.save()
    vm.ensure()
    assert status() == 'offline\n'

    vm.delete()
    assert status() == 'offline\n'


def test_crashed_vm_clean_restart(vm, capsys):
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
    assert out == """\
VM should be running here.
create-vm test00
rbd --id "admin" map test/test00.tmp
mkfs -q -m 1 -t ext4 "/dev/rbd/test/test00.tmp"
tune2fs -e remount-ro "/dev/rbd/test/test00.tmp"
rbd --id "admin" unmap /dev/rbd/test/test00.tmp
rbd --id "admin" map test/test00.swap
mkswap -f "/dev/rbd/test/test00.swap"
rbd --id "admin" unmap /dev/rbd/test/test00.swap
resizing disk for VM to 5 GiB
"""

    assert status() == """\
online
lock: test00.root@localhost
lock: test00.swap@localhost
lock: test00.tmp@localhost
"""

    vm.qemu.proc().kill()
    vm.qemu.proc().wait(10)
    assert status() == """\
offline
lock: test00.root@localhost
lock: test00.swap@localhost
lock: test00.tmp@localhost
"""

    vm.ensure()
    assert status() == """\
online
lock: test00.root@localhost
lock: test00.swap@localhost
lock: test00.tmp@localhost
"""

    vm.stop()
    vm.delete()
    assert status() == 'offline\n'


def test_vm_swapsize():
    from ..agent import swap_size
    assert swap_size(256) == 1 * 1024**3
    assert swap_size(512) == 1 * 1024**3
    assert swap_size(768) == 1 * 1024**3
    assert swap_size(1024) == 1 * 1024**3
    assert swap_size(2048) == 1 * 1024**3
    assert swap_size(4096) == 2 * 1024**3


def test_vm_tmpsize():
    from ..agent import tmp_size
    assert tmp_size(5) == 5120 * 1024**2
    assert tmp_size(10) == 5120 * 1024**2
    assert tmp_size(50) == 5120 * 1024**2
    assert tmp_size(100) == 10240 * 1024**2
    assert tmp_size(200) == 20480 * 1024**2
