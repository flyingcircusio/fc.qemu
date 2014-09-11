from ..exc import LockError, MigrationError
from ..lock import Locks
import os
import pkg_resources
import pytest


@pytest.fixture
def vm():
    fixtures = pkg_resources.resource_filename(__name__, 'fixtures')
    setattr(VM, 'CONFD_FILE',
            os.path.join(fixtures, 'conf.d/kvm.{}'))
    vm = VM('testvm')
    vm.locks = Locks()
    vm.locks.available = {
        'testvm.root': None, 'testvm.swap': None, 'testvm.tmp': None}
    vm.locks.held = vm.locks.available
    vm.monitor.status = lambda: 'VM status: running'
    vm.destroy = lambda: None
    return vm


class TestVM(object):

    def test_rescue_roll_forward_all_ok(self, vm):
        vm.rescue() is True

    def test_rescue_should_acquire_missing_lock(self, vm, monkeypatch):
        def fake_acquire_lock(image):
            vm.locks.held[image] = vm.locks.available[image]

        vm.locks.held = {'testvm.root': None, 'testvm.swap': None}
        monkeypatch.setattr(vm, 'acquire_lock', fake_acquire_lock)
        vm.rescue()
        assert set(vm.locks.available) == set(vm.locks.held)

    def test_rescue_should_bail_out_if_locking_fails(self, vm, monkeypatch):
        def fake_acquire_lock(image):
            raise LockError('cannot acquire lock')
        monkeypatch.setattr(vm, 'acquire_lock', fake_acquire_lock)

        vm.locks.held = {'testvm.root': None, 'testvm.tmp': None}
        with pytest.raises(RuntimeError):
            vm.rescue()

    def test_rescue_should_bail_out_unless_vm_running(self, vm, monkeypatch):
        monkeypatch.setattr(vm.monitor, 'status', lambda: 'VM status: paused')
        with pytest.raises(MigrationError):
            vm.rescue()
