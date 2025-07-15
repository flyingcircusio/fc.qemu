import configparser
import os.path
import re


def match_many(pattern: str, input: list[str]):
    """Apply a regexp match to a list of strings.

    Returns an iterator of simple objects for strings that match the
    pattern. The objects allow attribute access to the named groups
    of the regular expression. Non-matching strings are filtered out.

    """
    p = re.compile(pattern)
    return filter(None, map(p.match, input))


def section_matches_as_dicts(
    cp: configparser.ConfigParser, pattern: str
) -> dict:
    """Transform a set of sections that match a pattern into a dict of dicts.

    The match must provide a "section" group that identifies the key for the
    section. The match must cover the whole key.

    """
    result = {}
    for m in match_many(pattern, cp.sections()):
        result[m.groupdict()["section"]] = dict(cp.items(m.string))
    return result


class SysConfig(object):
    """A global config state registry.

    This is used to manage system-specific configuration overrides
    for values used in various places within fc.qemu.

    This provides the code for loading all those options from a central
    config file and to allow tests overriding those values gracefully.
    """

    cp: configparser.ConfigParser

    def __init__(self):
        self.qemu = {}
        self.ceph = {}
        self.agent = {}

    def read_config_files(self):
        """Tries to open fc-qemu.conf at various location."""
        self.cp = configparser.ConfigParser()
        self.cp.read(os.path.dirname(__file__) + "/default.conf")
        self.cp.read("/etc/qemu/fc-qemu.conf")
        if "qemu" not in self.cp.sections():
            raise RuntimeError(
                "error while reading config file: section [qemu] not found"
            )

    def load_system_config(self):
        self.read_config_files()

        self.qemu["migration_address"] = self.cp.get(
            "qemu", "migration-address"
        )
        self.qemu["require_kvm"] = bool(self.cp.get("qemu", "accelerator"))
        self.qemu["vnc"] = self.cp.get("qemu", "vnc")
        self.qemu["max_downtime"] = self.cp.getfloat("qemu", "max-downtime")
        self.qemu["migration_bandwidth"] = self.cp.getint(
            "qemu", "migration-bandwidth"
        )
        self.qemu["vm_max_total_memory"] = self.cp.getint(
            "qemu", "vm-max-total-memory"
        )
        self.qemu["vm_expected_overhead"] = self.cp.getint(
            "qemu", "vm-expected-overhead"
        )

        self.qemu["block_throttle"] = bt = {}
        for section, items in section_matches_as_dicts(
            self.cp, r"block-throttle-(?P<section>[a-zA-Z\.0-9]+)"
        ).items():
            bt[section] = {}
            for k, v in items.items():
                bt[section][k] = int(v)

        # Consul
        self.agent["consul_token"] = self.cp.get("consul", "access-token")
        self.agent["consul_event_threads"] = self.cp.getint(
            "consul", "event-threads"
        )

        # Qemu
        self.agent["accelerator"] = self.cp.get("qemu", "accelerator")
        self.agent["machine_type"] = self.cp.get("qemu", "machine-type")
        self.agent["migration_ctl_address"] = self.cp.get(
            "qemu", "migration-ctl-address"
        )
        self.agent["binary_generation"] = self.cp.getint(
            "qemu", "binary-generation"
        )
        self.agent["timeout_graceful"] = self.cp.getint(
            "qemu", "timeout-graceful"
        )
        self.agent["vhost"] = self.cp.getboolean("qemu", "vhost")

        self.agent["network_hooks"] = nh = {}
        for key, path in self.cp.items("network"):
            nh[key.lstrip("tap-")] = path

        # Ceph
        self.agent["this_host"] = self.cp.get("ceph", "lock_host")
        self.agent["ceph_id"] = self.cp.get("ceph", "client-id")

        self.ceph["CEPH_CLIENT"] = self.cp.get(
            "ceph", "client-id", fallback="admin"
        )
        self.ceph["CEPH_CLUSTER"] = self.cp.get(
            "ceph", "cluster", fallback="ceph"
        )
        self.ceph["CEPH_CONF"] = self.cp.get("ceph", "ceph-conf")
        self.ceph["CEPH_LOCK_HOST"] = self.cp.get("ceph", "lock_host")
        self.ceph["CREATE_VM"] = self.cp.get("ceph", "create-vm")
        self.ceph["MKFS_XFS"] = self.cp.get("ceph", "mkfs-xfs")
        self.ceph["MKFS_VFAT"] = self.cp.get("ceph", "mkfs-vfat")


sysconfig = SysConfig()
