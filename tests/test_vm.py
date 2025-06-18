import datetime
import json
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
from fc.qemu.agent import Agent, InvalidCommand, swap_size, tmp_size
from fc.qemu.util import GiB, MiB
from tests.conftest import get_log
from tests.ellipsis import Ellipsis


def clean_output(output):
    output = re.sub(
        r"^[a-zA-Z0-9\-:\. ]+ (E|C|I|D|W) ", "", output, flags=re.MULTILINE
    )
    output = re.sub(r"[\t ]+$", "", output, flags=re.MULTILINE)
    output = re.sub(r"\t", " ", output, flags=re.MULTILINE)
    return output


def test_clean_output():
    assert (
        clean_output(
            """
2025-01-23T18:09:22.747052 561232 D simplevm              waiting                        interval=3 remaining=3599
2025-01-23T18:09:25.747839 561232 D simplevm              check-staging-config           result='none'
2025-01-23T18:09:25.749856 561232 W simplevm              multiple-services-found        action='trying newest first' service='vm-inmigrate-simplevm'
2025-01-23T18:09:25.749912 561232 I simplevm              located-inmigration-service    url='http://host2.srv.test.gocept.net:32849'
2025-01-23T18:09:25.751980 561232 D simplevm              started-heartbeat-ping
2025-01-23T18:09:25.752032 561232 D simplevm              heartbeat-ping
2025-01-23T18:09:25.752188 561232 I simplevm              acquire-migration-locks
2025-01-23T18:09:25.752293 561232 D simplevm              check-staging-config           result='none'
2025-01-23T18:09:25.752418 561232 D simplevm         qemu acquire-migration-lock         result='success'
2025-01-23T18:09:25.752455 561232 D simplevm              acquire-local-migration-lock   result='success'
2025-01-23T18:09:25.752481 561232 D simplevm              acquire-remote-migration-lock
2025-01-23T18:09:25.754060 561232 D simplevm              acquire-remote-migration-lock  result='success
"""
        )
        == """
simplevm              waiting                        interval=3 remaining=3599
simplevm              check-staging-config           result='none'
simplevm              multiple-services-found        action='trying newest first' service='vm-inmigrate-simplevm'
simplevm              located-inmigration-service    url='http://host2.srv.test.gocept.net:32849'
simplevm              started-heartbeat-ping
simplevm              heartbeat-ping
simplevm              acquire-migration-locks
simplevm              check-staging-config           result='none'
simplevm         qemu acquire-migration-lock         result='success'
simplevm              acquire-local-migration-lock   result='success'
simplevm              acquire-remote-migration-lock
simplevm              acquire-remote-migration-lock  result='success
"""
    )


TEST_FIRST_START_VARIATION = """\
acquire-lock machine=simplevm target=/run/qemu.simplevm.lock
acquire-lock count=1 machine=simplevm result=locked target=/run/qemu.simplevm.lock
pre-start machine=simplevm subsystem=ceph volume_spec=root
ensure-presence machine=simplevm subsystem=ceph volume_spec=root
create-vm machine=simplevm subsystem=ceph volume=simplevm.root
/nix/store/zggynl2zs6m9swyqklqf0gr1dnga3dqx-python3.12-fc-agent-1.0/bin/fc-create-vm args=-I simplevm machine=simplevm subsystem=ceph volume=simplevm.root
fc-create-vm>
fc-create-vm> Establishing system identity
fc-create-vm> ----------------------------
fc-create-vm> $ rbd --format json --id host1 snap ls rbd.hdd/fc-21.05-dev
fc-create-vm> Snapshots:
fc-create-vm> snapid name size
fc-create-vm> 4 v1 524288000
fc-create-vm> $ rbd --id host1 clone rbd.hdd/fc-21.05-dev@v1 rbd.ssd/simplevm.root
fc-create-vm>
fc-create-vm> Finished
fc-create-vm> --------
/nix/store/zggynl2zs6m9swyqklqf0gr1dnga3dqx-python3.12-fc-agent-1.0/bin/fc-create-vm machine=simplevm returncode=0 subsystem=ceph volume=simplevm.root
partprobe args=/dev/rbd/rbd.ssd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
waiting interval=0 machine=simplevm remaining=4 subsystem=ceph volume=rbd.ssd/simplevm.root
blkid args=/dev/rbd/rbd.ssd/simplevm.root-part1 -o export machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
blkid> DEVNAME=/dev/rbd/rbd.ssd/simplevm.root-part1
blkid> UUID=3337fc07-3c0e-47d2-9c99-9c5d5345f7ba
blkid> BLOCK_SIZE=512
blkid> TYPE=xfs
blkid> PARTLABEL=ROOT
blkid> PARTUUID=d54bed04-c010-44de-a2ce-53390999f51a
blkid machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
partprobe args=/dev/rbd/rbd.ssd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
mount args="/dev/rbd/rbd.ssd/simplevm.root-part1" "/mnt/rbd/rbd.ssd/simplevm.root" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
umount args="/mnt/rbd/rbd.ssd/simplevm.root" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
regenerate-xfs-uuid device=/dev/rbd/rbd.ssd/simplevm.root-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
xfs_db args=-x -c 'uuid generate' /dev/rbd/rbd.ssd/simplevm.root-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
xfs_db> Clearing log and setting UUID
xfs_db> writing all SBs
xfs_db> new UUID = 117c71ff-ab7e-4573-8f98-d9e1b3bc6abb
xfs_db machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
ensure-size machine=simplevm subsystem=ceph volume_spec=root
start machine=simplevm subsystem=ceph volume_spec=root
start-root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
root-found-in current_pool=rbd.ssd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
pre-start machine=simplevm subsystem=ceph volume_spec=swap
ensure-presence machine=simplevm subsystem=ceph volume_spec=swap
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
ensure-size machine=simplevm subsystem=ceph volume_spec=swap
start machine=simplevm subsystem=ceph volume_spec=swap
start-swap machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
mkswap args=-f -L "swap" /dev/rbd/rbd.ssd/simplevm.swap machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
mkswap> Setting up swapspace version 1, size = 1024 MiB (1073737728 bytes)
mkswap> LABEL=swap, UUID=2ed4bc8c-77af-4eed-b044-fb8678b34210
mkswap machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.swap
pre-start machine=simplevm subsystem=ceph volume_spec=tmp
ensure-presence machine=simplevm subsystem=ceph volume_spec=tmp
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
ensure-size machine=simplevm subsystem=ceph volume_spec=tmp
start machine=simplevm subsystem=ceph volume_spec=tmp
start-tmp machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
create-fs machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk args=-o "/dev/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk> Creating new GPT entries in memory.
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk args=-a 8192 -n 1:8192:0 -c "1:tmp" -t 1:8300 "/dev/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe args=/dev/rbd/rbd.ssd/simplevm.tmp machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
waiting interval=0 machine=simplevm remaining=4 subsystem=ceph volume=rbd.ssd/simplevm.tmp
mkfs.xfs args=-q -f -K -m crc=1,finobt=1 -d su=4m,sw=1 -L "tmp" /dev/rbd/rbd.ssd/simplevm.tmp-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
mkfs.xfs> mkfs.xfs: Specified data stripe unit 8192 is not the same as the volume stripe unit 128
mkfs.xfs> mkfs.xfs: Specified data stripe width 8192 is not the same as the volume stripe width 128
mkfs.xfs> log stripe unit (4194304 bytes) is too large (maximum is 256KiB)
mkfs.xfs> log stripe unit adjusted to 32KiB
mkfs.xfs machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
seed machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe args=/dev/rbd/rbd.ssd/simplevm.tmp machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
mount args="/dev/rbd/rbd.ssd/simplevm.tmp-part1" "/mnt/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
guest-properties machine=simplevm properties={'binary_generation': 2, 'rbd_pool': 'rbd.ssd'} subsystem=ceph volume=rbd.ssd/simplevm.tmp
binary-generation generation=2 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
umount args="/mnt/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
pre-start machine=simplevm subsystem=ceph volume_spec=cidata
ensure-presence machine=simplevm subsystem=ceph volume_spec=cidata
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
ensure-size machine=simplevm subsystem=ceph volume_spec=cidata
start machine=simplevm subsystem=ceph volume_spec=cidata
start-cloud-init machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
create-fs machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
sgdisk args=-o "/dev/rbd/rbd.ssd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
sgdisk> Creating new GPT entries in memory.
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
sgdisk args=-n 1:: -c "1:cidata" -t 1:8300 "/dev/rbd/rbd.ssd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
partprobe args=/dev/rbd/rbd.ssd/simplevm.cidata machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
waiting interval=0 machine=simplevm remaining=4 subsystem=ceph volume=rbd.ssd/simplevm.cidata
mkfs.vfat args=-n "cidata" /dev/rbd/rbd.ssd/simplevm.cidata-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
mkfs.vfat> mkfs.fat: Warning: lowercase labels might not work properly on some systems
mkfs.vfat> mkfs.fat 4.2 (2021-01-31)
mkfs.vfat machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
seed machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
generate-config machine=simplevm
acquire-global-lock machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
global-lock-acquire machine=simplevm result=locked subsystem=qemu target=/run/fc-qemu.lock
global-lock-status count=1 machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
sufficient-host-memory available_real=10769.54296875 bookable=2000 machine=simplevm required=384 subsystem=qemu
start-qemu machine=simplevm subsystem=qemu
qemu-system-x86_64 additional_args=() local_args=['-nodefaults', '-only-migratable', '-cpu qemu64,enforce', '-name simplevm,process=kvm.simplevm', '-chroot /srv/vm/simplevm', '-runas nobody', '-serial file:/var/log/vm/simplevm.log', '-display vnc=127.0.0.1:2345', '-pidfile /run/qemu.simplevm.pid', '-vga std', '-m 256', '-readconfig /run/qemu.simplevm.cfg'] machine=simplevm subsystem=qemu
exec cmd=supervised-qemu qemu-system-x86_64 -nodefaults -only-migratable -cpu qemu64,enforce -name simplevm,process=kvm.simplevm -chroot /srv/vm/simplevm -runas nobody -serial file:/var/log/vm/simplevm.log -display vnc=127.0.0.1:2345 -pidfile /run/qemu.simplevm.pid -vga std -m 256 -readconfig /run/qemu.simplevm.cfg -D /var/log/vm/simplevm.qemu.internal.log simplevm /var/log/vm/simplevm.supervisor.log machine=simplevm subsystem=qemu
supervised-qemu-stdout machine=simplevm subsystem=qemu
supervised-qemu-stderr machine=simplevm subsystem=qemu
global-lock-status count=0 machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
global-lock-release machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
global-lock-release machine=simplevm result=unlocked subsystem=qemu
qmp_capabilities arguments={} id=None machine=simplevm subsystem=qemu/qmp
query-status arguments={} id=None machine=simplevm subsystem=qemu/qmp
consul-register machine=simplevm
query-block arguments={} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio0 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'device': 'virtio0', 'iops': 10000, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio1 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'device': 'virtio1', 'iops': 10000, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio2 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'device': 'virtio2', 'iops': 10000, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio3 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'device': 'virtio3', 'iops': 10000, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-watchdog action=none machine=simplevm
human-monitor-command arguments={'command-line': 'watchdog_action action=none'} id=None machine=simplevm subsystem=qemu/qmp
release-lock count=0 machine=simplevm target=/run/qemu.simplevm.lock
release-lock machine=simplevm result=unlocked target=/run/qemu.simplevm.lock
"""


@pytest.mark.live
def test_simple_vm_lifecycle_start_stop(vm, patterns):
    status = patterns.status
    status.continuous(
        """
vm-status machine=simplevm result=offline
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.cidata
"""
    )

    util.test_log_options["show_events"] = ["vm-status", "rbd-status"]
    vm.status()
    assert status == get_log()

    util.test_log_options["show_events"] = []
    vm.start()
    out = clean_output(get_log())

    # This is 1 end-to-end logging test to see everything. FIXME: decide which
    # lines are really necessary to avoid false test-negatives in case number
    # or order of lines changes
    start = patterns.start
    start.in_order(
        """
acquire-lock machine=simplevm target=/run/qemu.simplevm.lock
acquire-lock count=1 machine=simplevm result=locked target=/run/qemu.simplevm.lock

pre-start machine=simplevm subsystem=ceph volume_spec=root

ensure-presence machine=simplevm subsystem=ceph volume_spec=root
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
ensure-size machine=simplevm subsystem=ceph volume_spec=root
start machine=simplevm subsystem=ceph volume_spec=root
start-root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
root-found-in current_pool=rbd.ssd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root

pre-start machine=simplevm subsystem=ceph volume_spec=swap
ensure-presence machine=simplevm subsystem=ceph volume_spec=swap
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
ensure-size machine=simplevm subsystem=ceph volume_spec=swap
start machine=simplevm subsystem=ceph volume_spec=swap
start-swap machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
mkswap args=-f -L "swap" /dev/rbd/rbd.ssd/simplevm.swap machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
mkswap> Setting up swapspace version 1, size = 1024 MiB (1073737728 bytes)
mkswap> LABEL=swap, UUID=...-...-...-...-...
mkswap machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.swap

pre-start machine=simplevm subsystem=ceph volume_spec=tmp
ensure-presence machine=simplevm subsystem=ceph volume_spec=tmp
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
ensure-size machine=simplevm subsystem=ceph volume_spec=tmp
start machine=simplevm subsystem=ceph volume_spec=tmp
start-tmp machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
create-fs machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk args=-o "/dev/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk args=-a 8192 -n 1:8192:0 -c "1:tmp" -t 1:8300 "/dev/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe args=/dev/rbd/rbd.ssd/simplevm.tmp machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
mkfs.xfs args=-q -f -K -m crc=1,finobt=1 -d su=4m,sw=1 -L "tmp" /dev/rbd/rbd.ssd/simplevm.tmp-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
mkfs.xfs> mkfs.xfs: Specified data stripe unit 8192 is not the same as the volume stripe unit 128
mkfs.xfs> mkfs.xfs: Specified data stripe width 8192 is not the same as the volume stripe width 128
mkfs.xfs> log stripe unit (4194304 bytes) is too large (maximum is 256KiB)
mkfs.xfs> log stripe unit adjusted to 32KiB
mkfs.xfs machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
seed-fc machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe args=/dev/rbd/rbd.ssd/simplevm.tmp machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
mount args="/dev/rbd/rbd.ssd/simplevm.tmp-part1" "/mnt/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
guest-properties machine=simplevm properties={'binary_generation': 2, 'rbd_pool': 'rbd.ssd'} subsystem=ceph volume=rbd.ssd/simplevm.tmp
binary-generation generation=2 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
umount args="/mnt/rbd/rbd.ssd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.tmp
pre-start machine=simplevm subsystem=ceph volume_spec=cidata
ensure-presence machine=simplevm subsystem=ceph volume_spec=cidata
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
ensure-size machine=simplevm subsystem=ceph volume_spec=cidata
start machine=simplevm subsystem=ceph volume_spec=cidata
start-cloud-init machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
create-fs machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
sgdisk args=-o "/dev/rbd/rbd.ssd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
sgdisk args=-n 1:: -c "1:cidata" -t 1:8300 "/dev/rbd/rbd.ssd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
partprobe args=/dev/rbd/rbd.ssd/simplevm.cidata machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
mkfs.vfat args=-n "cidata" /dev/rbd/rbd.ssd/simplevm.cidata-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
mkfs.vfat> mkfs.fat: Warning: lowercase labels might not work properly on some systems
mkfs.vfat> mkfs.fat ...
mkfs.vfat machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
seed-fc machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
partprobe args=/dev/rbd/rbd.ssd/simplevm.cidata machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
mount args="/dev/rbd/rbd.ssd/simplevm.cidata-part1" "/mnt/rbd/rbd.ssd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
guest-properties machine=simplevm properties={'binary_generation': 2, 'rbd_pool': 'rbd.ssd'} subsystem=ceph volume=rbd.ssd/simplevm.cidata
umount args="/mnt/rbd/rbd.ssd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.cidata
generate-config machine=simplevm
acquire-global-lock machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
global-lock-acquire machine=simplevm result=locked subsystem=qemu target=/run/fc-qemu.lock
global-lock-status count=1 machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
sufficient-host-memory available_real=... bookable=2000 machine=simplevm required=384 subsystem=qemu
start-qemu machine=simplevm subsystem=qemu
qemu-system-x86_64 additional_args=() local_args=['-nodefaults', '-only-migratable', '-cpu qemu64,enforce', '-name simplevm,process=kvm.simplevm', '-chroot /srv/vm/simplevm', '-runas nobody', '-serial file:/var/log/vm/simplevm.log', '-display vnc=127.0.0.1:2345', '-pidfile /run/qemu.simplevm.pid', '-vga std', '-m 256', '-readconfig /run/qemu.simplevm.cfg'] machine=simplevm subsystem=qemu
exec cmd=supervised-qemu qemu-system-x86_64 -nodefaults -only-migratable -cpu qemu64,enforce -name simplevm,process=kvm.simplevm -chroot /srv/vm/simplevm -runas nobody -serial file:/var/log/vm/simplevm.log -display vnc=127.0.0.1:2345 -pidfile /run/qemu.simplevm.pid -vga std -m 256 -readconfig /run/qemu.simplevm.cfg -D /var/log/vm/simplevm.qemu.internal.log simplevm /var/log/vm/simplevm.supervisor.log machine=simplevm subsystem=qemu
supervised-qemu-stdout machine=simplevm subsystem=qemu
supervised-qemu-stderr machine=simplevm subsystem=qemu
global-lock-status count=0 machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
global-lock-release machine=simplevm subsystem=qemu target=/run/fc-qemu.lock
global-lock-release machine=simplevm result=unlocked subsystem=qemu
qmp_capabilities arguments={} id=None machine=simplevm subsystem=qemu/qmp
query-status arguments={} id=None machine=simplevm subsystem=qemu/qmp
consul-register machine=simplevm
query-block arguments={} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio0 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'device': 'virtio0', 'iops': 10000, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio1 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'device': 'virtio1', 'iops': 10000, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio2 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'device': 'virtio2', 'iops': 10000, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=0 device=virtio3 machine=simplevm target_iops=10000
block_set_io_throttle arguments={'device': 'virtio3', 'iops': 10000, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-watchdog action=none machine=simplevm
human-monitor-command arguments={'command-line': 'watchdog_action action=none'} id=None machine=simplevm subsystem=qemu/qmp
release-lock count=0 machine=simplevm target=/run/qemu.simplevm.lock
release-lock machine=simplevm result=unlocked target=/run/qemu.simplevm.lock
"""
    )
    start.optional(
        """
sgdisk> Creating new GPT entries in memory.
rbd> /dev/rbd0
waiting interval=0 machine=simplevm remaining=... subsystem=ceph volume=rbd.ssd/simplevm.tmp
waiting interval=0 machine=simplevm remaining=... subsystem=ceph volume=rbd.ssd/simplevm.cidata
sgdisk> Setting name!
sgdisk> partNum is 0
"""
    )

    bootstrap = patterns.bootstrap
    bootstrap.continuous(
        """
create-vm machine=simplevm subsystem=ceph volume=simplevm.root
/nix/store/.../bin/fc-create-vm args=-I simplevm machine=simplevm subsystem=ceph volume=simplevm.root
fc-create-vm>
fc-create-vm> Establishing system identity
fc-create-vm> ----------------------------
fc-create-vm> $ rbd --format json --id ... snap ls rbd.hdd/fc-21.05-dev
fc-create-vm> Snapshots:
fc-create-vm> snapid name size
fc-create-vm> 4 v1 524288000
fc-create-vm> $ rbd --id ... clone rbd.hdd/fc-21.05-dev@v1 rbd.ssd/simplevm.root
fc-create-vm>
fc-create-vm> Finished
fc-create-vm> --------
/nix/store/.../bin/fc-create-vm machine=simplevm returncode=0 subsystem=ceph volume=simplevm.root
partprobe args=/dev/rbd/rbd.ssd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
"""
    )
    # need to split the continuous blocks as there can be the optional "waiting" line in between
    bootstrap.continuous(
        """
blkid args=/dev/rbd/rbd.ssd/simplevm.root-part1 -o export machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
blkid> DEVNAME=/dev/rbd/rbd.ssd/simplevm.root-part1
blkid> UUID=...-...-...-...-...
blkid> BLOCK_SIZE=512
blkid> TYPE=xfs
blkid> PARTLABEL=ROOT
blkid> PARTUUID=...-...-...-...-...
blkid machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
partprobe args=/dev/rbd/rbd.ssd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
mount args="/dev/rbd/rbd.ssd/simplevm.root-part1" "/mnt/rbd/rbd.ssd/simplevm.root" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
umount args="/mnt/rbd/rbd.ssd/simplevm.root" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
regenerate-xfs-uuid device=/dev/rbd/rbd.ssd/simplevm.root-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
xfs_db args=-x -c 'uuid generate' /dev/rbd/rbd.ssd/simplevm.root-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
xfs_db> Clearing log and setting UUID
xfs_db> writing all SBs
xfs_db> new UUID = ...-...-...-...-...
xfs_db machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
"""
    )

    # Things that happen depending on timing:
    start.optional(
        """
waiting interval=0 machine=simplevm remaining=4 subsystem=ceph volume=rbd.ssd/simplevm...
qmp_capabilities arguments={} id=None machine=simplevm subsystem=qemu/qmp
"""
    )

    first_start = patterns.first_start
    first_start.merge("start", "bootstrap")

    # Validate the pattern
    assert TEST_FIRST_START_VARIATION == first_start
    assert out == first_start

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
disk-throttle device=virtio3 iops=10000 machine=simplevm
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
    )

    vm.stop()
    get_log()

    vm.status()
    assert (
        get_log()
        == """\
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
    )

    # Starting a second time doesn't show the bootstrap code!

    util.test_log_options["show_events"] = []
    vm.start()
    out = clean_output(get_log())

    no_bootstrap = patterns.no_bootstrap
    no_bootstrap.refused(
        """
create-vm machine=simplevm subsystem=ceph
/nix/store/.../bin/fc-create-vm args=-I simplevm machine=simplevm subsystem=ceph
fc-create-vm> ...
blkid> PARTLABEL=ROOT
mount args="/dev/rbd/rbd.ssd/simplevm.root-part1" "/mnt/rbd/rbd.ssd/simplevm.root" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
umount args="/mnt/rbd/rbd.ssd/simplevm.root" machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
regenerate-xfs-uuid device=/dev/rbd/rbd.ssd/simplevm.root-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
xfs_admin args=-U generate /dev/rbd/rbd.ssd/simplevm.root-part1 machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
xfs_admin> Clearing log and setting UUID
xfs_admin> writing all SBs
xfs_admin> new UUID = ...
xfs_admin machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root
"""
    )

    second_start = patterns.second_start
    second_start.merge("start", "no_bootstrap")
    second_start.in_order(
        """
mkswap> mkswap: /dev/rbd/rbd.ssd/simplevm.swap: warning: wiping old swap signature.
"""
    )

    assert second_start == out


@pytest.mark.live
def test_simple_vm_lifecycle_ensure_going_offline(vm, capsys, caplog):
    print(
        subprocess.check_output(
            "top -b -n 1 -o %MEM | head -n 20", shell=True
        ).decode("ascii")
    )
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
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
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
disk-throttle device=virtio3 iops=10000 machine=simplevm
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
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
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
    )

    # A VM that is offline and should stay offline should be very fast to evaluate.
    # Specifically we do not want any Ceph connections to appear here.
    #
    # Force re-initialisation of Ceph structures.
    vm.__exit__(None, None, None)
    # This is normally set through the CLI main code
    vm.ceph_attach_on_enter = False
    vm.__enter__()
    assert not vm.ceph.attached
    assert not any(vm.ceph.volumes.values())

    vm.ensure_offline()  # ensure() resets the ceph instance so we call this directly.
    util.test_log_options["show_events"] = []
    util.test_log_options["hide_subsystems"] = []
    assert (
        get_log()
        == """\
ensure-state action=none found=offline machine=simplevm wanted=offline"""
    )
    assert not vm.ceph.attached
    assert not any(vm.ceph.volumes.values())


@pytest.mark.live
def test_vm_not_running_here(vm, capsys):
    util.test_log_options["show_events"] = ["vm-status", "rbd-status"]

    vm.status()
    assert (
        get_log()
        == """\
vm-status machine=simplevm result=offline
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
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
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
    )


@pytest.mark.live
def test_crashed_vm_clean_restart(vm, patterns):
    util.test_log_options["show_events"] = [
        "rbd-status",
        "vm-status",
        "ensure",
        "throttle",
        "shutdown",
    ]

    util.test_log_print("=== Running status() ===")
    vm.status()

    assert get_log() == Ellipsis(
        """\
vm-status machine=simplevm result=offline
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status machine=simplevm presence=missing subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
    )

    util.test_log_print("=== Running ensure() ... ===")
    vm.ensure()
    util.test_log_print("=== Running status() ===")

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
disk-throttle device=virtio3 iops=10000 machine=simplevm
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
    )

    util.test_log_print("=== Killing the qemu process ===")
    p = vm.qemu.proc()
    p.kill()
    p.wait(2)
    get_log()

    util.test_log_print("=== Running status() ===")
    vm.status()
    assert get_log() == Ellipsis(
        """\
vm-status machine=simplevm result=offline
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
    )

    util.test_log_print("=== Running ensure() ===")
    vm.ensure()

    util.test_log_print("=== Running status() ===")
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
disk-throttle device=virtio3 iops=10000 machine=simplevm
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata"""
    )

    util.test_log_options["show_events"] = [
        "shutdown",
        "kill",
        "unlock",
        "vm-status",
        "vm-destroy",
        "consul",
        "clean",
        "rbd-status",
    ]
    util.test_log_print("=== Running stop() ===")
    vm.stop()

    stop = patterns.stop
    stop.in_order(
        """
graceful-shutdown machine=simplevm
graceful-shutdown-failed machine=simplevm reason=timeout
kill-vm machine=simplevm
vm-destroy-vm-via-qmp machine=simplevm subsystem=qemu
killed-vm machine=simplevm
unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
consul-deregister machine=simplevm
clean-run-files machine=simplevm subsystem=qemu
"""
    )
    stop.optional(
        """
vm-destroy-kill-supervisor attempt=... machine=simplevm subsystem=qemu
"""
    )
    assert stop == get_log()

    util.test_log_print("=== Running status() ===")
    vm.status()

    status = patterns.status
    status.in_order(
        """
vm-status machine=simplevm result=offline
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.swap
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.tmp
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.cidata
consul machine=simplevm service=<not registered>
"""
    )
    assert status == get_log()


@pytest.fixture
def cooldown():
    yield
    time.sleep(35)


@pytest.mark.live
@pytest.mark.first
def test_do_not_clean_up_crashed_vm_that_doesnt_get_restarted(
    vm,
    cooldown,
):
    # This VM leaves things around

    # XXX I've seen this test to be flaky and in the way that the ensure() call
    # branches out into an incoming migration, this fails with the auth_cookie
    # not being possible to compute. This happens relatively rarely and might be
    # an isolation issue.
    vm.ensure()
    assert vm.qemu.is_running() is True
    proc = vm.qemu.proc()
    proc.kill()
    proc.wait(2)
    assert vm.ceph.locked_by_me() is True
    vm.enc["parameters"]["online"] = False
    vm.enc["consul-generation"] += 1
    vm.stage_new_config()
    vm.enc["consul-generation"] -= 1
    vm.ensure()
    # We don't really know what's going on here, so, yeah, don't touch it.
    assert vm.ceph.locked_by_me() is True


@pytest.mark.live
def test_vm_snapshot_only_if_running(vm):
    vm.ceph.specs["root"].ensure_presence()
    assert list(x.fullname for x in vm.ceph.volumes["root"].snapshots) == []
    with pytest.raises(InvalidCommand):
        vm.snapshot("asdf")


@pytest.mark.live
def test_vm_snapshot_with_missing_guest_agent(vm, monkeypatch):
    util.test_log_options["show_events"] = [
        "consul",
        "snapshot",
        "freeze",
        "thaw",
        "disconnect",
    ]

    monkeypatch.setattr(util, "today", lambda: datetime.date(2010, 1, 1))

    vm.ceph.specs["root"].ensure_presence()
    assert list(x.fullname for x in vm.ceph.volumes["root"].snapshots) == []
    vm.ensure()
    get_log()

    with pytest.raises(Exception):
        vm.snapshot("asdf", 7)
    assert (
        Ellipsis(
            """\
snapshot-create machine=simplevm name=asdf-keep-until-20100108
freeze machine=simplevm volume=root
sync-gratuitous-thaw machine=simplevm subsystem=qemu/guestagent
disconnect machine=simplevm subsystem=qemu/guestagent
freeze-failed action=continue machine=simplevm reason=timed out
snapshot-ignore machine=simplevm reason=not frozen"""
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
sync-gratuitous-thaw machine=simplevm subsystem=qemu/guestagent
disconnect machine=simplevm subsystem=qemu/guestagent
freeze-failed action=continue machine=simplevm reason=timed out
snapshot-ignore machine=simplevm reason=not frozen"""
        )
        == get_log()
    )


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
ensure-throttle action=none current_iops=10000 device=virtio2 machine=simplevm target_iops=10000
ensure-throttle action=none current_iops=10000 device=virtio3 machine=simplevm target_iops=10000"""
    )

    vm.cfg["iops"] = 10

    vm.ensure_online_disk_throttle()
    assert (
        get_log()
        == """\
query-block arguments={} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=10000 device=virtio0 machine=simplevm target_iops=10
block_set_io_throttle arguments={'device': 'virtio0', 'iops': 10, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=10000 device=virtio1 machine=simplevm target_iops=10
block_set_io_throttle arguments={'device': 'virtio1', 'iops': 10, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=10000 device=virtio2 machine=simplevm target_iops=10
block_set_io_throttle arguments={'device': 'virtio2', 'iops': 10, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=throttle current_iops=10000 device=virtio3 machine=simplevm target_iops=10
block_set_io_throttle arguments={'device': 'virtio3', 'iops': 10, 'iops_rd': 0, 'iops_wr': 0, 'bps': 0, 'bps_wr': 0, 'bps_rd': 0} id=None machine=simplevm subsystem=qemu/qmp"""
    )

    vm.ensure_online_disk_throttle()
    assert (
        get_log()
        == """\
query-block arguments={} id=None machine=simplevm subsystem=qemu/qmp
ensure-throttle action=none current_iops=10 device=virtio0 machine=simplevm target_iops=10
ensure-throttle action=none current_iops=10 device=virtio1 machine=simplevm target_iops=10
ensure-throttle action=none current_iops=10 device=virtio2 machine=simplevm target_iops=10
ensure-throttle action=none current_iops=10 device=virtio3 machine=simplevm target_iops=10"""
    )


@pytest.fixture
def kernel_vrf_device():
    subprocess.run(["ip", "link", "delete", "vrfpub"])
    subprocess.check_call(
        ["ip", "link", "add", "vrfpub", "type", "vrf", "table", "21"]
    )
    subprocess.check_call(["ip", "link", "set", "vrfpub", "up"])
    yield "vrfpub"
    subprocess.check_call(["ip", "link", "delete", "vrfpub"])


@pytest.fixture
def kernel_tap_device(kernel_vrf_device):
    subprocess.run(["ip", "link", "delete", "taptest"])
    subprocess.check_call(["ip", "tuntap", "add", "taptest", "mode", "tap"])
    subprocess.check_call(["ip", "link", "set", "taptest", "master", "vrfpub"])
    subprocess.check_call(["ip", "link", "set", "taptest", "up"])
    yield "taptest"
    subprocess.check_call(["ip", "link", "delete", "taptest"])


def manage_routes_for_vrf(vrfname):
    def func(*args):
        subprocess.check_call(["ip", "route"] + list(args) + ["vrf", vrfname])

    return func


def show_routes_for_vrf(vrfname):
    def func():
        routes = []
        for family in ["-4", "-6"]:
            data = subprocess.check_output(
                ["ip", "-j", family, "route", "show", "vrf", vrfname]
            )
            data = data.decode("utf-8", errors="replace")
            if data:
                routes.extend(json.loads(data))
        return [
            x
            for x in routes
            if "protocol" not in x or x["protocol"] != "kernel"
        ]

    return func


def route_json_v4(dst, iface, protocol="fc-qemu", flags=[]):
    data = {
        "dst": dst,
        "dev": iface,
        "scope": "link",
        "flags": flags,
    }
    if protocol:
        data["protocol"] = protocol
    return data


def route_json_v6(dst, iface, protocol="fc-qemu", flags=[]):
    data = {
        "dst": dst,
        "dev": iface,
        "metric": 1024,
        "flags": flags,
        "pref": "medium",
    }
    if protocol:
        data["protocol"] = protocol
    return data


@pytest.mark.live
def test_vm_host_routes(vm_with_pub, kernel_vrf_device):
    vm = vm_with_pub

    manage_routes = manage_routes_for_vrf(kernel_vrf_device)
    show_routes = show_routes_for_vrf(kernel_vrf_device)

    guest_v4 = route_json_v4("192.0.2.23", "tpub3456")
    guest_v6 = route_json_v6("2001:db8:0:42::23", "tpub3456")

    assert show_routes() == []

    # Starting the VM should set the host routes correctly.
    vm.start()
    assert get_log() == Ellipsis(
        """\
...
ensure-routes action=start iface=tpub3456 machine=simplepubvm vrf=vrfpub
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ensure-routes action=reconciling current_routes=[] iface=tpub3456 machine=simplepubvm target_routes=['192.0.2.23/32', '2001:db8:0:42::23/128'] vrf=vrfpub
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ensure-routes action=finished iface=tpub3456 machine=simplepubvm vrf=vrfpub
..."""
    )
    assert show_routes() == [guest_v4, guest_v6]

    # ensure_online_host_routes() should be idempotent if nothing has
    # changed.
    vm.ensure_online_host_routes()
    assert get_log() == Ellipsis(
        """\
ensure-routes action=start iface=tpub3456 machine=simplepubvm vrf=vrfpub
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ensure-routes action=finished iface=tpub3456 machine=simplepubvm vrf=vrfpub"""
    )
    assert show_routes() == [guest_v4, guest_v6]

    # If routes belonging to a VM are deleted they should be restored.
    manage_routes("delete", "2001:db8:0:42::23/128", "dev", "tpub3456")
    assert show_routes() == [guest_v4]

    vm.ensure_online_host_routes()
    assert get_log() == Ellipsis(
        """\
ensure-routes action=start iface=tpub3456 machine=simplepubvm vrf=vrfpub
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ensure-routes action=reconciling current_routes=['192.0.2.23/32'] iface=tpub3456 machine=simplepubvm target_routes=['192.0.2.23/32', '2001:db8:0:42::23/128'] vrf=vrfpub
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ensure-routes action=finished iface=tpub3456 machine=simplepubvm vrf=vrfpub"""
    )
    assert show_routes() == [guest_v4, guest_v6]

    # If there are extra routes for the guest's tap device which are
    # not in ENC, these should be removed.
    manage_routes("add", "192.0.2.24/32", "dev", "tpub3456")
    assert show_routes() == [
        guest_v4,
        route_json_v4("192.0.2.24", "tpub3456", protocol=None),
        guest_v6,
    ]

    vm.ensure_online_host_routes()
    assert get_log() == Ellipsis(
        """\
ensure-routes action=start iface=tpub3456 machine=simplepubvm vrf=vrfpub
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ensure-routes action=reconciling current_routes=['192.0.2.23/32', '192.0.2.24/32', '2001:db8:0:42::23/128'] iface=tpub3456 machine=simplepubvm target_routes=['192.0.2.23/32', '2001:db8:0:42::23/128'] vrf=vrfpub
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ensure-routes action=finished iface=tpub3456 machine=simplepubvm vrf=vrfpub"""
    )
    assert show_routes() == [guest_v4, guest_v6]

    # If the addresses assigned to the guest in ENC change then the
    # routes should be updated correctly
    vm.cfg["interfaces"]["pub"]["networks"] = {
        "203.0.113.38/24": ["203.0.113.38"],
        "2001:db8:0:47::2014/64": ["2001:db8:0:47::2014"],
    }
    new_guest_v4 = route_json_v4("203.0.113.38", "tpub3456")
    new_guest_v6 = route_json_v6("2001:db8:0:47::2014", "tpub3456")

    vm.ensure_online_host_routes()
    assert get_log() == Ellipsis(
        """\
ensure-routes action=start iface=tpub3456 machine=simplepubvm vrf=vrfpub
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ensure-routes action=reconciling current_routes=['192.0.2.23/32', '2001:db8:0:42::23/128'] iface=tpub3456 machine=simplepubvm target_routes=['203.0.113.38/32', '2001:db8:0:47::2014/128'] vrf=vrfpub
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ensure-routes action=finished iface=tpub3456 machine=simplepubvm vrf=vrfpub"""
    )
    assert show_routes() == [new_guest_v4, new_guest_v6]


@pytest.mark.live
def test_vm_host_routes_orthogonal(
    vm_with_pub, kernel_vrf_device, kernel_tap_device
):
    vm = vm_with_pub

    manage_routes = manage_routes_for_vrf(kernel_vrf_device)
    show_routes = show_routes_for_vrf(kernel_vrf_device)

    guest_v4 = route_json_v4("192.0.2.23", "tpub3456")
    guest_v6 = route_json_v6("2001:db8:0:42::23", "tpub3456")

    assert show_routes() == []

    manage_routes("add", "192.0.2.111/32", "dev", kernel_tap_device)
    tap_v4 = route_json_v4(
        "192.0.2.111", kernel_tap_device, protocol=None, flags=["linkdown"]
    )

    assert show_routes() == [tap_v4]

    # Starting the VM should set the host routes correctly and ignore
    # routes for other tap interfaces in the same VRF.
    vm.start()
    assert get_log() == Ellipsis(
        """\
...
ensure-routes action=start iface=tpub3456 machine=simplepubvm vrf=vrfpub
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ensure-routes action=reconciling current_routes=[] iface=tpub3456 machine=simplepubvm target_routes=['192.0.2.23/32', '2001:db8:0:42::23/128'] vrf=vrfpub
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ensure-routes action=finished iface=tpub3456 machine=simplepubvm vrf=vrfpub
..."""
    )
    assert show_routes() == [guest_v4, tap_v4, guest_v6]

    # ensure_online_host_routes() should be idempotent if nothing has
    # changed.
    vm.ensure_online_host_routes()
    assert get_log() == Ellipsis(
        """\
ensure-routes action=start iface=tpub3456 machine=simplepubvm vrf=vrfpub
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ensure-routes action=finished iface=tpub3456 machine=simplepubvm vrf=vrfpub"""
    )
    assert show_routes() == [guest_v4, tap_v4, guest_v6]

    # If there are extra routes for the guest's tap device which are
    # not in ENC, these should be removed without affecting other tap
    # devices in the same VRF.
    manage_routes("add", "192.0.2.24/32", "dev", "tpub3456")
    assert show_routes() == [
        guest_v4,
        route_json_v4("192.0.2.24", "tpub3456", protocol=None),
        tap_v4,
        guest_v6,
    ]

    vm.ensure_online_host_routes()
    assert get_log() == Ellipsis(
        """\
ensure-routes action=start iface=tpub3456 machine=simplepubvm vrf=vrfpub
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ip args=... machine=simplepubvm
ip> ...
ip machine=simplepubvm returncode=0
ensure-routes action=reconciling current_routes=['192.0.2.23/32', '192.0.2.24/32', '2001:db8:0:42::23/128'] iface=tpub3456 machine=simplepubvm target_routes=['192.0.2.23/32', '2001:db8:0:42::23/128'] vrf=vrfpub
ip args=... machine=simplepubvm
ip machine=simplepubvm returncode=0
ensure-routes action=finished iface=tpub3456 machine=simplepubvm vrf=vrfpub"""
    )
    assert show_routes() == [guest_v4, tap_v4, guest_v6]


@pytest.mark.live
def test_vm_resize_disk(vm, patterns):
    vm.start()
    get_log()

    vm.cfg["root_size"] += 1 * 1024**3
    vm.ensure_online_disk_size()
    resize_1 = patterns.resize_1
    resize_1.in_order(
        """
check-disk-size action=resize found=2147483648 machine=simplevm wanted=3221225472
block_resize arguments={'device': 'virtio0', 'size': 3221225472} id=None machine=simplevm subsystem=qemu/qmp
"""
    )
    assert get_log() == resize_1

    # Increasing the desired disk size also triggers a change.
    vm.cfg["root_size"] *= 2
    vm.ensure_online_disk_size()
    resize_2 = patterns.resize_2
    resize_2.in_order(
        """
check-disk-size action=resize found=3221225472 machine=simplevm wanted=6442450944
block_resize arguments={'device': 'virtio0', 'size': 6442450944} id=None machine=simplevm subsystem=qemu/qmp
"""
    )
    assert get_log() == resize_2

    # The disk image is of the right size and thus nothing happens.
    vm.ensure_online_disk_size()
    resize_noop = patterns.resize_noop
    resize_noop.in_order(
        """
check-disk-size action=none found=6442450944 machine=simplevm wanted=6442450944
"""
    )

    assert get_log() == resize_noop

    # Decreasing the desired disk size does not trigger a change.
    vm.cfg["root_size"] = 6442450944 - 1
    vm.ensure_online_disk_size()
    resize_noop2 = patterns.resize_noop2
    resize_noop2.in_order(
        """
check-disk-size action=none found=6442450944 machine=simplevm wanted=6442450943
"""
    )
    assert get_log() == resize_noop2


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


@pytest.fixture
def outmigrate_pattern(patterns):
    outmigrate = patterns.outmigrate
    outmigrate.in_order(
        """
.../bin/fc-qemu -v outmigrate simplevm
load-system-config
simplevm              acquire-lock                   target='/run/qemu.simplevm.lock'
simplevm              acquire-lock                   count=1 result='locked' target='/run/qemu.simplevm.lock'
simplevm     qemu/qmp qmp_capabilities               arguments={} id=None
simplevm     qemu/qmp query-status                   arguments={} id=None

simplevm              outmigrate
simplevm              consul-register
simplevm              setup-outgoing-migration       cookie='...'
simplevm              locate-inmigration-service
simplevm              check-staging-config           result='none'
simplevm              located-inmigration-service    url='http://...test.gocept.net:...'

simplevm              acquire-migration-locks
simplevm              check-staging-config           result='none'
simplevm         qemu acquire-migration-lock         result='success'
simplevm              acquire-local-migration-lock   result='success'
simplevm              acquire-remote-migration-lock
simplevm              acquire-remote-migration-lock  result='success'

simplevm         ceph unlock                         volume='rbd.ssd/simplevm.root'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.swap'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.tmp'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.cidata'

simplevm              prepare-remote-environment
simplevm              start-migration                target='tcp:...:...'
simplevm         qemu migrate
simplevm     qemu/qmp migrate-set-capabilities       arguments={'capabilities': [{'capability': 'xbzrle', 'state': False}, {'capability': 'auto-converge', 'state': True}]} id=None
simplevm     qemu/qmp migrate-set-parameters         arguments={'compress-level': 0, 'downtime-limit': 4000, 'max-bandwidth': 22500} id=None
simplevm     qemu/qmp migrate                        arguments={'uri': 'tcp:...:...'} id=None

simplevm     qemu/qmp query-migrate-parameters       arguments={} id=None
simplevm         qemu migrate-parameters             announce-initial=50 announce-max=550 announce-rounds=5 announce-step=100 block-incremental=False compress-level=0 compress-threads=8 compress-wait-thread=True cpu-throttle-increment=10 cpu-throttle-initial=20 cpu-throttle-tailslow=False decompress-threads=2 downtime-limit=4000 max-bandwidth=22500 max-cpu-throttle=99 max-postcopy-bandwidth=0 multifd-channels=2 multifd-compression='none' multifd-zlib-level=1 multifd-zstd-level=1 throttle-trigger-threshold=50 tls-authz='' tls-creds='' tls-hostname='' x-checkpoint-delay=20000 xbzrle-cache-size=67108864

simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=... remaining='...' status='active'

simplevm              migration-status               mbps=... remaining='...' status='completed'

simplevm     qemu/qmp query-status                   arguments={} id=None
simplevm              finish-migration

simplevm         qemu vm-destroy-vm-via-qmp
simplevm     qemu/qmp quit                           arguments={} id=None
simplevm         qemu clean-run-files
simplevm              finish-remote
simplevm              consul-deregister
simplevm              outmigrate-finished            exitcode=0
simplevm              release-lock                   count=0 target='/run/qemu.simplevm.lock'
simplevm              release-lock                   result='unlocked' target='/run/qemu.simplevm.lock'
"""
    )
    # There are a couple of steps in the migration process that may repeat due to
    # timings,so this may or may not appear more often:
    outmigrate.optional(
        """
simplevm         qemu vm-destroy-kill-supervisor     attempt=...
simplevm              multiple-services-found        action='trying newest first' service='vm-inmigrate-simplevm'
simplevm              waiting                        interval=3 remaining=...
simplevm              check-staging-config           result='none'
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=... remaining='...' status='active'
simplevm              migration-status               mbps='-' remaining='0' status='setup'
simplevm         qemu vm-destroy-kill-vm             attempt=...
simplevm              connect                        reason='[Errno 111] Connection refused' result='failed'
simplevm              located-inmigration-service    url='http://...test.gocept.net:...'
simplevm      libceph ...
"""
    )
    # Expect debug output that doesn't matter as much
    patterns.debug.optional("simplevm> ...")

    # This part of the heartbeats must show up
    patterns.heartbeat.in_order(
        """\
simplevm              heartbeat-initialized
simplevm              started-heartbeat-ping
simplevm              heartbeat-ping
"""
    )
    # The pings may happen more times and sometimes the stopping part
    # isn't visible because we terminate too fast.
    patterns.heartbeat.optional(
        """
simplevm              heartbeat-ping
simplevm              stopped-heartbeat-ping
"""
    )

    outmigrate.merge("heartbeat", "debug")

    return outmigrate


def test_vm_migration_pattern(outmigrate_pattern):
    # This is a variety of outputs we have seen that are valid and where we want to be
    # sure that the Ellipsis matches them properly.
    assert (
        outmigrate_pattern
        == """\
.../bin/fc-qemu -v outmigrate simplevm
load-system-config
simplevm              acquire-lock                   target='/run/qemu.simplevm.lock'
simplevm              acquire-lock                   count=1 result='locked' target='/run/qemu.simplevm.lock'
simplevm     qemu/qmp qmp_capabilities               arguments={} id=None
simplevm     qemu/qmp query-status                   arguments={} id=None
simplevm              outmigrate
simplevm              consul-register
simplevm              setup-outgoing-migration       cookie='b76481202c5afb5b70feae12350c120b8e881356'
simplevm              heartbeat-initialized
simplevm              locate-inmigration-service
simplevm              check-staging-config           result='none'
simplevm              located-inmigration-service    url='http://host2.mgm.test.gocept.net:36573'
simplevm              started-heartbeat-ping
simplevm              acquire-migration-locks
simplevm              heartbeat-ping
simplevm              check-staging-config           result='none'
simplevm         qemu acquire-migration-lock         result='success'
simplevm              acquire-local-migration-lock   result='success'
simplevm              acquire-remote-migration-lock
simplevm              acquire-remote-migration-lock  result='success'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.root'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.swap'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.tmp'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.cidata'
simplevm              prepare-remote-environment
simplevm              start-migration                target='tcp:...:2345'
simplevm         qemu migrate
simplevm     qemu/qmp migrate-set-capabilities       arguments={'capabilities': [{'capability': 'xbzrle', 'state': False}, {'capability': 'auto-converge', 'state': True}]} id=None
simplevm     qemu/qmp migrate-set-parameters         arguments={'compress-level': 0, 'downtime-limit': 4000, 'max-bandwidth': 22500} id=None
simplevm     qemu/qmp migrate                        arguments={'uri': 'tcp:...:2345'} id=None
simplevm     qemu/qmp query-migrate-parameters       arguments={} id=None
simplevm         qemu migrate-parameters             announce-initial=50 announce-max=550 announce-rounds=5 announce-step=100 block-incremental=False compress-level=0 compress-threads=8 compress-wait-thread=True cpu-throttle-increment=10 cpu-throttle-initial=20 cpu-throttle-tailslow=False decompress-threads=2 downtime-limit=4000 max-bandwidth=22500 max-cpu-throttle=99 max-postcopy-bandwidth=0 multifd-channels=2 multifd-compression='none' multifd-zlib-level=1 multifd-zstd-level=1 throttle-trigger-threshold=50 tls-authz='' tls-creds='' tls-hostname='' x-checkpoint-delay=20000 xbzrle-cache-size=67108864
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps='-' remaining='0' status='setup'
simplevm>  {'blocked': False, 'status': 'setup'}
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.32976 remaining='285,528,064' status='active'
simplevm>  {'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 182,
simplevm>          'mbps': 0.32976,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 15,
simplevm>          'normal-bytes': 61440,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 285528064,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 63317},
simplevm>  'setup-time': 3,
simplevm>  'status': 'active',
simplevm>  'total-time': 1419}
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.32976 remaining='285,331,456' status='active'
simplevm>  {'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 210,
simplevm>          'mbps': 0.32976,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 35,
simplevm>          'normal-bytes': 143360,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 285331456,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 145809},
simplevm>  'setup-time': 3,
simplevm>  'status': 'active',
simplevm>  'total-time': 3423}
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.18144 remaining='267,878,400' status='active'
simplevm>  {'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 4460,
simplevm>          'mbps': 0.18144,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 46,
simplevm>          'normal-bytes': 188416,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 2500,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 267878400,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 229427},
simplevm>  'setup-time': 3,
simplevm>  'status': 'active',
simplevm>  'total-time': 6255}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.17964356435643564 remaining='226,918,400' status='active'
simplevm>  {'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 14460,
simplevm>          'mbps': 0.17964356435643564,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 46,
simplevm>          'normal-bytes': 188416,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 2475,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 226918400,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 319747},
simplevm>  'setup-time': 3,
simplevm>  'status': 'active',
simplevm>  'total-time': 10261}
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.18144 remaining='169,574,400' status='active'
simplevm>  {'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 28460,
simplevm>          'mbps': 0.18144,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 46,
simplevm>          'normal-bytes': 188416,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 2500,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 169574400,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 446195},
simplevm>  'setup-time': 3,
simplevm>  'status': 'active',
simplevm>  'total-time': 15925}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.18144 remaining='87,654,400' status='active'
simplevm>  {'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 48460,
simplevm>          'mbps': 0.18144,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 46,
simplevm>          'normal-bytes': 188416,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 2500,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 87654400,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 626835},
simplevm>  'setup-time': 3,
simplevm>  'status': 'active',
simplevm>  'total-time': 23935}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.32976 remaining='18,825,216' status='active'
simplevm>  {'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 65218,
simplevm>          'mbps': 0.32976,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 92,
simplevm>          'normal-bytes': 376832,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 18825216,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 967345},
simplevm>  'setup-time': 3,
simplevm>  'status': 'active',
simplevm>  'total-time': 35261}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.3264950495049505 remaining='839,680' status='active'
simplevm>  {'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 69511,
simplevm>          'mbps': 0.3264950495049505,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 190,
simplevm>          'normal-bytes': 778240,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 9,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 839680,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 1409137},
simplevm>  'setup-time': 3,
simplevm>  'status': 'active',
simplevm>  'total-time': 46586}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.32976 remaining='1,118,208' status='active'
simplevm>  {'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 4,
simplevm>          'dirty-sync-count': 2,
simplevm>          'duplicate': 69591,
simplevm>          'mbps': 0.32976,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 303,
simplevm>          'normal-bytes': 1241088,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 1118208,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 1874605},
simplevm>  'setup-time': 3,
simplevm>  'status': 'active',
simplevm>  'total-time': 57908}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.34057172924857854 remaining='0' status='completed'
simplevm>  {'blocked': False,
simplevm>  'downtime': 8,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 5,
simplevm>          'duplicate': 69724,
simplevm>          'mbps': 0.34057172924857854,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 447,
simplevm>          'normal-bytes': 1830912,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 0,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 2467703},
simplevm>  'setup-time': 3,
simplevm>  'status': 'completed',
simplevm>  'total-time': 68420}
simplevm     qemu/qmp query-status                   arguments={} id=None
simplevm              finish-migration
simplevm         qemu vm-destroy-kill-supervisor     attempt=1
simplevm         qemu vm-destroy-kill-supervisor     attempt=2
simplevm         qemu vm-destroy-vm-via-qmp
simplevm     qemu/qmp quit                           arguments={} id=None
simplevm         qemu clean-run-files
simplevm              finish-remote
simplevm              stopped-heartbeat-ping
simplevm              consul-deregister
simplevm              outmigrate-finished            exitcode=0
simplevm              release-lock                   count=0 target='/run/qemu.simplevm.lock'
simplevm              release-lock                   result='unlocked' target='/run/qemu.simplevm.lock'
"""
    )

    # This one is missing the "stopped-heartbeat-ping". This can happen
    # because the heartbeat has a sleep cycle of 10s and only finishes with
    # this log output when it actually triggers at the right moment.
    assert (
        outmigrate_pattern
        == """\
/nix/store/kj63j38ji0c8yk037yvzj9c5f27mzh58-python3.8-fc.qemu-d26a0eae29efd95fe5c328d8a9978eb5a6a4529e/bin/fc-qemu -v outmigrate simplevm
load-system-config
simplevm              acquire-lock                   target='/run/qemu.simplevm.lock'
simplevm              acquire-lock                   count=1 result='locked' target='/run/qemu.simplevm.lock'
simplevm     qemu/qmp qmp_capabilities               arguments={} id=None
simplevm     qemu/qmp query-status                   arguments={} id=None
simplevm              outmigrate
simplevm              consul-register
simplevm              setup-outgoing-migration       cookie='b76481202c5afb5b70feae12350c120b8e881356'
simplevm              heartbeat-initialized
simplevm              locate-inmigration-service
simplevm              check-staging-config           result='none'
simplevm              located-inmigration-service    url='http://host2.mgm.test.gocept.net:35241'
simplevm              started-heartbeat-ping
simplevm              acquire-migration-locks
simplevm              heartbeat-ping
simplevm              check-staging-config           result='none'
simplevm         qemu acquire-migration-lock         result='success'
simplevm              acquire-local-migration-lock   result='success'
simplevm              acquire-remote-migration-lock
simplevm              acquire-remote-migration-lock  result='success'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.root'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.swap'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.tmp'
simplevm         ceph unlock                         volume='rbd.ssd/simplevm.cidata'
simplevm              prepare-remote-environment
simplevm              start-migration                target='tcp:...:2345'
simplevm         qemu migrate
simplevm     qemu/qmp migrate-set-capabilities       arguments={'capabilities': [{'capability': 'xbzrle', 'state': False}, {'capability': 'auto-converge', 'state': True}]} id=None
simplevm     qemu/qmp migrate-set-parameters         arguments={'compress-level': 0, 'downtime-limit': 4000, 'max-bandwidth': 22500} id=None
simplevm     qemu/qmp migrate                        arguments={'uri': 'tcp:...:2345'} id=None
simplevm     qemu/qmp query-migrate-parameters       arguments={} id=None
simplevm         qemu migrate-parameters             announce-initial=50 announce-max=550 announce-rounds=5 announce-step=100 block-incremental=False compress-level=0 compress-threads=8 compress-wait-thread=True cpu-throttle-increment=10 cpu-throttle-initial=20 cpu-throttle-tailslow=False decompress-threads=2 downtime-limit=4000 max-bandwidth=22500 max-cpu-throttle=99 max-postcopy-bandwidth=0 multifd-channels=2 multifd-compression='none' multifd-zlib-level=1 multifd-zstd-level=1 throttle-trigger-threshold=50 tls-authz='' tls-creds='' tls-hostname='' x-checkpoint-delay=20000 xbzrle-cache-size=67108864
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps='-' remaining='0' status='setup'
simplevm> { 'blocked': False, 'status': 'setup'}
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.32976 remaining='285,528,064' status='active'
simplevm> { 'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 182,
simplevm>          'mbps': 0.32976,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 15,
simplevm>          'normal-bytes': 61440,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 285528064,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 63317},
simplevm>  'setup-time': 1,
simplevm>  'status': 'active',
simplevm>  'total-time': 1418}
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.32976 remaining='285,331,456' status='active'
simplevm> { 'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 210,
simplevm>          'mbps': 0.32976,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 35,
simplevm>          'normal-bytes': 143360,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 285331456,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 145809},
simplevm>  'setup-time': 1,
simplevm>  'status': 'active',
simplevm>  'total-time': 3422}
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.18144 remaining='267,878,400' status='active'
simplevm> { 'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 4460,
simplevm>          'mbps': 0.18144,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 46,
simplevm>          'normal-bytes': 188416,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 2500,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 267878400,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 229427},
simplevm>  'setup-time': 1,
simplevm>  'status': 'active',
simplevm>  'total-time': 6254}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.18144 remaining='226,918,400' status='active'
simplevm> { 'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 14460,
simplevm>          'mbps': 0.18144,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 46,
simplevm>          'normal-bytes': 188416,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 2500,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 226918400,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 319747},
simplevm>  'setup-time': 1,
simplevm>  'status': 'active',
simplevm>  'total-time': 10259}
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.18144 remaining='169,574,400' status='active'
simplevm> { 'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 28460,
simplevm>          'mbps': 0.18144,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 46,
simplevm>          'normal-bytes': 188416,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 2500,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 169574400,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 446195},
simplevm>  'setup-time': 1,
simplevm>  'status': 'active',
simplevm>  'total-time': 15923}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.18144 remaining='87,654,400' status='active'
simplevm> { 'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 48460,
simplevm>          'mbps': 0.18144,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 46,
simplevm>          'normal-bytes': 188416,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 2500,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 87654400,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 626835},
simplevm>  'setup-time': 1,
simplevm>  'status': 'active',
simplevm>  'total-time': 23932}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.32976 remaining='18,825,216' status='active'
simplevm> { 'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 65218,
simplevm>          'mbps': 0.32976,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 92,
simplevm>          'normal-bytes': 376832,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 18825216,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 967345},
simplevm>  'setup-time': 1,
simplevm>  'status': 'active',
simplevm>  'total-time': 35258}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.32976 remaining='843,776' status='active'
simplevm> { 'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 1,
simplevm>          'duplicate': 69511,
simplevm>          'mbps': 0.32976,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 189,
simplevm>          'normal-bytes': 774144,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 843776,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 1405025},
simplevm>  'setup-time': 1,
simplevm>  'status': 'active',
simplevm>  'total-time': 46582}
simplevm              heartbeat-ping
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.32976 remaining='1,118,208' status='active'
simplevm> { 'blocked': False,
simplevm>  'expected-downtime': 4000,
simplevm>  'ram': {'dirty-pages-rate': 4,
simplevm>          'dirty-sync-count': 2,
simplevm>          'duplicate': 69591,
simplevm>          'mbps': 0.32976,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 303,
simplevm>          'normal-bytes': 1241088,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 1118208,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 1874605},
simplevm>  'setup-time': 1,
simplevm>  'status': 'active',
simplevm>  'total-time': 57908}
simplevm              heartbeat-ping
simplevm     qemu/qmp query-migrate                  arguments={} id=None
simplevm              migration-status               mbps=0.3400370667639548 remaining='0' status='completed'
simplevm> { 'blocked': False,
simplevm>  'downtime': 11,
simplevm>  'ram': {'dirty-pages-rate': 0,
simplevm>          'dirty-sync-count': 5,
simplevm>          'duplicate': 69724,
simplevm>          'mbps': 0.3400370667639548,
simplevm>          'multifd-bytes': 0,
simplevm>          'normal': 447,
simplevm>          'normal-bytes': 1830912,
simplevm>          'page-size': 4096,
simplevm>          'pages-per-second': 10,
simplevm>          'postcopy-requests': 0,
simplevm>          'remaining': 0,
simplevm>          'skipped': 0,
simplevm>          'total': 286334976,
simplevm>          'transferred': 2467711},
simplevm>  'setup-time': 1,
simplevm>  'status': 'completed',
simplevm>  'total-time': 68526}
simplevm     qemu/qmp query-status                   arguments={} id=None
simplevm              finish-migration
simplevm         qemu vm-destroy-kill-supervisor     attempt=1
simplevm         qemu vm-destroy-vm-via-qmp
simplevm     qemu/qmp quit                           arguments={} id=None
simplevm         qemu clean-run-files
simplevm              finish-remote
simplevm              consul-deregister
simplevm              outmigrate-finished            exitcode=0
simplevm              release-lock                   count=0 target='/run/qemu.simplevm.lock'
simplevm              release-lock                   result='unlocked' target='/run/qemu.simplevm.lock'
"""
    )


@pytest.mark.live
@pytest.mark.timeout(300)
def test_vm_migration(
    request, vm, outmigrate_pattern, patterns, kill_vms_host2
):
    def call(cmd, wait=True, host=None, fail_on_exit_code=True):
        for ssh_cmd in ["scp", "ssh"]:
            if not cmd.startswith(ssh_cmd):
                continue
            cmd = cmd.replace(
                ssh_cmd,
                ssh_cmd + " -oStrictHostKeyChecking=no -i/etc/ssh_key ",
                1,
            )
            break
        print(f"Starting command `{cmd}`")
        p = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            encoding="ascii",
            errors="replace",
        )
        if wait:
            stdout, stderr = p.communicate()
            print(stdout)
            if fail_on_exit_code and (code := p.wait()):
                raise RuntimeError(cmd, code, stderr, stdout)
            return clean_output(stdout)
        return p

    def communicate_progress(p):
        stdout = ""
        while True:
            line = p.stdout.readline()
            if line:
                # This ensures we get partial output e.g. during timeouts.
                print(line.rstrip())
                stdout += line
            else:
                p.wait()
                output = clean_output(stdout)
                # This ensures we get the output the ellipsis sees.
                print("Cleaned output for test consumption:")
                print(output)
                return output

    call("fc-qemu start simplevm")
    call("sed -i -e 's/host1/host2/' /etc/qemu/vm/simplevm.cfg")
    call("scp /etc/qemu/vm/simplevm.cfg host2:/etc/qemu/vm/")

    inmigrate = call("ssh host2 'fc-qemu -v inmigrate simplevm'", wait=False)
    outmigrate = call("fc-qemu -v outmigrate simplevm", wait=False)

    # Consume both process outputs so in a failing test we see both
    # in the test output and can more easily compare what's going on.
    inmigrate_result = communicate_progress(inmigrate)
    outmigrate_result = communicate_progress(outmigrate)

    inmigrate_pattern = patterns.inmigrate
    inmigrate_pattern.in_order(
        """
.../bin/fc-qemu -v inmigrate simplevm
load-system-config
simplevm              acquire-lock                   target='/run/qemu.simplevm.lock'
simplevm              acquire-lock                   count=1 result='locked' target='/run/qemu.simplevm.lock'

simplevm              inmigrate
simplevm              start-server                   type='incoming' url='http://...test.gocept.net:.../'
simplevm              setup-incoming-api             cookie='...'
simplevm              consul-register-inmigrate

simplevm              received-acquire-migration-lock
simplevm         qemu acquire-migration-lock         result='success'
simplevm              received-acquire-ceph-locks
simplevm         ceph lock                           volume='rbd.ssd/simplevm.root'
simplevm         ceph lock                           volume='rbd.ssd/simplevm.swap'
simplevm         ceph lock                           volume='rbd.ssd/simplevm.tmp'
simplevm         ceph lock                           volume='rbd.ssd/simplevm.cidata'

simplevm              received-prepare-incoming
simplevm         qemu acquire-global-lock            target='/run/fc-qemu.lock'
simplevm         qemu global-lock-acquire            result='locked' target='/run/fc-qemu.lock'
simplevm         qemu global-lock-status             count=1 target='/run/fc-qemu.lock'
simplevm         qemu sufficient-host-memory         available_real=... bookable=... required=768
simplevm         qemu start-qemu
simplevm         qemu qemu-system-x86_64             additional_args=['-incoming tcp:...:...'] local_args=['-nodefaults', '-only-migratable', '-cpu qemu64,enforce', '-name simplevm,process=kvm.simplevm', '-chroot /srv/vm/simplevm', '-runas nobody', '-serial file:/var/log/vm/simplevm.log', '-display vnc=127.0.0.1:2345', '-pidfile /run/qemu.simplevm.pid', '-vga std', '-m 256', '-readconfig /run/qemu.simplevm.cfg']
simplevm         qemu exec                           cmd='supervised-qemu qemu-system-x86_64 -nodefaults -only-migratable -cpu qemu64,enforce -name simplevm,process=kvm.simplevm -chroot /srv/vm/simplevm -runas nobody -serial file:/var/log/vm/simplevm.log -display vnc=127.0.0.1:2345 -pidfile /run/qemu.simplevm.pid -vga std -m 256 -readconfig /run/qemu.simplevm.cfg -incoming tcp:...:2345 -D /var/log/vm/simplevm.qemu.internal.log simplevm /var/log/vm/simplevm.supervisor.log'
simplevm         qemu supervised-qemu-stdout
simplevm         qemu supervised-qemu-stderr

simplevm         qemu global-lock-status             count=0 target='/run/fc-qemu.lock'
simplevm         qemu global-lock-release            target='/run/fc-qemu.lock'
simplevm         qemu global-lock-release            result='unlocked'
simplevm     qemu/qmp qmp_capabilities               arguments={} id=None
simplevm     qemu/qmp query-status                   arguments={} id=None

simplevm              received-finish-incoming
simplevm     qemu/qmp query-status                   arguments={} id=None
simplevm              consul-deregister-inmigrate
simplevm              stop-server                    result='success' type='incoming'
simplevm              consul-register
simplevm              inmigrate-finished             exitcode=0
simplevm              release-lock                   count=0 target='/run/qemu.simplevm.lock'
simplevm              release-lock                   result='unlocked' target='/run/qemu.simplevm.lock'
"""
    )
    inmigrate_pattern.optional(
        """
simplevm>
simplevm              received-ping                  timeout=60
simplevm              reset-timeout
simplevm              waiting                        interval=0 remaining=...
simplevm              guest-disconnect
simplevm      libceph...
simplevm> rbd...
simplevm> ceph...
"""
    )

    assert outmigrate_pattern == outmigrate_result
    assert outmigrate.returncode == 0

    assert inmigrate_pattern == inmigrate_result
    assert inmigrate.returncode == 0

    # The consul check is a bit flaky as it only checks every 5 seconds
    # and I've seen the test be unreliable.
    time.sleep(6)

    local_status = call("fc-qemu status simplevm", fail_on_exit_code=False)
    assert local_status == Ellipsis(
        """\
simplevm              vm-status                      result='offline'
simplevm         ceph rbd-status                     locker=('client...', 'host2') volume='rbd.ssd/simplevm.root'
simplevm         ceph rbd-status                     locker=('client...', 'host2') volume='rbd.ssd/simplevm.swap'
simplevm         ceph rbd-status                     locker=('client...', 'host2') volume='rbd.ssd/simplevm.tmp'
simplevm         ceph rbd-status                     locker=('client...', 'host2') volume='rbd.ssd/simplevm.cidata'
simplevm              consul                         address='host2' service='qemu-simplevm'
"""
    )

    remote_status = call("ssh host2 'fc-qemu status simplevm'")
    assert remote_status == Ellipsis(
        """\
simplevm              vm-status                      result='online'
simplevm              disk-throttle                  device='virtio0' iops=...
simplevm              disk-throttle                  device='virtio1' iops=...
simplevm              disk-throttle                  device='virtio2' iops=...
simplevm              disk-throttle                  device='virtio3' iops=...
simplevm         ceph rbd-status                     locker=('client...', 'host2') volume='rbd.ssd/simplevm.root'
simplevm         ceph rbd-status                     locker=('client...', 'host2') volume='rbd.ssd/simplevm.swap'
simplevm         ceph rbd-status                     locker=('client...', 'host2') volume='rbd.ssd/simplevm.tmp'
simplevm         ceph rbd-status                     locker=('client...', 'host2') volume='rbd.ssd/simplevm.cidata'
simplevm              consul                         address='host2' service='qemu-simplevm'
"""
    )


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
ensure-presence machine=simplevm subsystem=ceph volume_spec=root
create-vm machine=simplevm subsystem=ceph volume=simplevm.root
...
generate-config machine=simplevm
...
"""
    )


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
        Ellipsis(
            """\
...
fc-create-vm>\tEstablishing system identity
fc-create-vm>\t----------------------------
fc-create-vm>\t$ rbd --format json --id ... snap ls rbd.hdd/does-not-exist
fc-create-vm>\t> return code: 2
fc-create-vm>\t> stdout:
fc-create-vm>\t
fc-create-vm>\t> stderr:
fc-create-vm>\trbd: error opening image does-not-exist: (2) No such file or directory
...
fc-create-vm>\tsubprocess.CalledProcessError: Command ...'rbd', '--format', 'json', '--id', '...', 'snap', 'ls', 'rbd.hdd/does-not-exist'... returned non-zero exit status 2.
...
"""
        )
        == get_log()
    )


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
        OK - ... VMs - ... MiB used - 768 MiB expected
        """
        )
    )
