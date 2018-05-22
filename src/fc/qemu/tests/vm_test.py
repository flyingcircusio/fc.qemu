from ..agent import swap_size, tmp_size
from ..conftest import get_log
from ..ellipsis import Ellipsis
from ..util import MiB, GiB
from fc.qemu import util
import datetime
import os.path
import pytest
import subprocess


def test_simple_vm_lifecycle_start_stop(vm):
    util.test_log_options['show_events'] = ['vm-status', 'rbd-status']

    vm.status()

    status = get_log()
    assert status == """\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""

    util.test_log_options['show_events'] = []
    vm.start()

    out = get_log()
    # This is 1 end-to-end logging test to see everything.
    assert out == Ellipsis("""\
event=acquire-lock machine=simplevm target=/run/qemu.simplevm.lock
event=acquire-lock machine=simplevm result=locked target=/run/qemu.simplevm.lock
count=1 event=lock-status machine=simplevm
event=generate-config machine=simplevm
event=ensure-root machine=simplevm subsystem=ceph
event=create-vm machine=simplevm subsystem=ceph
args=-I rbd.ssd simplevm event=/usr/local/sbin/create-vm machine=simplevm subsystem=ceph
event=lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
event=ensure-tmp machine=simplevm subsystem=ceph
event=lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
args=-c "/etc/ceph/ceph.conf" --id "admin" map "rbd.ssd/simplevm.tmp" event=rbd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=create-fs machine=simplevm subsystem=ceph type=xfs volume=rbd.ssd/simplevm.tmp
args=-o "/dev/rbd/rbd.ssd/simplevm.tmp" event=sgdisk machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=sgdisk machine=simplevm output=Creating new GPT entries.
The operation has completed successfully. subsystem=ceph volume=rbd.ssd/simplevm.tmp
args=-a 8192 -n 1:8192:0 -c "1:tmp" -t 1:8300 "/dev/rbd/rbd.ssd/simplevm.tmp" event=sgdisk machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=sgdisk machine=simplevm output=The operation has completed successfully. subsystem=ceph volume=rbd.ssd/simplevm.tmp
args=/dev/rbd/rbd.ssd/simplevm.tmp event=partprobe machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
args=-q -f -L "tmp" "/dev/rbd/rbd.ssd/simplevm.tmp-part1" event=mkfs.xfs machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=mkfs.xfs machine=simplevm output=log stripe unit (4194304 bytes) is too large (maximum is 256KiB)
log stripe unit adjusted to 32KiB subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=seed machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
args="/dev/rbd/rbd.ssd/simplevm.tmp-part1" "/mnt/rbd/rbd.ssd/simplevm.tmp" event=mount machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
args="/mnt/rbd/rbd.ssd/simplevm.tmp" event=umount machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
args=-c "/etc/ceph/ceph.conf" --id "admin" unmap "/dev/rbd/rbd.ssd/simplevm.tmp" event=rbd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=ensure-swap machine=simplevm subsystem=ceph
event=lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
args=-c "/etc/ceph/ceph.conf" --id "admin" map "rbd.ssd/simplevm.swap" event=rbd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
args=-f -L "swap" "/dev/rbd/rbd.ssd/simplevm.swap" event=mkswap machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
event=mkswap machine=simplevm output=Setting up swapspace version 1, size = 1048572 KiB
LABEL=swap, UUID=...-...-...-...-... subsystem=ceph volume=rbd.ssd/simplevm.swap
args=-c "/etc/ceph/ceph.conf" --id "admin" unmap "/dev/rbd/rbd.ssd/simplevm.swap" event=rbd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
event=acquire-global-lock machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
event=global-lock-acquire machine=simplevm result=locked subsystem=qemu target=/run/fc-qemu.lock
count=1 event=global-lock-status machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
available_real=... bookable=0 event=sufficient-host-memory machine=simplevm required=384 subsystem=qemu
event=start-qemu machine=simplevm subsystem=qemu
additional_args=() event=qemu-system-x86_64 local_args=[\'-daemonize\', \'-nodefaults\', \'-name simplevm,process=kvm.simplevm\', \'-chroot /srv/vm/simplevm\', \'-runas nobody\', \'-serial file:/var/log/vm/simplevm.log\', \'-display vnc=host1:2345\', \'-pidfile /run/qemu.simplevm.pid\', \'-vga std\', \'-m 256\', \'-readconfig /run/qemu.simplevm.cfg\'] machine=simplevm subsystem=qemu
count=0 event=global-lock-status machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
event=global-lock-release machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
event=global-lock-release machine=simplevm result=unlocked subsystem=qemu
arguments={} event=qmp_capabilities id=None machine=simplevm subsystem=qemu/qmp
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
event=consul-register machine=simplevm
arguments={} event=query-block id=None machine=simplevm subsystem=qemu/qmp
action=throttle current_iops=0 device=virtio0 event=ensure-throttle machine=simplevm target_iops=3000
arguments={\'bps_rd\': 0, \'bps_wr\': 0, \'bps\': 0, \'iops\': 3000, \'iops_rd\': 0, \'device\': u\'virtio0\', \'iops_wr\': 0} event=block_set_io_throttle id=None machine=simplevm subsystem=qemu/qmp
action=throttle current_iops=0 device=virtio1 event=ensure-throttle machine=simplevm target_iops=3000
arguments={\'bps_rd\': 0, \'bps_wr\': 0, \'bps\': 0, \'iops\': 3000, \'iops_rd\': 0, \'device\': u\'virtio1\', \'iops_wr\': 0} event=block_set_io_throttle id=None machine=simplevm subsystem=qemu/qmp
action=throttle current_iops=0 device=virtio2 event=ensure-throttle machine=simplevm target_iops=3000
arguments={\'bps_rd\': 0, \'bps_wr\': 0, \'bps\': 0, \'iops\': 3000, \'iops_rd\': 0, \'device\': u\'virtio2\', \'iops_wr\': 0} event=block_set_io_throttle id=None machine=simplevm subsystem=qemu/qmp
action=none event=ensure-watchdog machine=simplevm
arguments={'command-line': 'watchdog_action action=none'} event=human-monitor-command id=None machine=simplevm subsystem=qemu/qmp
count=0 event=lock-status machine=simplevm
event=release-lock machine=simplevm target=/run/qemu.simplevm.lock
event=release-lock machine=simplevm result=unlocked target=/run/qemu.simplevm.lock""")

    util.test_log_options['show_events'] = [
        'vm-status', 'rbd-status', 'disk-throttle']

    vm.status()
    assert get_log() == Ellipsis("""\
event=vm-status machine=simplevm result=online
device=virtio0 event=disk-throttle iops=3000 machine=simplevm
device=virtio1 event=disk-throttle iops=3000 machine=simplevm
device=virtio2 event=disk-throttle iops=3000 machine=simplevm
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.stop()
    get_log()

    vm.status()
    assert get_log() == """\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""


def test_simple_vm_lifecycle_ensure_going_offline(vm, capsys, caplog):
    util.test_log_options['show_events'] = ['vm-status', 'rbd-status', 'ensure-state', 'disk-throttle']
    vm.status()
    assert get_log() == """\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""

    vm.ensure()
    out = get_log()
    assert "action=start event=ensure-state found=offline machine=simplevm wanted=online" in out

    vm.status()
    assert get_log() == Ellipsis("""\
event=vm-status machine=simplevm result=online
device=virtio0 event=disk-throttle iops=3000 machine=simplevm
device=virtio1 event=disk-throttle iops=3000 machine=simplevm
device=virtio2 event=disk-throttle iops=3000 machine=simplevm
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.enc['parameters']['online'] = False
    vm.enc['consul-generation'] += 1
    vm.stage_new_config()
    # As we're re-using the same agent object, we have to time-travel here,
    # otherwise ensure will already think we're on the new generation.
    vm.enc['consul-generation'] -= 1
    vm.ensure()
    assert get_log() == """\
action=stop event=ensure-state found=online machine=simplevm wanted=offline"""

    vm.status()
    assert get_log() == """\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""


def test_vm_not_running_here(vm, capsys):
    util.test_log_options['show_events'] = [
        'vm-status', 'rbd-status']

    vm.status()
    assert get_log() == """\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""

    vm.enc['parameters']['kvm_host'] = 'otherhost'
    vm.enc['consul-generation'] += 1
    vm.stage_new_config()
    vm.enc['consul-generation'] -= 1
    vm.ensure()
    vm.status()
    assert get_log() == Ellipsis("""\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp""")


def test_crashed_vm_clean_restart(vm):
    util.test_log_options['show_events'] = [
        'rbd-status', 'vm-status', 'ensure', 'throttle', 'shutdown']

    vm.status()

    assert get_log() == Ellipsis("""\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.ensure()
    vm.status()
    assert get_log() == Ellipsis("""\
event=running-ensure generation=0 machine=simplevm
action=start event=ensure-state found=offline machine=simplevm wanted=online
...
event=vm-status machine=simplevm result=online
device=virtio0 event=disk-throttle iops=3000 machine=simplevm
device=virtio1 event=disk-throttle iops=3000 machine=simplevm
device=virtio2 event=disk-throttle iops=3000 machine=simplevm
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.qemu.proc().kill()
    vm.qemu.proc().wait(2)
    get_log()

    vm.status()
    assert get_log() == Ellipsis("""\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.ensure()

    vm.status()
    assert get_log() == Ellipsis("""\
event=running-ensure generation=0 machine=simplevm
action=start event=ensure-state found=offline machine=simplevm wanted=online
...
event=vm-status machine=simplevm result=online
device=virtio0 event=disk-throttle iops=3000 machine=simplevm
device=virtio1 event=disk-throttle iops=3000 machine=simplevm
device=virtio2 event=disk-throttle iops=3000 machine=simplevm
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    util.test_log_options['show_events'] = [
        'shutdown', 'kill', 'unlock', 'vm-status', 'consul', 'clean', 'rbd-status']
    vm.stop()
    vm.status()
    assert get_log() == Ellipsis("""\
event=graceful-shutdown machine=simplevm
event=graceful-shutdown-failed machine=simplevm reason=timeout
event=kill-vm machine=simplevm
event=killed-vm machine=simplevm
event=unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
event=unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
event=unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=consul-deregister machine=simplevm
event=clean-run-files machine=simplevm subsystem=qemu
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp""")


def test_do_not_clean_up_crashed_vm_that_doesnt_get_restarted(vm):
    vm.ensure()
    assert vm.qemu.is_running() is True
    vm.qemu.proc().kill()
    vm.qemu.proc().wait(2)
    assert vm.ceph.locked_by_me() is True
    vm.enc['parameters']['online'] = False
    vm.enc['consul-generation'] += 1
    vm.stage_new_config()
    vm.enc['consul-generation'] -= 1
    vm.ensure()
    # We don't really know what's going on here, so, yeah, don't touch it.
    assert vm.ceph.locked_by_me() is True


def test_vm_snapshot_only_if_running(vm):
    assert list(x.fullname for x in vm.ceph.root.snapshots) == []
    vm.ceph.root.ensure_presence()
    vm.snapshot('asdf')
    assert list(x.fullname for x in vm.ceph.root.snapshots) == []


def test_vm_snapshot_with_missing_guest_agent(vm, monkeypatch):
    util.test_log_options['show_events'] = [
        'consul', 'snapshot', 'freeze', 'thaw']

    monkeypatch.setattr(
        util, 'today', lambda: datetime.date(2010, 1, 1))

    assert list(x.fullname for x in vm.ceph.root.snapshots) == []
    vm.ensure()
    get_log()

    with pytest.raises(Exception):
        vm.snapshot('asdf', 7)
    assert Ellipsis("""\
event=snapshot-create machine=simplevm name=asdf-keep-until-20100108
event=freeze machine=simplevm volume=root
action=continue event=freeze-failed machine=simplevm reason=Unable to sync \
with guest agent after 20 tries.
event=snapshot-ignore machine=simplevm reason=not frozen
event=thaw machine=simplevm volume=root
action=retry event=thaw-failed machine=simplevm reason=Unable to sync with \
guest agent after 20 tries.
action=continue event=thaw-failed machine=simplevm reason=Unable to sync \
with guest agent after 20 tries.""") == get_log()

    with pytest.raises(Exception):
        vm.snapshot('asdf', 0)
    assert """\
event=snapshot-create machine=simplevm name=asdf
event=freeze machine=simplevm volume=root
action=continue event=freeze-failed machine=simplevm reason=[Errno 11] Resource temporarily unavailable
event=snapshot-ignore machine=simplevm reason=not frozen
event=thaw machine=simplevm volume=root
action=retry event=thaw-failed machine=simplevm reason=[Errno 11] Resource temporarily unavailable
action=continue event=thaw-failed machine=simplevm reason=[Errno 11] Resource temporarily unavailable\
""" == get_log()


def test_vm_throttle_iops(vm):
    vm.start()
    get_log()

    vm.ensure_online_disk_throttle()
    assert get_log() == """\
arguments={} event=query-block id=None machine=simplevm subsystem=qemu/qmp
action=none current_iops=3000 device=virtio0 event=ensure-throttle machine=simplevm target_iops=3000
action=none current_iops=3000 device=virtio1 event=ensure-throttle machine=simplevm target_iops=3000
action=none current_iops=3000 device=virtio2 event=ensure-throttle machine=simplevm target_iops=3000"""

    vm.cfg['iops'] = 10

    vm.ensure_online_disk_throttle()
    assert get_log() == """\
arguments={} event=query-block id=None machine=simplevm subsystem=qemu/qmp
action=throttle current_iops=3000 device=virtio0 event=ensure-throttle machine=simplevm target_iops=10
arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10, 'iops_rd': 0, 'device': u'virtio0', 'iops_wr': 0} event=block_set_io_throttle id=None machine=simplevm subsystem=qemu/qmp
action=throttle current_iops=3000 device=virtio1 event=ensure-throttle machine=simplevm target_iops=10
arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10, 'iops_rd': 0, 'device': u'virtio1', 'iops_wr': 0} event=block_set_io_throttle id=None machine=simplevm subsystem=qemu/qmp
action=throttle current_iops=3000 device=virtio2 event=ensure-throttle machine=simplevm target_iops=10
arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10, 'iops_rd': 0, 'device': u'virtio2', 'iops_wr': 0} event=block_set_io_throttle id=None machine=simplevm subsystem=qemu/qmp"""

    vm.ensure_online_disk_throttle()
    assert get_log() == """\
arguments={} event=query-block id=None machine=simplevm subsystem=qemu/qmp
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
arguments={'device': 'virtio0', 'size': 10737418240} event=block_resize id=None machine=simplevm subsystem=qemu/qmp"""

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


def test_vm_migration(vm):
    subprocess.check_call('./test-migration.sh', shell=True)


def test_simple_cancelled_migration_doesnt_clean_up(vm, monkeypatch):
    import fc.qemu.outgoing
    monkeypatch.setattr(fc.qemu.outgoing.Outgoing, 'connect_timeout', 2)

    vm.start()
    assert os.path.exists('/run/qemu.simplevm.pid')
    assert vm.ceph.locked_by_me()

    vm.ensure_online_remote()
    assert vm.ceph.locked_by_me()
    assert os.path.exists('/run/qemu.simplevm.pid')
