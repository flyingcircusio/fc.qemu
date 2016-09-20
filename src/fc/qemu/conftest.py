from .agent import Agent
import os
import pkg_resources
import pytest
import shutil
import structlog
import subprocess
import sys
import traceback


def pytest_collectstart(collector):
    # sys.modules['rados'] = mock.Mock()
    # sys.modules['rbd'] = mock.Mock()
    from fc.qemu.sysconfig import sysconfig
    sysconfig.load_system_config()


@pytest.fixture(scope='session')
def setup_structlog():
    from . import util
    util.log_data = []

    def test_logger(logger, method_name, event):
        result = []
        for key in sorted(event):
            result.append('{}={}'.format(key, event[key]))
        util.log_data.append(' '.join(result))
        raise structlog.DropEvent

    structlog.configure(
        processors=[test_logger])


@pytest.fixture(autouse=True)
def reset_structlog(setup_structlog):
    from . import util
    util.log_data = []


def pytest_assertrepr_compare(op, left, right):
    if left.__class__.__name__ == 'Ellipsis':
        return left.compare(right).diff
    elif right.__class__.__name__ == 'Ellipsis':
        return right.compare(left).diff


@pytest.yield_fixture
def clean_environment():
    def clean():
        subprocess.call('pkill -f qemu', shell=True)
        subprocess.call('rbd rm rbd.ssd/simplevm.swap', shell=True)
        subprocess.call('rbd snap purge rbd.ssd/simplevm.root', shell=True)
        subprocess.call('rbd rm rbd.ssd/simplevm.root', shell=True)
        subprocess.call('rbd rm rbd.ssd/simplevm.tmp', shell=True)
    clean()
    yield
    clean()


@pytest.yield_fixture
def vm(clean_environment):
    fixtures = pkg_resources.resource_filename(__name__, 'tests/fixtures')
    shutil.copy(fixtures + '/simplevm.yaml', '/etc/qemu/vm/simplevm.cfg')
    vm = Agent('simplevm')
    vm.timeout_graceful = 1
    vm.__enter__()
    for snapshot in vm.ceph.root.snapshots:
        snapshot.remove()
    vm.qemu.destroy()
    vm.unlock()
    get_log()
    yield vm
    for snapshot in vm.ceph.root.snapshots:
        snapshot.remove()
    exc_info = sys.exc_info()
    vm.__exit__(*exc_info)
    if len(exc_info):
        print(traceback.print_tb(exc_info[2]))
    os.unlink('/etc/qemu/vm/simplevm.cfg')


def get_log():
    from fc.qemu import util
    result = '\n'.join(util.log_data)
    util.log_data = []
    return result
