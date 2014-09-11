from ..agent import Agent
import pkg_resources
import pytest
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
    vm = Agent(fixtures + '/simplevm.yaml')
    vm.timeout_graceful = 1
    vm.__enter__()
    yield vm
    vm.__exit__(None, None, None)


def test_simple_vm_lifecycle(vm, capsys):
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
