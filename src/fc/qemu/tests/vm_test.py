from ..agent import Agent, swap_size, tmp_size
from ..util import MiB, GiB
from ..ellipsis import Ellipsis
import os
import pkg_resources
import pytest
import shutil
import subprocess
import traceback
import sys


def get_log():
    from fc.qemu import util
    result = '\n'.join(util.log_data)
    util.log_data = []
    return result


@pytest.yield_fixture
def clean_environment():
    def clean():
        subprocess.call('pkill -f qemu', shell=True)
        subprocess.call('rbd rm rbd.ssd/test00.swap', shell=True)
        subprocess.call('rbd rm rbd.ssd/test00.root', shell=True)
        subprocess.call('rbd rm rbd.ssd/test00.tmp', shell=True)
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


def test_simple_vm_lifecycle_start_stop(vm):

    vm.status()

    status = get_log()
    assert status == """\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.tmp"""

    vm.start()

    out = get_log()
    assert out == Ellipsis("""\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=generate-config machine=simplevm
event=ensure-root machine=test00 subsystem=ceph
event=create-vm machine=test00 subsystem=ceph
args=-I rbd.ssd test00 event=/usr/local/sbin/create-vm machine=test00 subsystem=ceph
event=lock machine=test00 subsystem=ceph volume=rbd.ssd/test00.root
event=ensure-tmp machine=test00 subsystem=ceph
event=lock machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
args=-c "/etc/ceph/ceph.conf" --id "admin" map "rbd.ssd/test00.tmp" event=rbd machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
event=create-fs machine=test00 subsystem=ceph type=xfs volume=rbd.ssd/test00.tmp
args=-o "/dev/rbd/rbd.ssd/test00.tmp" event=sgdisk machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
event=sgdisk machine=test00 output=Creating new GPT entries.
The operation has completed successfully. subsystem=ceph volume=rbd.ssd/test00.tmp
args=-a 8192 -n 1:8192:0 -c "1:tmp" -t 1:8300 "/dev/rbd/rbd.ssd/test00.tmp" event=sgdisk machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
event=sgdisk machine=test00 output=The operation has completed successfully. subsystem=ceph volume=rbd.ssd/test00.tmp
args= event=partprobe machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
args=-q -f -L "tmp" "/dev/rbd/rbd.ssd/test00.tmp-part1" event=mkfs.xfs machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
event=mkfs.xfs machine=test00 output=log stripe unit (4194304 bytes) is too large (maximum is 256KiB)
log stripe unit adjusted to 32KiB subsystem=ceph volume=rbd.ssd/test00.tmp
event=seed-enc volume=test00.tmp
event=seed-enc machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
args="/dev/rbd/rbd.ssd/test00.tmp-part1" "/mnt/rbd/rbd.ssd/test00.tmp" event=mount machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
args="/mnt/rbd/rbd.ssd/test00.tmp" event=umount machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
args=-c "/etc/ceph/ceph.conf" --id "admin" unmap "/dev/rbd/rbd.ssd/test00.tmp" event=rbd machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
event=ensure-swap machine=test00 subsystem=ceph
event=lock machine=test00 subsystem=ceph volume=rbd.ssd/test00.swap
args=-c "/etc/ceph/ceph.conf" --id "admin" map "rbd.ssd/test00.swap" event=rbd machine=test00 subsystem=ceph volume=rbd.ssd/test00.swap
args=-f -L "swap" "/dev/rbd/rbd.ssd/test00.swap" event=mkswap machine=test00 subsystem=ceph volume=rbd.ssd/test00.swap
event=mkswap machine=test00 output=Setting up swapspace version 1, size = 1048572 KiB
LABEL=swap, UUID=...-...-...-...-... subsystem=ceph volume=rbd.ssd/test00.swap
args=-c "/etc/ceph/ceph.conf" --id "admin" unmap "/dev/rbd/rbd.ssd/test00.swap" event=rbd machine=test00 subsystem=ceph volume=rbd.ssd/test00.swap
event=start-qemu machine=test00 subsystem=qemu
additional_args=() event=qemu-system-x86_64 local_args=[\'-daemonize\', \'-nodefaults\', \'-name test00,process=kvm.test00\', \'-chroot /srv/vm/test00\', \'-runas nobody\', \'-serial file:/var/log/vm/test00.log\', \'-display vnc=host1:2345\', \'-pidfile /run/qemu.test00.pid\', \'-vga std\', \'-m 256\', \'-watchdog i6300esb\', \'-watchdog-action reset\', \'-readconfig /run/qemu.test00.cfg\'] machine=test00 subsystem=qemu
arguments={} event=qmp_capabilities id=None machine=test00 subsystem=qemu/qmp
arguments={} event=query-status id=None machine=test00 subsystem=qemu/qmp
arguments={} event=query-block id=None machine=test00 subsystem=qemu/qmp
action=throttle current_iops=0 device=virtio0 event=ensure-throttle machine=simplevm target_iops=3000
arguments={\'bps_rd\': 0, \'bps_wr\': 0, \'bps\': 0, \'iops\': 3000, \'iops_rd\': 0, \'device\': u\'virtio0\', \'iops_wr\': 0} event=block_set_io_throttle id=None machine=test00 subsystem=qemu/qmp
action=throttle current_iops=0 device=virtio1 event=ensure-throttle machine=simplevm target_iops=3000
arguments={\'bps_rd\': 0, \'bps_wr\': 0, \'bps\': 0, \'iops\': 3000, \'iops_rd\': 0, \'device\': u\'virtio1\', \'iops_wr\': 0} event=block_set_io_throttle id=None machine=test00 subsystem=qemu/qmp
action=throttle current_iops=0 device=virtio2 event=ensure-throttle machine=simplevm target_iops=3000
arguments={\'bps_rd\': 0, \'bps_wr\': 0, \'bps\': 0, \'iops\': 3000, \'iops_rd\': 0, \'device\': u\'virtio2\', \'iops_wr\': 0} event=block_set_io_throttle id=None machine=test00 subsystem=qemu/qmp
event=register-consul machine=simplevm""")

    vm.status()
    assert get_log() == Ellipsis("""\
arguments={} event=query-status id=None machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=online
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.tmp""")

    vm.stop()
    get_log()

    vm.status()
    assert get_log() == """\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.tmp"""


def test_simple_vm_lifecycle_ensure_going_offline(vm, capsys, caplog):
    vm.status()
    assert get_log() == """\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.tmp"""

    vm.ensure()
    out = get_log()
    assert "action=start event=ensure-state found=offline machine=simplevm wanted=online" in out

    vm.status()
    assert get_log() == Ellipsis("""\
arguments={} event=query-status id=None machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=online
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.tmp""")

    vm.cfg['online'] = False
    vm.save_enc()
    vm.ensure()
    get_log()
    vm.status()
    assert get_log() == """\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.tmp"""


def test_vm_not_running_here(vm, capsys):
    vm.status()
    assert get_log() == """\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.tmp"""

    vm.cfg['kvm_host'] = 'otherhost'
    vm.ensure()
    vm.status()
    assert get_log() == """\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
ceph_lock=False event=check-state-consistency is_consistent=True machine=simplevm proc=False qemu=False
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=purge-run-files machine=test00 subsystem=qemu
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.tmp"""


def test_crashed_vm_clean_restart(vm):
    vm.status()

    assert get_log() == Ellipsis("""\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.tmp""")

    vm.ensure()
    vm.status()
    assert get_log() == Ellipsis("""\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
action=start event=ensure-state found=offline machine=simplevm wanted=online
...
event=vm-status machine=simplevm result=online
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.tmp""")

    vm.qemu.proc().kill()
    vm.qemu.proc().wait(2)
    get_log()

    vm.status()
    assert get_log() == Ellipsis("""\
arguments={} event=query-status id=None machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.tmp""")

    vm.ensure()

    vm.status()
    assert get_log() == Ellipsis("""\
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
action=start event=ensure-state found=offline machine=simplevm wanted=online
...
event=vm-status machine=simplevm result=online
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/test00.tmp""")

    vm.stop()
    vm.status()
    assert get_log() == Ellipsis("""\
arguments={} event=query-status id=None machine=test00 subsystem=qemu/qmp
event=graceful-shutdown machine=simplevm
arguments={'keys': [{'data': 'ctrl', 'type': 'qcode'}, {'data': 'alt', 'type': 'qcode'}, {'data': 'delete', 'type': 'qcode'}]} event=send-key id=None machine=test00 subsystem=qemu/qmp
event=graceful-shutdown-failed machine=simplevm reason=timeout
arguments={} event=query-status id=None machine=test00 subsystem=qemu/qmp
event=kill-vm machine=simplevm
arguments={} event=query-status id=None machine=test00 subsystem=qemu/qmp
event=killed-vm machine=simplevm
event=unlock machine=test00 subsystem=ceph volume=rbd.ssd/test00.root
event=unlock machine=test00 subsystem=ceph volume=rbd.ssd/test00.swap
event=unlock machine=test00 subsystem=ceph volume=rbd.ssd/test00.tmp
event=purge-run-files machine=test00 subsystem=qemu
event=deregister-consul machine=simplevm
event=connect-failed exc_info=True machine=test00 subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/test00.tmp""")


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


def test_simple_vm_snapshot(vm):
    assert list(x.fullname for x in vm.ceph.root.snapshots) == []
    vm.ceph.root.ensure_presence()
    vm.snapshot('asdf')
    assert list(x.fullname for x in vm.ceph.root.snapshots) == [
        'rbd.ssd/test00.root@asdf']


def test_vm_throttle_iops(vm):
    vm.start()
    get_log()

    vm.ensure_online_disk_throttle()
    assert get_log() == """\
arguments={} event=query-block id=None machine=test00 subsystem=qemu/qmp
action=none current_iops=3000 device=virtio0 event=ensure-throttle machine=simplevm target_iops=3000
action=none current_iops=3000 device=virtio1 event=ensure-throttle machine=simplevm target_iops=3000
action=none current_iops=3000 device=virtio2 event=ensure-throttle machine=simplevm target_iops=3000"""

    vm.cfg['iops'] = 10

    vm.ensure_online_disk_throttle()
    assert get_log() == """\
arguments={} event=query-block id=None machine=test00 subsystem=qemu/qmp
action=throttle current_iops=3000 device=virtio0 event=ensure-throttle machine=simplevm target_iops=10
arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10, 'iops_rd': 0, 'device': u'virtio0', 'iops_wr': 0} event=block_set_io_throttle id=None machine=test00 subsystem=qemu/qmp
action=throttle current_iops=3000 device=virtio1 event=ensure-throttle machine=simplevm target_iops=10
arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10, 'iops_rd': 0, 'device': u'virtio1', 'iops_wr': 0} event=block_set_io_throttle id=None machine=test00 subsystem=qemu/qmp
action=throttle current_iops=3000 device=virtio2 event=ensure-throttle machine=simplevm target_iops=10
arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10, 'iops_rd': 0, 'device': u'virtio2', 'iops_wr': 0} event=block_set_io_throttle id=None machine=test00 subsystem=qemu/qmp"""

    vm.ensure_online_disk_throttle()
    assert get_log() == """\
arguments={} event=query-block id=None machine=test00 subsystem=qemu/qmp
action=none current_iops=10 device=virtio0 event=ensure-throttle machine=simplevm target_iops=10
action=none current_iops=10 device=virtio1 event=ensure-throttle machine=simplevm target_iops=10
action=none current_iops=10 device=virtio2 event=ensure-throttle machine=simplevm target_iops=10"""


def test_vm_resize_disk(vm):
    vm.start()
    get_log()
    vm.ensure_online_disk_size()
    assert get_log() == """\
action=none event=check-disk-size found=5368709120 machine=simplevm wanted=5368709120\
"""

    vm.cfg['disk'] *= 2

    vm.ensure_online_disk_size()
    assert get_log() == """\
action=resize event=check-disk-size found=5368709120 machine=simplevm wanted=10737418240
arguments={'device': 'virtio0', 'size': 10737418240} event=block_resize id=None machine=test00 subsystem=qemu/qmp"""

    vm.ensure_online_disk_size()
    assert get_log() == """\
action=none event=check-disk-size found=10737418240 machine=simplevm wanted=10737418240"""


def test_swap_size():
    assert swap_size(512) == 1024 * MiB
    assert swap_size(768) == 1024 * MiB
    assert swap_size(1024) == 1024 * MiB
    assert swap_size(2048) == 1448 * MiB
    assert swap_size(4096) == 2048 * MiB
    assert swap_size(8192) == 2896 * MiB


def test_tmp_size():
    assert tmp_size(30) == 5 * GiB
    assert tmp_size(50) == 7 * GiB
    assert tmp_size(100) == 10 * GiB
    assert tmp_size(500) == 22 * GiB
    assert tmp_size(1000) == 31 * GiB


def test_vm_migration():
    subprocess.check_call('./test-migration.sh', shell=True)
