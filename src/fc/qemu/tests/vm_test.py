import datetime
import os
import os.path
import re
import shutil
import subprocess
import textwrap
import time

import pytest
import yaml

from fc.qemu import util

from ..agent import Agent, InvalidCommand, swap_size, tmp_size
from ..conftest import get_log
from ..ellipsis import Ellipsis
from ..hazmat import qemu
from ..util import GiB, MiB


def clean_output(output):
    output = re.sub(
        r"^[a-zA-Z0-9\-:\. ]+ (I|D) ", "", output, flags=re.MULTILINE
    )
    output = re.sub(r"[\t ]+$", "", output, flags=re.MULTILINE)
    output = re.sub(r"\t", " ", output, flags=re.MULTILINE)
    return output


@pytest.mark.timeout(60)
@pytest.mark.live
def test_simple_vm_lifecycle_start_stop(vm):
    util.test_log_options["show_events"] = ["vm-status", "rbd-status"]

    vm.status()

    status = get_log()
    assert (
        status
        == """\
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )

    util.test_log_options["show_events"] = []
    vm.start()

    out = clean_output(get_log())
    # WORKAROUND: one line might be duplicate in the log output. This is specific
    # pre-processing to allow both cases and could probably be done in a more
    # elegant way.
    possibly_dupl_line = r"qmp_capabilities arguments={} id=None machine=simplevm subsystem=qemu/qmp"
    out_lines = out.split("\n")
    while out_lines.count(possibly_dupl_line) > 1:
        out_lines.remove(possibly_dupl_line)
    out = "\n".join(out_lines)

    # This is 1 end-to-end logging test to see everything.
    # FIXME: decide which lines are really necessary to avoid false test-negatives in
    # case number or order of lines changes
    assert out == Ellipsis(
        """\
acquire-lock machine=simplevm target=/run/qemu.simplevm.lock
acquire-lock machine=simplevm result=locked target=/run/qemu.simplevm.lock
lock-status count=1 machine=simplevm
generate-config machine=simplevm
ensure-root machine=simplevm subsystem=ceph
create-vm machine=simplevm subsystem=ceph
/nix/store/.../bin/fc-create-vm args=-I simplevm machine=simplevm subsystem=ceph
fc-create-vm>
fc-create-vm> Establishing system identity
fc-create-vm> ----------------------------
fc-create-vm> $ rbd --format json --id host1 snap ls rbd.hdd/fc-21.05-dev
fc-create-vm> Snapshots:
fc-create-vm> snapid name size
fc-create-vm> 4 v1 104857600
fc-create-vm> $ rbd --id host1 clone rbd.hdd/fc-21.05-dev@v1 rbd.ssd/simplevm.root
fc-create-vm>
fc-create-vm> Finished
fc-create-vm> --------
/nix/store/.../bin/fc-create-vm machine=simplevm returncode=0 subsystem=ceph
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
ensure-tmp machine=simplevm subsystem=ceph
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd args=-c "/etc/ceph/ceph.conf" --id "host1" map "rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd> /dev/rbd0
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
create-fs machine=simplevm subsystem=ceph type=xfs volume=rbd.ssd/simplevm.tmp
sgdisk args=-o "/dev/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk> Creating new GPT entries in memory.
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk args=-a 8192 -n 1:8192:0 -c "1:tmp" -t 1:8300 "/dev/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk> Setting name!
sgdisk> partNum is 0
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe args=/dev/rbd/rbd.ssd/simplevm.tmp machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
mkfs.xfs args=-q -f -K -m crc=1,finobt=1 -d su=4m,sw=1 -L "tmp" "/dev/rbd/rbd.ssd/simplevm.tmp-part1" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
mkfs.xfs> mkfs.xfs: Specified data stripe unit 8192 is not the same as the volume stripe unit 128
mkfs.xfs> mkfs.xfs: Specified data stripe width 8192 is not the same as the volume stripe width 128
mkfs.xfs> log stripe unit (4194304 bytes) is too large (maximum is 256KiB)
mkfs.xfs> log stripe unit adjusted to 32KiB
mkfs.xfs machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
seed machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
mount args="/dev/rbd/rbd.ssd/simplevm.tmp-part1" "/mnt/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
umount args="/mnt/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd args=-c "/etc/ceph/ceph.conf" --id "host1" unmap "/dev/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
ensure-swap machine=simplevm subsystem=ceph
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd args=-c "/etc/ceph/ceph.conf" --id "host1" map "rbd.ssd/simplevm.swap" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd> /dev/rbd0
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.swap
mkswap args=-f -L "swap" "/dev/rbd/rbd.ssd/simplevm.swap" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
mkswap> Setting up swapspace version 1, size = 1024 MiB (1073737728 bytes)
mkswap> LABEL=swap, UUID=...-...-...-...-...
mkswap machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd args=-c "/etc/ceph/ceph.conf" --id "host1" unmap "/dev/rbd/rbd.ssd/simplevm.swap" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.swap
acquire-global-lock machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
global-lock-acquire machine=simplevm result=locked subsystem=qemu target=/run/fc-qemu.lock
global-lock-status count=1 machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
sufficient-host-memory available_real=... bookable=2000 machine=simplevm required=384 subsystem=qemu
start-qemu machine=simplevm subsystem=qemu
qemu-system-x86_64 additional_args=() local_args=['-nodefaults', '-only-migratable', '-cpu qemu64,enforce', '-name simplevm,process=kvm.simplevm', '-chroot /srv/vm/simplevm', '-runas nobody', '-serial file:/var/log/vm/simplevm.log', '-display vnc=127.0.0.1:2345', '-pidfile /run/qemu.simplevm.pid', '-vga std', '-m 256', '-readconfig /run/qemu.simplevm.cfg'] machine=simplevm subsystem=qemu
global-lock-status count=0 machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
global-lock-release machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
global-lock-release machine=simplevm result=unlocked subsystem=qemu
qmp_capabilities arguments={} id=None machine=simplevm subsystem=qemu/qmp
query-status arguments={} id=None machine=simplevm subsystem=qemu/qmp
consul-register machine=simplevm
query-block arguments={} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio0 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10000, 'iops_rd': 0, 'device': u'virtio0', 'iops_wr': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio1 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10000, 'iops_rd': 0, 'device': u'virtio1', 'iops_wr': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio2 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10000, 'iops_rd': 0, 'device': u'virtio2', 'iops_wr': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-watchdog action=none machine=simplevm
human-monitor-command arguments={'command-line': 'watchdog_action action=none'} id=None machine=simplevm subsystem=qemu/qmp
lock-status count=0 machine=simplevm
release-lock machine=simplevm target=/run/qemu.simplevm.lock
release-lock machine=simplevm result=unlocked target=/run/qemu.simplevm.lock"""
    )

    util.test_log_options["show_events"] = [
        "vm-status",
        "rbd-status",
        "disk-throttle",
    ]

    vm.status()
    assert get_log() == Ellipsis(
        """\
vm-status machine=simplevm result=online
disk-throttle device=virtio0 iops=10000 machine=simplevm
disk-throttle device=virtio1 iops=10000 machine=simplevm
disk-throttle device=virtio2 iops=10000 machine=simplevm
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )

    vm.stop()
    get_log()

    vm.status()
    assert (
        get_log()
        == """\
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )


@pytest.mark.timeout(60)
@pytest.mark.live
def test_simple_vm_lifecycle_ensure_going_offline(vm, capsys, caplog):
    util.test_log_options["show_events"] = [
        "vm-status",
        "rbd-status",
        "ensure-state",
        "disk-throttle",
    ]
    vm.status()
    assert (
        get_log()
        == """\
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )

    vm.ensure()
    out = get_log()
    assert (
        "ensure-state action=start found=offline machine=simplevm wanted=online"
        in out
    )

    vm.status()
    assert get_log() == Ellipsis(
        """\
vm-status machine=simplevm result=online
disk-throttle device=virtio0 iops=10000 machine=simplevm
disk-throttle device=virtio1 iops=10000 machine=simplevm
disk-throttle device=virtio2 iops=10000 machine=simplevm
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )

    vm.enc["parameters"]["online"] = False
    vm.enc["consul-generation"] += 1
    vm.stage_new_config()
    # As we're re-using the same agent object, we have to time-travel here,
    # otherwise ensure will already think we're on the new generation.
    vm.enc["consul-generation"] -= 1
    vm.ensure()
    assert (
        get_log()
        == """\
ensure-state action=stop found=online machine=simplevm wanted=offline"""
    )

    vm.status()
    assert (
        get_log()
        == """\
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )


@pytest.mark.live
def test_vm_not_running_here(vm, capsys):
    util.test_log_options["show_events"] = ["vm-status", "rbd-status"]

    vm.status()
    assert (
        get_log()
        == """\
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )

    vm.enc["parameters"]["kvm_host"] = "otherhost"
    vm.enc["consul-generation"] += 1
    vm.stage_new_config()
    vm.enc["consul-generation"] -= 1
    vm.ensure()
    vm.status()
    assert get_log() == Ellipsis(
        """\
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )


@pytest.mark.timeout(60)
@pytest.mark.live
def test_crashed_vm_clean_restart(vm):
    util.test_log_options["show_events"] = [
        "rbd-status",
        "vm-status",
        "ensure",
        "throttle",
        "shutdown",
    ]

    vm.status()

    assert get_log() == Ellipsis(
        """\
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )

    vm.ensure()
    vm.status()
    assert get_log() == Ellipsis(
        """\
running-ensure generation=0 machine=simplevm
ensure-state action=start found=offline machine=simplevm wanted=online
...
vm-status machine=simplevm result=online
disk-throttle device=virtio0 iops=10000 machine=simplevm
disk-throttle device=virtio1 iops=10000 machine=simplevm
disk-throttle device=virtio2 iops=10000 machine=simplevm
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )

    p = vm.qemu.proc()
    p.kill()
    p.wait(2)
    get_log()

    vm.status()
    assert get_log() == Ellipsis(
        """\
vm-status machine=simplevm result=offline
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )

    vm.ensure()

    vm.status()
    assert get_log() == Ellipsis(
        """\
running-ensure generation=0 machine=simplevm
ensure-state action=start found=offline machine=simplevm wanted=online
...
vm-status machine=simplevm result=online
disk-throttle device=virtio0 iops=10000 machine=simplevm
disk-throttle device=virtio1 iops=10000 machine=simplevm
disk-throttle device=virtio2 iops=10000 machine=simplevm
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=(u'client...', u'host1') machine=simplevm volume=rbd.ssd/simplevm.tmp"""
    )

    util.test_log_options["show_events"] = [
        "shutdown",
        "kill",
        "unlock",
        "vm-status",
        "consul",
        "clean",
        "rbd-status",
    ]
    vm.stop()
    vm.status()
    assert get_log() == Ellipsis(
        """\
graceful-shutdown machine=simplevm
graceful-shutdown-failed machine=simplevm reason=timeout
kill-vm machine=simplevm
killed-vm machine=simplevm
unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
consul-deregister machine=simplevm
clean-run-files machine=simplevm subsystem=qemu
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm volume=rbd.ssd/simplevm.tmp
consul machine=simplevm service=<not registered>"""
    )


@pytest.mark.timeout(60)
@pytest.mark.live
def test_do_not_clean_up_crashed_vm_that_doesnt_get_restarted(vm):
    vm.ensure()
    assert vm.qemu.is_running() is True
    vm.qemu.proc().kill()
    vm.qemu.proc().wait(2)
    assert vm.ceph.locked_by_me() is True
    vm.enc["parameters"]["online"] = False
    vm.enc["consul-generation"] += 1
    vm.stage_new_config()
    vm.enc["consul-generation"] -= 1
    vm.ensure()
    # We don't really know what's going on here, so, yeah, don't touch it.
    assert vm.ceph.locked_by_me() is True


@pytest.mark.live
@pytest.mark.timeout(60)
def test_vm_snapshot_only_if_running(vm):
    assert list(x.fullname for x in vm.ceph.root.snapshots) == []
    vm.ceph.root.ensure_presence()
    with pytest.raises(InvalidCommand):
        vm.snapshot("asdf")


@pytest.mark.timeout(60)
@pytest.mark.live
def test_vm_snapshot_with_missing_guest_agent(vm, monkeypatch):
    util.test_log_options["show_events"] = [
        "consul",
        "snapshot",
        "freeze",
        "thaw",
    ]

    monkeypatch.setattr(util, "today", lambda: datetime.date(2010, 1, 1))

    monkeypatch.setattr(qemu, "FREEZE_TIMEOUT", 1)

    assert list(x.fullname for x in vm.ceph.root.snapshots) == []
    vm.ensure()
    get_log()

    with pytest.raises(Exception):
        vm.snapshot("asdf", 7)
    assert (
        Ellipsis(
            """\
snapshot-create machine=simplevm name=asdf-keep-until-20100108
freeze machine=simplevm volume=root
freeze-failed action=continue machine=simplevm reason=Unable to sync with guest agent after 10 tries.
snapshot-ignore machine=simplevm reason=not frozen
ensure-thawed machine=simplevm volume=root
guest-fsfreeze-thaw-failed exc_info=True machine=simplevm subsystem=qemu
ensure-thawed-failed machine=simplevm reason=Unable to sync with guest agent after 10 tries."""
        )
        == get_log()
    )

    with pytest.raises(Exception):
        vm.snapshot("asdf", 0)
    assert (
        Ellipsis(
            """\
snapshot-create machine=simplevm name=asdf
freeze machine=simplevm volume=root
freeze-failed action=continue machine=simplevm reason=...
snapshot-ignore machine=simplevm reason=not frozen
ensure-thawed machine=simplevm volume=root
guest-fsfreeze-thaw-failed exc_info=True machine=simplevm subsystem=qemu
ensure-thawed-failed machine=simplevm reason=Unable to sync with guest agent after 10 tries."""
        )
        == get_log()
    )


@pytest.mark.timeout(60)
@pytest.mark.live
def test_vm_throttle_iops(vm):
    vm.start()
    get_log()

    vm.ensure_online_disk_throttle()
    assert (
        get_log()
        == """\
query-block arguments={} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=none current_iops=10000 device=virtio0 machine=simplevm target_iops=10000
ensure-throttle action=none current_iops=10000 device=virtio1 machine=simplevm target_iops=10000
ensure-throttle action=none current_iops=10000 device=virtio2 machine=simplevm target_iops=10000"""
    )

    vm.cfg["iops"] = 10

    vm.ensure_online_disk_throttle()
    assert (
        get_log()
        == """\
query-block arguments={} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=10000 device=virtio0 machine=simplevm target_iops=10
block_set_io_throttle arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10, 'iops_rd': 0, 'device': u'virtio0', 'iops_wr': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=10000 device=virtio1 machine=simplevm target_iops=10
block_set_io_throttle arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10, 'iops_rd': 0, 'device': u'virtio1', 'iops_wr': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=10000 device=virtio2 machine=simplevm target_iops=10
block_set_io_throttle arguments={'bps_rd': 0, 'bps_wr': 0, 'bps': 0, 'iops': 10, 'iops_rd': 0, 'device': u'virtio2', 'iops_wr': 0} id=None machine=simplevm subsystem=qemu/qmp"""
    )

    vm.ensure_online_disk_throttle()
    assert (
        get_log()
        == """\
query-block arguments={} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=none current_iops=10 device=virtio0 machine=simplevm target_iops=10
ensure-throttle action=none current_iops=10 device=virtio1 machine=simplevm target_iops=10
ensure-throttle action=none current_iops=10 device=virtio2 machine=simplevm target_iops=10"""
    )


@pytest.mark.timeout(60)
@pytest.mark.live
def test_vm_resize_disk(vm):
    vm.start()
    get_log()

    # The cloned image is smaller than the initial desired
    # disk so we immediately get a resize.
    vm.ensure_online_disk_size()
    assert (
        get_log()
        == """\
check-disk-size action=resize found=104857600 machine=simplevm wanted=2147483648
block_resize arguments={'device': 'virtio0', 'size': 2147483648} id=None machine=simplevm subsystem=qemu/qmp"""
    )

    # Increasing the desired disk size also triggers a change.
    vm.cfg["disk"] *= 2
    vm.ensure_online_disk_size()
    assert (
        get_log()
        == """\
check-disk-size action=resize found=2147483648 machine=simplevm wanted=4294967296
block_resize arguments={'device': 'virtio0', 'size': 4294967296} id=None machine=simplevm subsystem=qemu/qmp"""
    )

    # The disk image is of the right size and thus nothing happens.
    vm.ensure_online_disk_size()
    assert (
        get_log()
        == """\
check-disk-size action=none found=4294967296 machine=simplevm wanted=4294967296"""
    )


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


@pytest.yield_fixture
def kill_vms():
    def _kill_vms():
        subprocess.call("pkill -f qemu", shell=True)
        subprocess.call(
            "ssh -oStrictHostKeyChecking=no -i/etc/ssh_key host2 'pkill -f qemu'",
            shell=True,
        )
        subprocess.call("fc-qemu force-unlock simplevm", shell=True)

    _kill_vms()
    yield
    _kill_vms()


@pytest.mark.live
@pytest.mark.timeout(120)
def test_vm_migration(vm, kill_vms):
    def call(cmd, wait=True, host=None):
        for ssh_cmd in ["scp", "ssh"]:
            if not cmd.startswith(ssh_cmd):
                continue
            cmd = cmd.replace(
                ssh_cmd,
                ssh_cmd + " -oStrictHostKeyChecking=no -i/etc/ssh_key ",
                1,
            )
            break
        print("Starting command `{}`".format(cmd))
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
        if wait:
            stdout, _ = p.communicate()
            print(stdout)
            return clean_output(stdout)
        return p

    def communicate_progress(p):
        stdout = ""
        while True:
            line = subprocess._eintr_retry_call(p.stdout.readline)
            if line:
                # This ensures we get partial output in case of test failures
                print(line.strip())
                stdout += line
            else:
                p.wait()
                return clean_output(stdout)

    call("fc-qemu start simplevm")
    call("sed -i -e 's/host1/host2/' /etc/qemu/vm/simplevm.cfg")
    call("scp /etc/qemu/vm/simplevm.cfg host2:/etc/qemu/vm/")

    inmigrate = call("ssh host2 'fc-qemu -v inmigrate simplevm'", wait=False)
    outmigrate = call("fc-qemu outmigrate simplevm", wait=False)

    outmigrate_result = communicate_progress(outmigrate)
    assert outmigrate_result == Ellipsis(
        """\
simplevm             outmigrate
simplevm             locate-inmigration-service
simplevm             located-inmigration-service    url='http://host2.mgm.test.gocept.net:...'
simplevm             acquire-migration-locks
simplevm             unlock                         subsystem='ceph' volume='rbd.ssd/simplevm.root'
simplevm             unlock                         subsystem='ceph' volume='rbd.ssd/simplevm.swap'
simplevm             unlock                         subsystem='ceph' volume='rbd.ssd/simplevm.tmp'
simplevm             prepare-remote-environment
simplevm             start-migration                target='tcp:192.168.4.7:...'
simplevm             migration-status               mbps='-' remaining='0' status=u'setup'
simplevm> {u'blocked': False, u'status': u'setup'}
simplevm             migration-status               mbps=... remaining='0' status=u'completed'
simplevm> {u'blocked': False,
simplevm>  u'downtime': ...,
simplevm>  u'ram': {u'dirty-pages-rate': ...,
simplevm>           u'dirty-sync-count': ...,
simplevm>           u'duplicate': ...,
simplevm>           u'mbps': ...,
simplevm>           u'multifd-bytes': 0,
simplevm>           u'normal': ...,
simplevm>           u'normal-bytes': ...,
simplevm>           u'page-size': 4096,
simplevm>           u'pages-per-second': ...,
simplevm>           u'postcopy-requests': 0,
simplevm>           u'remaining': 0,
simplevm>           u'skipped': 0,
simplevm>           u'total': ...,
simplevm>           u'transferred': ...},
simplevm>  u'setup-time': ...,
simplevm>  u'status': u'completed',
simplevm>  u'total-time': ...}
simplevm             finish-migration
simplevm             consul-deregister
simplevm             outmigrate-finished            exitcode=0
"""
    )
    assert outmigrate.returncode == 0

    inmigrate_result = communicate_progress(inmigrate)
    assert inmigrate_result == Ellipsis(
        """\
/nix/store/.../bin/fc-qemu -v inmigrate simplevm
load-system-config
simplevm             connect-rados                  subsystem='ceph'
simplevm             acquire-lock                   target='/run/qemu.simplevm.lock'
simplevm             acquire-lock                   result='locked' target='/run/qemu.simplevm.lock'
simplevm             lock-status                    count=1
simplevm             inmigrate
simplevm             start-server                   type='incoming' url='http://host2.mgm.test.gocept.net:.../'
simplevm             setup-incoming-api             cookie='...'
simplevm             consul-register-inmigrate
simplevm             received-ping                  timeout=60
simplevm             waiting                        interval=0 remaining=...
simplevm             received-ping                  timeout=...
simplevm             waiting                        interval=0 remaining=...
simplevm             received-acquire-migration-lock
simplevm             acquire-migration-lock         result='success' subsystem='qemu'
simplevm             reset-timeout
simplevm             waiting                        interval=0 remaining=...
simplevm             received-ping                  timeout=60
simplevm             waiting                        interval=0 remaining=...
simplevm             received-acquire-ceph-locks
simplevm             lock                           subsystem='ceph' volume='rbd.ssd/simplevm.root'
simplevm             lock                           subsystem='ceph' volume='rbd.ssd/simplevm.swap'
simplevm             lock                           subsystem='ceph' volume='rbd.ssd/simplevm.tmp'
simplevm             reset-timeout
simplevm             waiting                        interval=0 remaining=...
simplevm             received-prepare-incoming
simplevm             acquire-global-lock            subsystem='qemu' target='/run/fc-qemu.lock'
simplevm             global-lock-acquire            result='locked' subsystem='qemu' target='/run/fc-qemu.lock'
simplevm             global-lock-status             count=1 subsystem='qemu' target='/run/fc-qemu.lock'
simplevm             sufficient-host-memory         available_real=... bookable=2000 required=768 subsystem='qemu'
simplevm             start-qemu                     subsystem='qemu'
simplevm             qemu-system-x86_64             additional_args=['-incoming tcp:192.168.4.7:...'] local_args=['-nodefaults', '-only-migratable', '-cpu qemu64,enforce', '-name simplevm,process=kvm.simplevm', '-chroot /srv/vm/simplevm', '-runas nobody', '-serial file:/var/log/vm/simplevm.log', '-display vnc=127.0.0.1:2345', '-pidfile /run/qemu.simplevm.pid', '-vga std', '-m 256', '-readconfig /run/qemu.simplevm.cfg'] subsystem='qemu'
simplevm             global-lock-status             count=0 subsystem='qemu' target='/run/fc-qemu.lock'
simplevm             global-lock-release            subsystem='qemu' target='/run/fc-qemu.lock'
simplevm             global-lock-release            result='unlocked' subsystem='qemu'
simplevm             qmp_capabilities               arguments={} id=None subsystem='qemu/qmp'
simplevm             query-status                   arguments={} id=None subsystem='qemu/qmp'
simplevm             reset-timeout
simplevm             waiting                        interval=0 remaining=...
simplevm             received-ping                  timeout=60
simplevm             waiting                        interval=0 remaining=...
simplevm             received-ping                  timeout=60
simplevm             waiting                        interval=0 remaining=...
simplevm             received-finish-incoming
simplevm             query-status                   arguments={} id=None subsystem='qemu/qmp'
simplevm             reset-timeout
simplevm             consul-deregister-inmigrate
simplevm             stop-server                    result='success' type='incoming'
simplevm             consul-register
simplevm             inmigrate-finished             exitcode=0
simplevm             lock-status                    count=0
simplevm             release-lock                   target='/run/qemu.simplevm.lock'
simplevm             release-lock                   result='unlocked' target='/run/qemu.simplevm.lock'
"""
    )
    assert inmigrate.returncode == 0

    # The consul check is a bit flaky as it only checks every 5 seconds
    # and I've seen the test be unreliable.
    time.sleep(6)

    local_status = call("fc-qemu status simplevm")
    assert local_status == Ellipsis(
        """\
simplevm             vm-status                      result='offline'
simplevm             rbd-status                     locker=(u'client...', u'host2') volume='rbd.ssd/simplevm.root'
simplevm             rbd-status                     locker=(u'client...', u'host2') volume='rbd.ssd/simplevm.swap'
simplevm             rbd-status                     locker=(u'client...', u'host2') volume='rbd.ssd/simplevm.tmp'
simplevm             consul                         address=u'host2' service=u'qemu-simplevm'
"""
    )

    remote_status = call("ssh host2 'fc-qemu status simplevm'")
    assert remote_status == Ellipsis(
        """\
simplevm             vm-status                      result='online'
simplevm             disk-throttle                  device=u'virtio0' iops=0
simplevm             disk-throttle                  device=u'virtio1' iops=0
simplevm             disk-throttle                  device=u'virtio2' iops=0
simplevm             rbd-status                     locker=(u'client...', u'host2') volume='rbd.ssd/simplevm.root'
simplevm             rbd-status                     locker=(u'client...', u'host2') volume='rbd.ssd/simplevm.swap'
simplevm             rbd-status                     locker=(u'client...', u'host2') volume='rbd.ssd/simplevm.tmp'
simplevm             consul                         address=u'host2' service=u'qemu-simplevm'
"""
    )


@pytest.mark.timeout(60)
@pytest.mark.live
def test_simple_cancelled_migration_doesnt_clean_up(vm, monkeypatch):
    import fc.qemu.outgoing

    monkeypatch.setattr(fc.qemu.outgoing.Outgoing, "connect_timeout", 2)

    vm.start()
    assert os.path.exists("/run/qemu.simplevm.pid")
    assert vm.ceph.locked_by_me()

    vm.ensure_online_remote()
    assert vm.ceph.locked_by_me()
    assert os.path.exists("/run/qemu.simplevm.pid")


@pytest.mark.timeout(60)
@pytest.mark.live
def test_new_vm(vm):
    # A new VM gets created by consul adding the staging filename and then
    # starting it. At this point the main config file doesn't exist yet.
    shutil.copy(
        "/etc/qemu/vm/simplevm.cfg", "/etc/qemu/vm/.simplevm.cfg.staging"
    )
    os.unlink("/etc/qemu/vm/simplevm.cfg")
    # Include testing the agent setup for this scenario.
    vm = Agent("simplevm")
    with vm:
        vm.ensure()
    assert get_log() == Ellipsis(
        """\
...
running-ensure generation=-1 machine=simplevm
...
update-check action=update current=-1 machine=simplevm result=update-available update=0
...
ensure-state action=start found=offline machine=simplevm wanted=online
...
generate-config machine=simplevm
ensure-root machine=simplevm subsystem=ceph
create-vm machine=simplevm subsystem=ceph
...
"""
    )


@pytest.mark.timeout(60)
@pytest.mark.live
def test_create_vm_shows_error(vm, tmpdir):
    # A new VM gets created by consul adding the staging filename and then
    # starting it. At this point the main config file doesn't exist yet.
    with open("/etc/qemu/vm/simplevm.cfg", "r") as f:
        config = yaml.safe_load(f)
        config["parameters"]["environment"] = "does-not-exist"
    with open("/etc/qemu/vm/.simplevm.cfg.staging", "w") as f:
        f.write(yaml.dump(config))
    os.unlink("/etc/qemu/vm/simplevm.cfg")
    # Include testing the agent setup for this scenario.
    vm = Agent("simplevm")
    with vm:
        with pytest.raises(subprocess.CalledProcessError):
            vm.ensure()
    assert (
        '$ rbd --format json --id host1 snap ls rbd.hdd/does-not-exist'
        in get_log()
    )


@pytest.mark.timeout(60)
@pytest.mark.live
def test_agent_check(vm, capsys):
    util.test_log_options["show_events"] = ["vm-status", "rbd-status"]
    vm.start()

    assert Agent.check() == 0

    captured = capsys.readouterr()

    assert str(captured.out) == Ellipsis(
        textwrap.dedent(
            """\
        ...
        OK - 1 VMs - ... MiB used - 768 MiB expected
        """
        )
    )
