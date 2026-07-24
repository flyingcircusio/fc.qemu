import fcntl
import os
import subprocess
from abc import ABC
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv6Address,
    IPv6Interface,
    ip_interface,
)
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Iterable, Type

from structlog import BoundLogger

from fc.qemu.config import Config, Interface, LinkType
from fc.qemu.util import cmd

from . import iproute2


# The iteration order of items in a set is non-deterministic which is
# a problem the integration tests
def sorted_ipset(
    items: Iterable[IPv4Interface | IPv6Interface],
) -> Iterable[IPv4Interface | IPv6Interface]:
    def key(
        x: IPv4Interface | IPv6Interface,
    ) -> tuple[int, IPv4Address | IPv6Address]:
        return (x.version, x.ip)

    return sorted(items, key=key)


def ensure_tap_interface(
    interface: Interface, altname: str, log: BoundLogger
) -> iproute2.Interface:
    """Ensure a given interface exists as a tap interface with given properties."""
    iinfo = iproute2.Interface.get(interface.name, log)
    if not iinfo:
        cmd(f"ip tuntap add {interface.name} mode tap", log=log)
        iinfo = iproute2.Interface.get(interface.name, log)
    assert iinfo

    if altname not in iinfo.altnames:
        cmd(
            f"ip link property add dev {interface.name} altname {altname}",
            log=log,
        )

    cmd(f"ip link set {interface.name} up", log=log)
    # We previously used to read this from the bridge via /sys/class/net/brXXX
    # but we might create inconsistent intermediate states in any case and
    # this is forward consistent.
    cmd(f"ip link set mtu {interface.mtu} dev {interface.name} up", log=log)
    # XXX we might want to set the mac to avoid mac re-election upon vm attachment
    # (they seem to pick the 02:... MACs from the vx interface though ... but the kernel
    # will randomly generate one on the outer side and this may start with 02: as well.
    # # This doesn't really break anything but may cause the bridges' MAC to change
    # # and cause annoying secondary effects.
    result = iproute2.Interface.get(interface.name, log)
    assert result
    return result


def locked(f: Callable[..., Any]):
    # A simplified, non-reentrant version of the fc.qemu.agent.locked decorator
    def locked_func(self: ConfigureInterface, *args: Any, **kw: Any):
        self.lockfile.touch()
        lock = os.open(self.lockfile, os.O_RDONLY)
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.log.debug(
            "acquire-lock",
            target=self.lockfile,
            result="locked",
        )
        try:
            f(self, *args, **kw)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
            self.log.debug(
                "release-lock",
                target=self.lockfile,
                result="released",
            )

    return locked_func


class ConfigureInterface(ABC):
    """Manage interface for a VM.

    Methods are intended to be idempotent and can be called
    multiple times.

    The base class methods ensure that each interface gets a properly
    TAP device that can be passed to the VM.

    """

    cfg: Config
    interface: Interface
    log: BoundLogger
    lockfile: Path

    def __init__(self, cfg: Config, interface: Interface, log: BoundLogger):
        self.lockfile = (
            cfg.agent.prefix
            / "run"
            / f"qemu.net.{interface.network_abbreviation}.lock"
        )
        self.cfg = cfg
        self.interface = interface
        self.log = log

    def _ip(self, command: str):
        return cmd(f"ip {command}", log=self.log)

    def up(self):
        altname = f"fcqemu-vm-{self.cfg.id}-net-{self.interface.network_number}"
        ensure_tap_interface(self.interface, altname, self.log)

    def down(self):
        try:
            if iinfo := iproute2.Interface.get(self.interface.name, self.log):
                iinfo.delete(self.log)
        except Exception:
            pass


class ConfigureBridgedInterface(ConfigureInterface):
    """The traditional bridged L2 interface (i.e. fe/srv).

    We assume the bridge and the outside configuration is pre-existing
    from fc-nixos.

    """

    def up(self):
        super().up()
        bridge = f"br{self.interface.network_abbreviation}"
        self._ip(f"link set {self.interface.name} master {bridge}")


class ConfigureRoutedInterface(ConfigureInterface):
    """A point-to-point VRF-based routed L3 interface (e.g. pub)."""

    VIRTUAL_GW_V4 = "169.254.83.168"
    VIRTUAL_GW_V6 = "fe80::1"

    def up(self):
        super().up()

        vrf = f"vrf{self.interface.network_abbreviation}"
        self._ip(f"link set {self.interface.name} master {vrf}")

        # add addresses idempotently
        self._ip(
            f"address replace {self.VIRTUAL_GW_V4}/16 dev {self.interface.name}"
        )
        self._ip(
            f"address replace {self.VIRTUAL_GW_V6}/64 dev {self.interface.name}"
        )

        target_routes = {
            ip_interface(addr)
            for addrs in self.interface.networks.values()
            for addr in addrs
        }
        current_routes = {}

        self.log.info(
            "ensure-routes", iface=self.interface.name, vrf=vrf, action="start"
        )

        try:
            current_v4 = iproute2.Route.list(
                family=4, vrf=vrf, dev=self.interface.name, log=self.log
            )
            current_v6 = iproute2.Route.list(
                family=6, vrf=vrf, dev=self.interface.name, log=self.log
            )

            current_routes = {
                route.dst
                for route in (current_v4 + current_v6)
                # ignore routes managed by the kernel
                if route.protocol != "kernel"
            }

            add = target_routes - current_routes
            remove = current_routes - target_routes

            if add or remove:
                self.log.info(
                    "ensure-routes",
                    iface=self.interface.name,
                    vrf=vrf,
                    current_routes=[
                        x.with_prefixlen for x in sorted_ipset(current_routes)
                    ],
                    target_routes=[
                        x.with_prefixlen for x in sorted_ipset(target_routes)
                    ],
                    action="reconciling",
                )

            for route in add:
                self._ip(
                    f"-{route.version} route add {route} "
                    f"dev {self.interface.name} vrf {vrf} proto fc-qemu"
                )
            for route in remove:
                self._ip(
                    f"-{route.version} route del {route} "
                    f"dev {self.interface.name} vrf {vrf}"
                )

            self.log.info(
                "ensure-routes",
                iface=self.interface.name,
                vrf=vrf,
                action="finished",
            )
        except subprocess.CalledProcessError:
            self.log.exception(
                "ensure-routes-failed",
                iface=self.interface,
                vrf=vrf,
                exc_info=True,
            )

    def down(self):
        self._ip(f"address flush dev {self.interface.name}")
        super().down()


class ConfigureDynamicInterface(ConfigureInterface):
    """A bridged, dynamic VXLAN interface (e.g. customer-specific L2)."""

    VXLAN_PORT = 4789

    @locked
    def up(self):
        """
        Attach the dynamic guest interface to the corresponding bridge
        interface, creating the bridge and VXLAN interfaces if
        necessary.
        """
        # Prepare the bridge
        bridge = f"br{self.interface.network_abbreviation}"
        bridge_info = iproute2.Interface.get(bridge, self.log)
        if bridge_info and bridge_info.linkinfo.info_kind != "bridge":
            raise ValueError(
                f"Interface {bridge} is not a bridge interface, aborting!"
            )

        if not bridge_info:
            # create bridge interface
            self._ip(f"link add {bridge} type bridge")
        self._ip(f"link set {bridge} up")

        # Prepare the VXLAN device
        vxlan = f"vx{self.interface.network_abbreviation}"
        vxlan_info = iproute2.Interface.get(vxlan, self.log)
        if vxlan_info and vxlan_info.linkinfo.info_kind != "vxlan":
            raise ValueError(
                f"Interface {vxlan} is not a VXLAN interface, aborting!"
            )
        if not vxlan_info:
            self._ip(
                f"link add {vxlan} type vxlan "
                f"id {self.interface.network_number} "
                f"local {self.cfg.agent.network.underlay_loopback} "
                f"dstport {self.VXLAN_PORT} nolearning"
            )
        self._ip(f"link set {vxlan} addrgenmode none")
        self._ip(f"link set {vxlan} master {bridge}")
        self._ip(
            f"link set {vxlan} type bridge_slave learning off neigh_suppress on"
        )
        self._ip(f"link set {vxlan} up")

        # Prepare the VM interface
        super().up()
        self._ip(f"link set dev {self.interface.name} master {bridge}")

    @locked
    def down(self):
        """Detach the dynamic guest interface.

        If the corresponding bridge interface has no further child
        interfaces, then delete it and the associated VXLAN interface.

        """
        super().down()

        # Tear down the bridge and vxlan if we're the last one using it.
        bridge = f"br{self.interface.network_abbreviation}"
        vxlan = f"vx{self.interface.network_abbreviation}"
        for iface in iproute2.Interface.list(self.log):
            if iface.ifname != vxlan and iface.master == bridge:
                # There's someone else using it, abort.
                self.log.debug(
                    "gc-dynamic-interface",
                    action="none",
                    bridge=bridge,
                    vxlan=vxlan,
                    user=iface.ifname,
                )
                break
        else:
            self.log.debug(
                "gc-dynamic-interface",
                action="cleanup",
                bridge=bridge,
                vxlan=vxlan,
            )
            try:
                self._ip(f"link delete {bridge}")
            except Exception:
                pass
            try:
                self._ip(f"link delete {vxlan}")
            except Exception:
                pass


class Network:
    configurators: dict[LinkType, Type[ConfigureInterface]] = {
        LinkType.bridged: ConfigureBridgedInterface,
        LinkType.routed: ConfigureRoutedInterface,
        LinkType.dynamic: ConfigureDynamicInterface,
    }

    def __init__(self, cfg: Config, log: BoundLogger) -> None:
        self.cfg = cfg
        self.log = log.bind(subsystem="network")

    def __enter__(self):
        pass

    def __exit__(
        self,
        exc_type: Type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ):
        pass

    @property
    def interfaces(self):
        # Work on network interfaces in order, helps avoid deadlocks.
        return [iface for _, iface in sorted(self.cfg.interfaces.items())]

    def start(self):
        for iface in self.interfaces:
            c = self.configurators[iface.linktype](self.cfg, iface, self.log)
            c.up()

    def stop(self):
        for iface in self.interfaces:
            try:
                c = self.configurators[iface.linktype](
                    self.cfg, iface, self.log
                )
                c.down()
            except Exception:
                # Continue if this fails to ensure proper cleanup.
                pass

    def generate_config(self):
        netconfig: list[str] = []
        vhost = '  vhost = "on"' if self.cfg.agent.network.use_vhost else ""
        for _, iface in sorted(self.cfg.interfaces.items()):
            script = self.cfg.agent.network.hooks[
                f"tap-ifup-{iface.linktype.value}"
            ]
            netconfig.append(
                f"""
[device]
  driver = "virtio-net-pci"
  netdev = "{iface.name}"
  mac = "{iface.mac}"

[netdev "{iface.name}"]
  type = "tap"
  ifname = "{iface.name}"
  script = "{script}" # BBB PL-135610 deprecated will be removed in the future.
{vhost}
"""
            )
        return "".join(netconfig)
