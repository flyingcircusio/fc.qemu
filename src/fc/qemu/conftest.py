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
    log_exceptions = False  # set to True to temporarily get detailed tracebacks

    def test_logger(logger, method_name, event):
        result = []

        show_methods = util.test_log_options['show_methods']
        if show_methods and method_name not in show_methods:
            raise structlog.DropEvent

        show_events = util.test_log_options['show_events']
        if show_events:
            for show in show_events:
                if show in event['event']:
                    break
            else:
                raise structlog.DropEvent

        if log_exceptions:
            stack = event.pop("stack", None)
            exc = event.pop("exception", None)
        for key in sorted(event):
            result.append('{}={}'.format(key, event[key]))
        util.log_data.append(' '.join(result))
        if log_exceptions:
          if stack:
              util.log_data.extend(stack.splitlines())
          if exc:
              util.log_data.extend(exc.splitlines())
        raise structlog.DropEvent

    structlog.configure(processors=(
        ([structlog.processors.format_exc_info] if log_exceptions else []) +
        [test_logger]))


@pytest.fixture(autouse=True)
def reset_structlog(setup_structlog):
    from . import util
    util.log_data = []
    util.test_log_options = {
        'show_methods': [],
        'show_events': []}


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
    if os.path.exists('/etc/qemu/vm/.simplevm.cfg.staging'):
        os.unlink('/etc/qemu/vm/.simplevm.cfg.staging')
    vm = Agent('simplevm')
    vm.timeout_graceful = 1
    vm.__enter__()
    vm.qemu.guestagent_timeout = .1
    vm.qemu.qmp_timeout = .1
    vm.qemu.vm_expected_overhead = 128
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
    if os.path.exists('/etc/qemu/vm/.simplevm.cfg.staging'):
        os.unlink('/etc/qemu/vm/.simplevm.cfg.staging')


def get_log():
    from fc.qemu import util
    result = '\n'.join(util.log_data)
    util.log_data = []
    return result
