"""High-level handling of Ceph volumes.

We expect Ceph Python bindings to be present in the system site packages.
"""

import hashlib
import ipaddress
import json
import os
import xmlrpc.client
from pathlib import Path
from typing import Dict, Optional

import yaml

import fc.qemu.directory
import fc.qemu.hazmat.libceph as libceph
from fc.qemu.util import (
    conditional_update,
    generate_cloudinit_ssh_keyfile,
    inplace_update,
)

from ..sysconfig import sysconfig
from ..timeout import TimeoutError
from ..util import cmd, log, parse_export_format
from .volume import Volume

# These values need to be kept in sync with the KVM role in fc-nixos
# to ensure that the virtual gateway addresses inside and outside of
# VMs match each other.
ROUTED_VIRTUAL_GATEWAY_V4 = "169.254.83.168"
ROUTED_VIRTUAL_GATEWAY_V6 = "fe80::1"
ROUTED_VIRTUAL_NAMESERVER = "169.254.83.168"


def valid_rbd_pool_name(name):
    if name == "rbd":
        return True
    if ".rgw." in name:
        return False
    if name.startswith("rbd."):
        return True
    return False


# This is an additional abstraction layer on top of generic Ceph volume
# handling. When introducing the ability to migrate root volumes we had to
# start differentiating between the abstract concept of "I need a
# root/tmp/swap" volume and the specific volume objects in Ceph that may or
# may not exist.


class VolumeSpecification:
    """This is a specification of a disk attached to a VM.

    This will correlate to specific RBD volumes but exists
    independent of any specific one to allow easier state
    reconciliation and life cycle management.

    """

    # Used for internal book-keeping, suffixing the rbd image name, and for
    # labels on partitions and/or file systems.
    suffix: str

    current_pool: Optional[str]

    def __init__(self, ceph):
        self.ceph = ceph

        self.name = f"{ceph.cfg['name']}.{self.suffix}"

        self.ceph.specs[self.suffix] = self
        self.ceph.volumes.setdefault(self.suffix, None)

        self._log = ceph.log
        self.cmd = lambda cmdline, **args: cmd(cmdline, log=self.log, **args)

    @property
    def desired_pool(self) -> str:
        return self.ceph.cfg["rbd_pool"]

    @desired_pool.setter
    def desired_pool(self, value: str):
        self.ceph.cfg["rbd_pool"] = value

    @property
    def desired_size(self) -> int:
        return self.ceph.cfg[f"{self.suffix}_size"]

    @desired_size.setter
    def desired_size(self, value: int):
        self.ceph.cfg[f"{self.suffix}_size"] = value

    @property
    def log(self):
        if self.volume:
            return self._log.bind(volume=self.volume.fullname)
        return self._log.bind(volume=self.name)

    @property
    def volume(self):
        return self.ceph.volumes.get(self.suffix)

    def exists_in_pools(self):
        result = []

        for pool, ioctx in self.ceph.ioctxs.items():
            try:
                libceph.Image(ioctx, self.name)
            except libceph.ImageNotFound:
                continue
            result.append(pool)
        return result

    def exists_in_desired_pool(self):
        """Check whether the image exists in the desired pool.

        This should be used preferably in situations where we really don't
        want to do anything to look around whether it exists in any other
        pools, as this can be a performance issue to always check
        all pools all the time.

        This also doesn't do a consistency check whether the image
        also exists in other pools.

        """
        ioctx = self.ceph.ioctxs[self.desired_pool]
        try:
            libceph.Image(ioctx, self.name)
        except libceph.ImageNotFound:
            return False
        return True

    def exists_in_pool(self):
        """Show which pool the image currently exists in, independent
        of whether it's in the desired pool or elsewhere.

        This also performs a consistency check and raises an error
        if it exists in multiple pools.

        """
        pools = self.exists_in_pools()
        if len(pools) > 1:
            raise RuntimeError(f"Inconsistent pools: {pools}")
        if pools:
            return pools[0]
        return None

    def pre_start(self):  # pragma: no cover
        pass

    def start(self):  # pragma: no cover
        pass

    def status(self):
        if self.volume:
            locker = self.volume.lock_status()
            self.log.info(
                "rbd-status", volume=self.volume.fullname, locker=locker
            )
        else:
            self.log.info(
                "rbd-status",
                volume=f"{self.desired_pool}/{self.name}",
                presence="missing",
            )

    def ensure(self):  # pragma: no cover
        pass

    def ensure_presence(self):
        self.ceph.ensure_volume_presence(
            self.name, self.desired_pool, self.desired_size
        )
        self.ceph.get_volume(self)


class RootSpec(VolumeSpecification):
    suffix = "root"

    def start(self):
        self.log.info("start-root")

        if self.exists_in_desired_pool():
            self.log.debug("root-found-in", current_pool=self.desired_pool)
            return

        current_pool = self.exists_in_pool()
        self.log.debug("root-found-in", current_pool=current_pool)

        # Image needs migration.
        if self.ensure_migration(allow_execute=False):
            # Variaton 1: The image that exists is itself being migrated
            # currently, so postpone the migration and keep running with
            # the existing disk.
            # If there was a migration in progress, give it a chance to
            # commit. but do not start executing it as that seems to create
            # exclusive-locking issues result in VMs stuck booting until
            # the execution phase has finished.
            self.log.info(
                "migrate-vm-root-disk",
                action="postpone",
                reason="A different migration is currently in progress.",
            )
        else:
            # Variation 2: The image is ready to be migrated, so let's go.
            self.log.info(
                "migrate-vm-root-disk",
                action="start",
                pool_from=current_pool,
                pool_to=self.desired_pool,
            )
            self.volume.unlock()
            self.volume.close()
            self.cmd(
                f"rbd migration prepare {current_pool}/{self.name} "
                f"{self.desired_pool}/{self.name}"
            )
            # Ensure we now expose the correct volume.
            self.ceph.get_volume(self)

    def ensure_presence(self):
        # This is a lazy evaluation to prefer only checking the desired pool
        # but fall back to any pool.

        if self.exists_in_desired_pool() or self.exists_in_pool():
            self.ceph.get_volume(self)
            return

        self.log.info("create-vm")
        # We rely on the image being created in the CREATE_VM script as this
        # will perform necessary cloning (or other) operations from whatever
        # source it considers best.
        self.cmd(self.ceph.CREATE_VM.format(**self.ceph.cfg))
        self.ceph.get_volume(self)
        self.regen_xfs_uuid()

    def ensure(self):
        super().ensure()
        self.ensure_migration()

    def ensure_migration(self, allow_execute=True, allow_commit=True):
        migration = self.migration_status()
        if not migration:
            return
        elif migration["state"] == "prepared" and allow_execute:
            self.log.info("root-migration-execute")
            self.cmd(
                f"ceph rbd task add migration execute {self.volume.fullname}"
            )
            return migration
        elif migration["state"] == "executed" and allow_commit:
            self.log.info("root-migration-commit")
            self.cmd(
                f"rbd --no-progress migration commit {self.volume.fullname}",
            )
            return
        # Indicate that there is a migration pending.
        return migration

    def status(self):
        super().status()
        self.migration_status()

    def migration_status(self):
        if not self.volume:
            return
        output = self.cmd(
            f"rbd status --format json {self.volume.fullname}",
            encoding="utf-8",
        )
        migration = json.loads(output).get("migration", None)
        if not migration:
            return
        self.log.info(
            "root-migration-status",
            status=migration["state"],
            pool_from=migration["source_pool_name"],
            pool_to=migration["dest_pool_name"],
            progress=migration["state_description"],
        )
        return migration

    def regen_xfs_uuid(self):
        """Regenerate the UUID of the XFS filesystem on partition 1."""
        with self.volume.mapped():
            try:
                self.volume.wait_for_part1dev()
            except TimeoutError:
                self.log.warn(
                    "regenerate-xfs-uuid",
                    status="skipped",
                    reason="no partition found",
                )
                return
            partition = self.volume.part1dev
            output = self.cmd(f"blkid {partition} -o export")
            values = parse_export_format(output)
            fs_type = values.get("TYPE")
            if fs_type != "xfs":
                self.log.info(
                    "regenerate-xfs-uuid",
                    device=partition,
                    status="skipped",
                    fs_type=fs_type,
                    reason="filesystem type != xfs",
                )
                return
            with self.volume.mounted():
                # Mount once to ensure a clean log.
                pass
            self.log.info("regenerate-xfs-uuid", device=partition)
            self.cmd(f"xfs_db -x -c 'uuid generate' {partition}")


class TmpSpec(VolumeSpecification):
    suffix = "tmp"

    ENC_SEED_PARAMETERS = ["cpu_model", "rbd_pool"]

    def pre_start(self):
        for pool in self.exists_in_pools():
            if pool != self.desired_pool:
                self.log.info("delete-outdated-tmp", pool=pool, image=self.name)
                self.ceph.remove_volume(self.name, pool)

    def start(self):
        self.log.info("start-tmp")
        with self.volume.mapped():
            self.mkfs()
            self.seed(self.ceph.enc, self.ceph.cfg["binary_generation"])

    def mkfs(self):
        self.log.debug("create-fs")
        device = self.volume.device
        assert device, f"volume must be mapped first: {device}"
        self.cmd(f'sgdisk -o "{device}"')
        self.cmd(
            f'sgdisk -a 8192 -n 1:8192:0 -c "1:{self.suffix}" '
            f'-t 1:8300 "{device}"'
        )
        self.volume.wait_for_part1dev()
        options = getattr(self.ceph, "MKFS_XFS")
        self.cmd(
            f'mkfs.xfs {options} -L "{self.suffix}" {self.volume.part1dev}'
        )

    def seed(self, enc, generation):
        self.log.info("seed")
        with self.volume.mounted() as target:
            target.chmod(0o1777)
            fc_data = target / "fc-data"
            fc_data.mkdir()
            fc_data.chmod(0o750)
            enc_json = fc_data / "enc.json"
            enc_json.touch(0o640)
            with enc_json.open("w") as f:
                json.dump(enc, f)
                f.write("\n")
            # Seed boot-time VM properties which require a reboot to
            # change. While some of these properties are copied from
            # the ENC data, a separate file allows properties which
            # are not exposed to guests through ENC to be added in the
            # future.
            properties = {}
            properties["binary_generation"] = generation
            for key in self.ENC_SEED_PARAMETERS:
                if key in enc["parameters"]:
                    properties[key] = enc["parameters"][key]
            self.log.debug("guest-properties", properties=properties)
            guest_properties = fc_data / "qemu-guest-properties-booted"
            with guest_properties.open("w") as f:
                json.dump(properties, f)
            # For backwards compatibility with old fc-agent versions,
            # write the Qemu binary generation into a separate file.
            self.log.debug("binary-generation", generation=generation)
            generation_marker = fc_data / "qemu-binary-generation-booted"
            with generation_marker.open("w") as f:
                f.write(str(generation) + "\n")


class CloudInitSpec(VolumeSpecification):
    suffix = "cidata"

    def pre_start(self):
        for pool in self.exists_in_pools():
            if pool != self.desired_pool:
                self.log.info(
                    "delete-outdated-cloud-init", pool=pool, image=self.name
                )
                self.ceph.remove_volume(self.name, pool)

    def start(self):
        self.log.info("start-cloud-init")
        with self.volume.mapped():
            self.mkfs()
            self.seed(self.ceph.enc)

    def mkfs(self):
        self.log.debug("create-fs")
        device = self.volume.device
        assert device, f"volume must be mapped first: {device}"
        self.cmd(f'sgdisk -o "{device}"')
        self.cmd(f'sgdisk -n 1:: -c "1:{self.suffix}" -t 1:8300 "{device}"')

        self.volume.wait_for_part1dev()
        options = getattr(self.ceph, "MKFS_VFAT")
        self.cmd(
            f'mkfs.vfat {options} -n "{self.suffix}" {self.volume.part1dev}'
        )

    def seed(self, enc):
        self.log.info("seed")
        if enc["parameters"]["environment_class_type"] != "cloudinit":
            return
        managed_files = [
            {
                "path": "/etc/ssh/sshd_config.d/10-cloud-init-fc.conf",
                "content": "AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys_fc\n",
                "permissions": "0644",
            }
        ]

        rg = enc["parameters"]["resource_group"]
        users_file = Path(f"/etc/qemu/users/{rg}.json")
        if not users_file.exists():
            directory = fc.qemu.directory.connect(ring="max")
            try:
                users = directory.list_users(rg)
                Path("/etc/qemu/users").mkdir(exist_ok=True)
                try:
                    conditional_update(
                        f"/etc/qemu/users/{rg}.json", users, 0o640
                    )
                except (IOError, OSError):
                    inplace_update(f"/etc/qemu/users/{rg}.json", users)
            except (Exception, xmlrpc.client.Fault):
                self.log.warning("retrieve-all-rg-users-failed", exc_info=True)

        ssh_authorized_keys_content = ""
        try:
            with Path(f"/etc/qemu/users/{rg}.json").open() as f:
                users = json.load(f)
                ssh_authorized_keys_content = generate_cloudinit_ssh_keyfile(
                    users, rg
                )
        except IOError:
            self.log.error("users-file-not-existing")
        managed_files.append(
            {
                "path": "/root/.ssh/authorized_keys_fc",
                "content": ssh_authorized_keys_content,
                "permissions": "0600",
            }
        )

        enc_to_hash = enc.copy()
        enc_to_hash.pop("last_maintenance_end", None)
        enc_to_hash.pop("consul-generation", None)
        enc_to_hash["parameters"].pop("kvm_host", None)
        enc_hash = hashlib.md5(json.dumps(enc_to_hash, sort_keys=True).encode())
        instance_id = enc_hash.hexdigest()
        with self.volume.mounted() as target:
            metadata = target / "meta-data"
            metadata.touch()
            with metadata.open("w") as f:
                yaml.safe_dump({"instance-id": instance_id}, f)
            userdata = target / "user-data"
            userdata.touch()
            with userdata.open("w") as f:
                f.write("#cloud-config\n")
                yaml.safe_dump(
                    {
                        "allow_public_ssh_keys": True,
                        "ssh_pwauth": False,
                        "disable_root": False,
                        "package_update": True,
                        "packages": ["qemu-guest-agent"],
                        "hostname": enc["name"],
                        # don't create ubuntu user, but only root
                        "users": [{"name": "root"}],
                        "write_files": managed_files,
                        "runcmd": [
                            "systemctl enable --now qemu-guest-agent",
                            "systemctl restart ssh",
                            "sed -ie 's/- ssh/- [ssh, once]/' /etc/cloud/cloud.cfg",
                            "sed -ie 's/- set_passwords/- [set_passwords, once]/' /etc/cloud/cloud.cfg",
                        ],
                    },
                    f,
                )
            networkconfig_path = target / "network-config"
            networkconfig_path.touch()
            networkconfig = {"version": 1, "config": []}
            for ifacename, ifaceconfig in enc["parameters"][
                "interfaces"
            ].items():
                cfg = {
                    "type": "physical",
                    "name": "eth" + ifacename,
                    "mac_address": ifaceconfig["mac"],
                    "accept-ra": False,
                    "subnets": [],
                }
                for net, netconfig in ifaceconfig["networks"].items():
                    if not netconfig:
                        continue
                    ip_network = ipaddress.ip_network(net)
                    type_ = "static" if ip_network.version == 4 else "static6"
                    prefixlen = (
                        ip_network.max_prefixlen
                        if ifaceconfig["routed"]
                        else ip_network.prefixlen
                    )
                    match (ifaceconfig["routed"], ip_network.version):
                        case (False, 4):
                            gateway = ifaceconfig["gateways"][net]
                            nameservers = ["9.9.9.9", "8.8.8.8"]
                        case (False, 6):
                            gateway = ifaceconfig["gateways"][net]
                            nameservers = [
                                "2620:fe::fe",
                                "2001:4860:4860::8888",
                            ]
                        case (True, 4):
                            gateway = ROUTED_VIRTUAL_GATEWAY_V4
                            nameservers = [ROUTED_VIRTUAL_NAMESERVER]
                        case (True, 6):
                            gateway = ROUTED_VIRTUAL_GATEWAY_V6
                            nameservers = []
                        case _:
                            continue
                    for address in netconfig:
                        cfg["subnets"].append(
                            {
                                "type": type_,
                                "address": f"{address}/{prefixlen}",
                                "gateway": gateway,
                                "dns_nameservers": nameservers,
                            }
                        )
                networkconfig["config"].append(cfg)
            with networkconfig_path.open("w") as f:
                yaml.safe_dump(networkconfig, f)


class SwapSpec(VolumeSpecification):
    suffix = "swap"

    def pre_start(self):
        for pool in self.exists_in_pools():
            if pool != self.desired_pool:
                self.log.info(
                    "delete-outdated-swap", pool=pool, image=self.name
                )
                self.ceph.remove_volume(self.name, pool)

    def start(self):
        self.log.info("start-swap")
        with self.volume.mapped():
            self.cmd(f'mkswap -f -L "{self.suffix}" {self.volume.device}')


class Ceph(object):
    # Attributes on this class can be overriden in a controlled fashion
    # from the sysconfig module. See __init__(). The defaults are here to
    # support testing.

    CREATE_VM = None

    # Those are two different representations of the disks/volumes we manage.
    # The can be treated from client code as well-known structures, so that
    # when the context manager is active then the keys 'root', 'tmp','swp'
    # etc. always exist. The specs will always carry a proper object, but the
    # volumes may be None, as that depends on the bootstrapping of a VM which
    # may not have happened.
    # Otherwise, this code takes care that the volume
    # objects are available to client code as long as a real Ceph volume does
    # exist.
    specs: Dict[str, VolumeSpecification]
    volumes: Dict[str, Optional[Volume]]

    attach_on_enter = True

    def __init__(self, cfg, enc) -> None:
        # Update configuration values from system or test config.
        self.__dict__.update(sysconfig.ceph)
        self.log = log.bind(subsystem="ceph", machine=cfg["name"])

        # enc `parameters` plus additional configs not included in the enc
        self.cfg = cfg
        # the original enc data
        self.enc = enc

        self.rados = None
        self.ioctxs: Dict[str, libceph.Ioctx] = {}
        self.rbd = libceph.RBD()

        self.specs = {}
        self.volumes = {}

    def __enter__(self):
        # Not sure whether it makes sense that we configure the client ID
        # without 'client.': qemu doesn't want to see this, whereas the
        # Rados binding does ... :/
        self.log.debug("connect-rados")
        os.environ["CEPH_ARGS"] = f"--id {self.CEPH_CLIENT} -c {self.CEPH_CONF}"
        self.rados = libceph.Rados(
            conffile=self.CEPH_CONF,
            name="client." + self.CEPH_CLIENT,
            log=self.log,
        )
        self.rados.connect()

        # Keep open ioctx handles to all relevant pools.
        for pool_name in self.rados.list_pools():
            if not valid_rbd_pool_name(pool_name):
                continue
            self.ioctxs[pool_name] = self.rados.open_ioctx(pool_name)

        RootSpec(self)
        SwapSpec(self)
        TmpSpec(self)
        CloudInitSpec(self)

        if self.attach_on_enter:
            self.attach_volumes()

    def __exit__(self, exc_value, exc_type, exc_tb):
        for volume in self.opened_volumes:
            volume.close()
        self.volumes.clear()
        for ioctx in self.ioctxs.values():
            ioctx.close()
        self.ioctxs.clear()
        self.rados.shutdown()

    def attach_volumes(self):
        for spec in self.specs.values():
            self.get_volume(spec)

    def start(self):
        """Perform Ceph-related tasks before starting a VM."""
        for spec in self.specs.values():
            # The pre-start phase guarantees that volumes are not locked
            # and have no watchers, so that they can be deleted if needed.
            if spec.volume:
                spec.volume.unlock()
                spec.volume.close()
            self.log.debug("pre-start", volume_spec=spec.suffix)
            spec.pre_start()

            self.log.debug("ensure-presence", volume_spec=spec.suffix)
            spec.ensure_presence()

            # The start phase guarantees the locks again.
            spec.volume.lock()

            self.log.debug("ensure-size", volume_spec=spec.suffix)
            spec.volume.ensure_size(spec.desired_size)

            self.log.debug("start", volume_spec=spec.suffix)
            spec.start()

    def stop(self):
        """Perform Ceph-related tasks after a VM has been stopped."""
        self.unlock()

    def ensure(self):
        """Perform Ceph-related tasks to maintain a running VM."""
        for spec in self.specs.values():
            spec.ensure()

    def ensure_volume_presence(self, name, pool, size):
        for ioctx in self.ioctxs.values():
            try:
                libceph.Image(ioctx, name)
            except libceph.ImageNotFound:
                continue
            else:
                return
        self.rbd.create(self.ioctxs[pool], name, size)

    def remove_volume(self, name, pool):
        self.rbd.remove(self.ioctxs[pool], name)

    def get_volume(self, spec):
        """(Re-)Attach a volume object for a spec."""
        if volume := self.volumes[spec.suffix]:
            volume.close()
        if spec.exists_in_desired_pool():
            # Performance: prefer the desired pool and avoid superfluous
            # checks. This doesn't trigger the consistency check, but that's
            # way too expensive if we're doing it all the time.
            current_pool = spec.desired_pool
        else:
            current_pool = spec.exists_in_pool()
        if not current_pool:
            return
        self.volumes[spec.suffix] = volume = Volume(
            self, self.ioctxs[current_pool], spec.name
        )
        return volume

    @property
    def opened_volumes(self):
        return filter(None, self.volumes.values())

    def _clean_volume(self, volume):
        for key, candidate in self.volumes.items():
            if candidate is volume:
                self.volumes[key] = None

    def status(self):
        # Report status for CLI usage
        for spec in self.specs.values():
            spec.status()

    def locks(self):
        for volume in self.opened_volumes:
            status = volume.lock_status()
            if not status:
                continue
            yield volume.name, status[1]

    def is_unlocked(self):
        """Returns True if no volume is locked."""
        return all(not volume.lock_status() for volume in self.opened_volumes)

    def locked_by_me(self):
        """Returns True if CEPH_LOCK_HOST holds locks for all volumes."""
        assert self.opened_volumes
        try:
            return all(
                v.lock_status()[1] == self.CEPH_LOCK_HOST
                for v in self.opened_volumes
            )
        except TypeError:  # status[1] not accessible
            return False

    def locked_by(self):
        """Returns a hostname holding all locks or None if not locked.

        Raises ValueError if not all locks are held by same owner.

        """
        lock_owners = set(
            v.lock_status()[1] for v in self.opened_volumes if v.lock_status()
        )
        if not lock_owners:
            return None
        if len(lock_owners) != 1:
            raise ValueError(f"Multiple lock owners: {lock_owners}")
        return lock_owners.pop()

    def lock(self):
        for volume in self.opened_volumes:
            volume.lock()

    def unlock(self):
        """Remove all of *our* volume locks.

        We try to agressively get rid of as many locks as we can, but propagate
        an exception if it occurs.

        This leaves other hosts' locks in place.
        """
        exception = False
        for volume in self.opened_volumes:
            try:
                volume.unlock()
            except Exception:
                volume.log.warning("unlock-failed", exc_info=True)
                exception = True
        if exception:
            raise RuntimeError(
                "Failed to unlock all locks. See log for specific exceptions."
            )

    def force_unlock(self):
        for volume in self.opened_volumes:
            volume.unlock(force=True)

    def auth_cookie(self):
        """This is a cookie that can be used to validate that a party
        has access to Ceph.

        Used to authenticate migration requests.
        """
        c = hashlib.sha1()
        for key in ["root", "swap", "tmp"]:
            # This order needs to stay stable to support the auth cookie
            # between old and new versions of fc.qemu
            vol = self.volumes[key]
            status = [vol.name]
            lock = vol.lock_status()
            if lock:
                status.extend(lock)
            status = ("\0".join(status) + "\0").encode("ascii")
            c.update(status)
        return c.hexdigest()
