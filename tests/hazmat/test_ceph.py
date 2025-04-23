from unittest.mock import Mock, patch

import pytest
import yaml

from fc.qemu.hazmat import libceph
from tests.conftest import get_log


@pytest.fixture
def ceph_with_volumes(ceph_inst):
    for vol in ceph_inst.specs.values():
        vol.ensure_presence()
    ceph_inst.lock()
    yield ceph_inst
    for volume in ceph_inst.opened_volumes:
        volume.unlock(force=True)
        volume.snapshots.purge()
        volume.close()
        libceph.RBD().remove(volume.ioctx, volume.name)


@pytest.fixture
def ceph_with_volumes_ci(ceph_inst_cloudinit_enc):
    ceph_inst = ceph_inst_cloudinit_enc
    for vol in ceph_inst.specs.values():
        vol.ensure_presence()
    ceph_inst.lock()
    yield ceph_inst
    for volume in ceph_inst.opened_volumes:
        volume.unlock(force=True)
        volume.snapshots.purge()
        volume.close()
        libceph.RBD().remove(volume.ioctx, volume.name)


def test_ceph_stop_should_unlock_all_volumes(ceph_with_volumes):
    for volume in ceph_with_volumes.opened_volumes:
        assert volume.lock_status()
    ceph_with_volumes.stop()
    for volume in ceph_with_volumes.opened_volumes:
        assert volume.lock_status() is None


def test_ceph_stop_remove_only_own_locks(ceph_with_volumes):
    """Test case where failed migrations leave inconsistent locking."""
    ceph_with_volumes.volumes["root"].unlock()
    ceph_with_volumes.volumes["root"].rbdimage.lock_exclusive("someotherhost")
    # It unlocks what it can.
    ceph_with_volumes.stop()
    assert ceph_with_volumes.volumes["root"].lock_status()
    assert ceph_with_volumes.volumes["swap"].lock_status() is None
    assert ceph_with_volumes.volumes["tmp"].lock_status() is None


@pytest.mark.live
def test_ceph_exclusive_lock_can_be_taken_twice_with_same_cookie(ceph_inst):
    """Test case where failed migrations leave inconsistent locking."""
    ceph = ceph_inst
    pool = ceph.ioctxs["rbd.ssd"]
    libceph.RBD().create(pool, "test", 1024)
    img = libceph.Image(pool, "test")
    img.lock_exclusive("cookie-1")
    img.lock_exclusive("cookie-1")
    with pytest.raises(libceph.ImageBusy):
        img.lock_exclusive("cookie-2")


def test_is_unlocked(ceph_with_volumes):
    assert ceph_with_volumes.is_unlocked() is False
    ceph_with_volumes.unlock()
    assert ceph_with_volumes.is_unlocked() is True


def test_multiple_images_raises_error(ceph_inst):
    libceph.RBD().create(ceph_inst.ioctxs["rbd.hdd"], "simplevm.root", 1024)
    libceph.RBD().create(ceph_inst.ioctxs["rbd.ssd"], "simplevm.root", 1024)
    root_spec = ceph_inst.specs["root"]
    assert sorted(root_spec.exists_in_pools()) == ["rbd.hdd", "rbd.ssd"]
    with pytest.raises(RuntimeError):
        root_spec.exists_in_pool()


def test_cloud_init_seed_simple(ceph_inst_cloudinit_enc):
    ceph = ceph_inst_cloudinit_enc
    libceph.RBD().create(
        ceph.ioctxs["rbd.ssd"],
        "simplevm.cidata",
        ceph.cfg["cidata_size"],
    )

    cidata_spec = ceph.specs["cidata"]
    cidata_spec.ensure_presence()
    with patch(
        "fc.qemu.directory.connect", autospec=True
    ) as directory_connect_mock:
        directory_mock = Mock()
        directory_connect_mock.return_value = directory_mock
        directory_mock.list_users.side_effect = lambda _rg: [
            {
                "class": "human",
                "email_addresses": ["test@example.com"],
                "gid": 100,
                "home_directory": "/home/test",
                "id": 1000,
                "login_shell": "/bin/zsh",
                "name": "Test Benutzer",
                "password": "{CRYPT}$6$rounds=656000$foobar",
                "permissions": {"test": ["login", "sudo-srv"]},
                "ssh_pubkey": [
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO+C/OaWGUbNrf45RYxzgxzX2OZBPLH9VararPYDuorg"
                ],
                "uid": "test",
            }
        ]
        cidata_spec.start()
        directory_mock.list_users.assert_called_once_with("test")
    with cidata_spec.volume.mounted() as target:
        metadata_file = target / "meta-data"
        assert (
            metadata_file.read_text()
            == "instance-id: e0999536194a42170cde0d3698fb47ee\n"
        )
        userdata_file = target / "user-data"
        userdata_content = userdata_file.read_text()
        assert userdata_content.startswith("#cloud-config\n")
        userdata_content_parsed = yaml.safe_load(userdata_content)
        assert userdata_content_parsed == {
            "allow_public_ssh_keys": True,
            "ssh_pwauth": False,
            "disable_root": False,
            "package_update": True,
            "packages": ["qemu-guest-agent"],
            "hostname": "simplevm",
            "users": [{"name": "root"}],
            "write_files": [
                {
                    "content": "AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys_fc\n",
                    "path": "/etc/ssh/sshd_config.d/10-cloud-init-fc.conf",
                    "permissions": "0644",
                },
                {
                    "content": """\
### managed by Flying Circus - do not edit! ###
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO+C/OaWGUbNrf45RYxzgxzX2OZBPLH9VararPYDuorg
""",
                    "path": "/root/.ssh/authorized_keys_fc",
                    "permissions": "0600",
                },
            ],
            "runcmd": [
                "systemctl enable --now qemu-guest-agent",
                "systemctl restart ssh",
                "sed -iE 's/- ssh$/- [ssh, once]/' /etc/cloud/cloud.cfg",
                "sed -iE 's/- set_passwords$/- [set_passwords, once]/' /etc/cloud/cloud.cfg",
            ],
        }
        network_config_file = target / "network-config"
        network_config_content = network_config_file.read_text()
        network_config_content_parsed = yaml.safe_load(network_config_content)
        assert network_config_content_parsed == {
            "config": [
                {
                    "accept-ra": False,
                    "mac_address": "02:00:00:02:1d:e4",
                    "name": "ethpub",
                    "subnets": [
                        {
                            "address": "203.0.113.10/24",
                            "dns_nameservers": ["9.9.9.9", "8.8.8.8"],
                            "gateway": "293.0.113.1",
                            "type": "static",
                        },
                        {
                            "address": "2001:db8:500:2::5/64",
                            "dns_nameservers": [
                                "2620:fe::fe",
                                "2001:4860:4860::8888",
                            ],
                            "gateway": "2001:db8:500:2::1",
                            "type": "static6",
                        },
                    ],
                    "type": "physical",
                }
            ],
            "version": 1,
        }


def test_cloud_init_seed_instance_id_hashing(ceph_inst_cloudinit_enc):
    ceph = ceph_inst_cloudinit_enc
    libceph.RBD().create(
        ceph.ioctxs["rbd.ssd"],
        "simplevm.cidata",
        ceph.cfg["cidata_size"],
    )

    cidata_spec = ceph.specs["cidata"]
    cidata_spec.ensure_presence()
    cidata_spec.start()
    with cidata_spec.volume.mounted() as target:
        metadata_file = target / "meta-data"
        metadata_config = yaml.safe_load(metadata_file.read_text())
        previous_instance_id = metadata_config["instance-id"]

    ceph_inst_cloudinit_enc.enc["disk"] = 20
    cidata_spec = ceph.specs["cidata"]
    cidata_spec.start()
    with cidata_spec.volume.mounted() as target:
        metadata_file = target / "meta-data"
        metadata_config = yaml.safe_load(metadata_file.read_text())
        assert metadata_config["instance-id"] != previous_instance_id


def test_cloud_init_seed_routed_pub(ceph_inst_cloudinit_enc):
    ceph = ceph_inst_cloudinit_enc
    ceph.enc["parameters"]["interfaces"]["pub"]["routed"] = True
    libceph.RBD().create(
        ceph.ioctxs["rbd.ssd"],
        "simplevm.cidata",
        ceph.cfg["cidata_size"],
    )

    cidata_spec = ceph.specs["cidata"]
    cidata_spec.ensure_presence()
    cidata_spec.start()
    with cidata_spec.volume.mounted() as target:
        network_config_file = target / "network-config"
        network_config_content = network_config_file.read_text()
        network_config_content_parsed = yaml.safe_load(network_config_content)
        assert network_config_content_parsed == {
            "config": [
                {
                    "accept-ra": False,
                    "mac_address": "02:00:00:02:1d:e4",
                    "name": "ethpub",
                    "subnets": [
                        {
                            "address": "203.0.113.10/32",
                            "dns_nameservers": ["169.254.83.168"],
                            "gateway": "169.254.83.168",
                            "type": "static",
                        },
                        {
                            "address": "2001:db8:500:2::5/128",
                            "dns_nameservers": [],
                            "gateway": "fe80::1",
                            "type": "static6",
                        },
                    ],
                    "type": "physical",
                }
            ],
            "version": 1,
        }


@pytest.mark.live
def test_rbd_pool_migration(ceph_inst, patterns) -> None:
    ceph_inst.cfg["tmp_size"] = 500 * 1024 * 1024
    ceph_inst.cfg["swap_size"] = 50 * 1024 * 1024
    ceph_inst.cfg["root_size"] = 50 * 1024 * 1024
    ceph_inst.cfg["cidata_size"] = 10 * 1024 * 1024
    libceph.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.root",
        ceph_inst.cfg["root_size"],
    )
    libceph.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.tmp",
        ceph_inst.cfg["tmp_size"],
    )
    libceph.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.swap",
        ceph_inst.cfg["swap_size"],
    )
    libceph.RBD().create(
        ceph_inst.ioctxs["rbd.ssd"],
        "simplevm.cidata",
        ceph_inst.cfg["cidata_size"],
    )
    assert ceph_inst.specs["root"].exists_in_pool() == "rbd.ssd"
    assert ceph_inst.specs["swap"].exists_in_pool() == "rbd.ssd"
    assert ceph_inst.specs["tmp"].exists_in_pool() == "rbd.ssd"
    assert ceph_inst.specs["cidata"].exists_in_pool() == "rbd.ssd"

    ceph_inst.start()
    ceph_inst.status()

    first_start = patterns.first_start
    first_start.optional(
        """
waiting interval=0 machine=simplevm remaining=... subsystem=ceph volume=rbd.hdd/simplevm.tmp
waiting interval=0 machine=simplevm remaining=... subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk> Setting name!
sgdisk> partNum is 0
mkfs.xfs>       mkfs.xfs: small data volume, ignoring data volume stripe unit 128 and stripe width 128
"""
    )
    first_start.in_order(
        """
pre-start machine=simplevm subsystem=ceph volume_spec=root
ensure-presence machine=simplevm subsystem=ceph volume_spec=root
lock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
ensure-size machine=simplevm subsystem=ceph volume_spec=root
start machine=simplevm subsystem=ceph volume_spec=root
start-root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
root-found-in current_pool=rbd.ssd machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd args=status --format json rbd.ssd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd>    {"watchers":[]}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.ssd/simplevm.root

migrate-vm-root-disk action=start machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd subsystem=ceph volume=rbd.ssd/simplevm.root
unlock machine=simplevm subsystem=ceph volume=rbd.ssd/simplevm.root
rbd args=migration prepare rbd.ssd/simplevm.root rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=simplevm.root
rbd machine=simplevm returncode=0 subsystem=ceph volume=simplevm.root

pre-start machine=simplevm subsystem=ceph volume_spec=swap
delete-outdated-swap image=simplevm.swap machine=simplevm pool=rbd.ssd subsystem=ceph volume=simplevm.swap
ensure-presence machine=simplevm subsystem=ceph volume_spec=swap
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
ensure-size machine=simplevm subsystem=ceph volume_spec=swap
start machine=simplevm subsystem=ceph volume_spec=swap
start-swap machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
mkswap args=-f -L "swap" /dev/rbd/rbd.hdd/simplevm.swap machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
mkswap> Setting up swapspace version 1, size = 50 MiB (52424704 bytes)
mkswap> LABEL=swap, UUID=...-...-...-...-...
mkswap machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.swap

pre-start machine=simplevm subsystem=ceph volume_spec=tmp
delete-outdated-tmp image=simplevm.tmp machine=simplevm pool=rbd.ssd subsystem=ceph volume=simplevm.tmp
ensure-presence machine=simplevm subsystem=ceph volume_spec=tmp
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
ensure-size machine=simplevm subsystem=ceph volume_spec=tmp
start machine=simplevm subsystem=ceph volume_spec=tmp
start-tmp machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
create-fs machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
sgdisk args=-o "/dev/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
sgdisk> Creating new GPT entries in memory.
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
sgdisk args=-a 8192 -n 1:8192:0 -c "1:tmp" -t 1:8300 "/dev/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
partprobe args=/dev/rbd/rbd.hdd/simplevm.tmp machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
mkfs.xfs args=-q -f -K -L "tmp" /dev/rbd/rbd.hdd/simplevm.tmp-part1 machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
mkfs.xfs machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
seed machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
partprobe args=/dev/rbd/rbd.hdd/simplevm.tmp machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
mount args="/dev/rbd/rbd.hdd/simplevm.tmp-part1" "/mnt/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
mount machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
guest-properties machine=simplevm properties={'binary_generation': 2} subsystem=ceph volume=rbd.hdd/simplevm.tmp
binary-generation generation=2 machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
umount args="/mnt/rbd/rbd.hdd/simplevm.tmp" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
umount machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.tmp
pre-start machine=simplevm subsystem=ceph volume_spec=cidata
delete-outdated-cloud-init image=simplevm.cidata machine=simplevm pool=rbd.ssd subsystem=ceph volume=simplevm.cidata
ensure-presence machine=simplevm subsystem=ceph volume_spec=cidata
lock machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
ensure-size machine=simplevm subsystem=ceph volume_spec=cidata
start machine=simplevm subsystem=ceph volume_spec=cidata
start-cloud-init machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
create-fs machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk args=-o "/dev/rbd/rbd.hdd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk> Creating new GPT entries in memory.
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk args=-n 1:: -c "1:cidata" -t 1:8300 "/dev/rbd/rbd.hdd/simplevm.cidata" machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
sgdisk> The operation has completed successfully.
sgdisk machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
partprobe args=/dev/rbd/rbd.hdd/simplevm.cidata machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
partprobe machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
mkfs.vfat args=-n "cidata" /dev/rbd/rbd.hdd/simplevm.cidata-part1 machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
mkfs.vfat>      mkfs.fat: Warning: lowercase labels might not work properly on some systems
mkfs.vfat>      mkfs.fat 4.2 (2021-01-31)
mkfs.vfat machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.cidata
seed machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"prepared","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=prepared subsystem=ceph volume=rbd.hdd/simplevm.root
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
"""
    )

    assert get_log() == first_start

    assert ceph_inst.specs["root"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["swap"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["tmp"].exists_in_pool() == "rbd.hdd"
    assert ceph_inst.specs["cidata"].exists_in_pool() == "rbd.hdd"

    ceph_inst.ensure()
    ceph_inst.status()

    first_ensure = patterns.first_ensure
    first_ensure.in_order(
        """
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"prepared","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=prepared subsystem=ceph volume=rbd.hdd/simplevm.root

root-migration-execute machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
ceph args=rbd task add migration execute rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
ceph>   {"sequence": ..., "id": "...-...-...-...-...", "message": "Migrating image rbd.ssd/simplevm.root to rbd.ssd/simplevm.root", "refs": {"action": "migrate execute", "pool_name": "rbd.hdd", "pool_namespace": "", "image_name": "simplevm.root", "image_id": "..."}}
ceph machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"...","state_description":...}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress=...status=... subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
"""
    )

    assert get_log() == first_ensure

    while "status=executed" not in get_log():
        ceph_inst.status()

    ceph_inst.ensure()
    ceph_inst.status()

    commit_ensure = patterns.commit_ensure
    commit_ensure.in_order(
        """
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[],"migration":{"source_pool_name":"rbd.ssd","source_pool_namespace":"","source_image_name":"simplevm.root","source_image_id":"...","dest_pool_name":"rbd.hdd","dest_pool_namespace":"","dest_image_name":"simplevm.root","dest_image_id":"...","state":"executed","state_description":""}}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

root-migration-status machine=simplevm pool_from=rbd.ssd pool_to=rbd.hdd progress= status=executed subsystem=ceph volume=rbd.hdd/simplevm.root
root-migration-commit machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=--no-progress migration commit rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=None machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd args=status --format json rbd.hdd/simplevm.root machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.root
rbd>    {"watchers":[]}
rbd machine=simplevm returncode=0 subsystem=ceph volume=rbd.hdd/simplevm.root

rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.swap
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.tmp
rbd-status locker=('client...', '...') machine=simplevm subsystem=ceph volume=rbd.hdd/simplevm.cidata
"""
    )

    assert get_log() == commit_ensure
