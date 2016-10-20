from ..agent import swap_size, tmp_size
from ..conftest import get_log
from ..ellipsis import Ellipsis
from ..util import MiB, GiB
import subprocess


def test_simple_vm_lifecycle_start_stop(vm):

    vm.status()

    status = get_log()
    assert status == """\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""

    vm.start()

    out = get_log()
    assert out == Ellipsis("""\
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
args= event=partprobe machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
args=-q -f -L "tmp" "/dev/rbd/rbd.ssd/simplevm.tmp-part1" event=mkfs.xfs machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=mkfs.xfs machine=simplevm output=log stripe unit (4194304 bytes) is too large (maximum is 256KiB)
log stripe unit adjusted to 32KiB subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=seed volume=simplevm.tmp
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
event=start-qemu machine=simplevm subsystem=qemu
additional_args=() event=qemu-system-x86_64 local_args=[\'-daemonize\', \'-nodefaults\', \'-name simplevm,process=kvm.simplevm\', \'-chroot /srv/vm/simplevm\', \'-runas nobody\', \'-serial file:/var/log/vm/simplevm.log\', \'-display vnc=host1:2345\', \'-pidfile /run/qemu.simplevm.pid\', \'-vga std\', \'-m 256\', \'-watchdog i6300esb\', \'-watchdog-action reset\', \'-readconfig /run/qemu.simplevm.cfg\'] machine=simplevm subsystem=qemu
arguments={} event=qmp_capabilities id=None machine=simplevm subsystem=qemu/qmp
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
arguments={} event=query-block id=None machine=simplevm subsystem=qemu/qmp
action=throttle current_iops=0 device=virtio0 event=ensure-throttle machine=simplevm target_iops=3000
arguments={\'bps_rd\': 0, \'bps_wr\': 0, \'bps\': 0, \'iops\': 3000, \'iops_rd\': 0, \'device\': u\'virtio0\', \'iops_wr\': 0} event=block_set_io_throttle id=None machine=simplevm subsystem=qemu/qmp
action=throttle current_iops=0 device=virtio1 event=ensure-throttle machine=simplevm target_iops=3000
arguments={\'bps_rd\': 0, \'bps_wr\': 0, \'bps\': 0, \'iops\': 3000, \'iops_rd\': 0, \'device\': u\'virtio1\', \'iops_wr\': 0} event=block_set_io_throttle id=None machine=simplevm subsystem=qemu/qmp
action=throttle current_iops=0 device=virtio2 event=ensure-throttle machine=simplevm target_iops=3000
arguments={\'bps_rd\': 0, \'bps_wr\': 0, \'bps\': 0, \'iops\': 3000, \'iops_rd\': 0, \'device\': u\'virtio2\', \'iops_wr\': 0} event=block_set_io_throttle id=None machine=simplevm subsystem=qemu/qmp
event=register-consul machine=simplevm""")

    vm.status()
    assert get_log() == Ellipsis("""\
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
event=vm-status machine=simplevm result=online
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
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
event=vm-status machine=simplevm result=online
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.cfg['online'] = False
    vm.save_enc()
    vm.ensure()
    get_log()
    vm.status()
    assert get_log() == """\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""


def test_vm_not_running_here(vm, capsys):
    vm.status()
    assert get_log() == """\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""

    vm.cfg['kvm_host'] = 'otherhost'
    vm.ensure()
    vm.status()
    assert get_log() == """\
ceph_lock=False event=check-state-consistency is_consistent=True machine=simplevm proc=False qemu=False
event=purge-run-files machine=simplevm subsystem=qemu
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""


def test_crashed_vm_clean_restart(vm):
    vm.status()

    assert get_log() == Ellipsis("""\
event=vm-status machine=simplevm result=offline
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.ensure()
    vm.status()
    assert get_log() == Ellipsis("""\
action=start event=ensure-state found=offline machine=simplevm wanted=online
...
event=vm-status machine=simplevm result=online
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.qemu.proc().kill()
    vm.qemu.proc().wait(2)
    get_log()

    vm.status()
    assert get_log() == Ellipsis("""\
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
event=vm-status machine=simplevm result=offline
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.ensure()

    vm.status()
    assert get_log() == Ellipsis("""\
action=start event=ensure-state found=offline machine=simplevm wanted=online
...
event=vm-status machine=simplevm result=online
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.root
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
event=rbd-status locker=('client...', 'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp""")

    vm.stop()
    vm.status()
    assert get_log() == Ellipsis("""\
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
event=graceful-shutdown machine=simplevm
arguments={'keys': [{'data': 'ctrl', 'type': 'qcode'}, {'data': 'alt', 'type': 'qcode'}, {'data': 'delete', 'type': 'qcode'}]} event=send-key id=None machine=simplevm subsystem=qemu/qmp
event=graceful-shutdown-failed machine=simplevm reason=timeout
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
event=kill-vm machine=simplevm
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
event=killed-vm machine=simplevm
event=unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
event=unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
event=unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
event=purge-run-files machine=simplevm subsystem=qemu
event=deregister-consul machine=simplevm
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
    vm.cfg['online'] = False
    vm.ensure()
    # We don't really know what's going on here, so, yeah, don't touch it.
    assert vm.ceph.locked_by_me() is True


def test_simple_vm_snapshot(vm):
    assert list(x.fullname for x in vm.ceph.root.snapshots) == []
    vm.ceph.root.ensure_presence()
    vm.snapshot('asdf')
    assert list(x.fullname for x in vm.ceph.root.snapshots) == [
        'rbd.ssd/simplevm.root@asdf']


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
