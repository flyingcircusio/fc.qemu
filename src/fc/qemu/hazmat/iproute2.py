"""A pydantic-based wrapper to interact with `ip` style utils.

Provides access to fetch and list objects and perform some
basic operations.

This is not intended to be a full wrapper. The hazmat code is expected to
call `ip` directly to manipulate the state and then use this wrapper to
retrieve status information.

The main objects are:

- Interface (incl. link details)
- TunTap
- Route

"""

from collections.abc import Sequence
from ipaddress import ip_interface
from subprocess import CalledProcessError
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, RootModel, model_validator
from pydantic.networks import IPvAnyAddress, IPvAnyInterface
from structlog import BoundLogger

from fc.qemu.typing import DefaultRouteDict
from fc.qemu.util import cmd, model_from_json_cmd


class MissingLinkInfo(BaseModel):
    info_kind: None = None


class Interface(BaseModel):
    """A network interface."""

    ifname: str
    altnames: tuple[str, ...] = ()
    # Relying on smart mode here - discriminators are very unwieldy
    # for this.
    linkinfo: "BridgeLinkInfo | GenericLinkInfo | MissingLinkInfo" = (
        MissingLinkInfo()
    )
    master: str | None = None

    # ip -d -j l show dev foobar
    # [
    #   {
    #     "ifindex": 327,
    #     "ifname": "foobar",
    #     "flags": [
    #       "BROADCAST",
    #       "MULTICAST"
    #     ],
    #     "mtu": 1500,
    #     "qdisc": "noop",
    #     "operstate": "DOWN",
    #     "linkmode": "DEFAULT",
    #     "group": "default",
    #     "txqlen": 1000,
    #     "link_type": "ether",
    #     "address": "32:d7:f0:59:83:92",
    #     "broadcast": "ff:ff:ff:ff:ff:ff",
    #     "altnames": [
    #       "asdf",
    #       "vm-1235",
    #       "vlan-123431341",
    #       "vm-1235-vlan-123312321"
    #     ]
    #    "linkinfo": {
    #      "info_kind": "bridge",
    #      "info_data": {
    #        "forward_delay": 1500,
    #        "hello_time": 200,
    #        "max_age": 2000,
    #        "ageing_time": 30000,
    #        "stp_state": 0,
    #        ...
    #       }
    #     }
    #   }
    # ]

    @classmethod
    def get(cls, ifname: str, log: BoundLogger) -> Self | None:
        interfaces = []
        try:
            interfaces = model_from_json_cmd(
                RootModel[list[cls]],
                f"ip -d -j link show dev {ifname}",
                log=log,
            ).root
        except CalledProcessError as e:
            if e.returncode == 1 and "does not exist" in e.output:
                pass
            else:
                raise
        if not interfaces:
            return None
        assert len(interfaces) == 1
        interface = interfaces[0]
        assert interface.ifname == ifname
        return interface

    @classmethod
    def list(cls, log: BoundLogger) -> list[Self]:
        interfaces = model_from_json_cmd(
            RootModel[list[cls]], "ip -d -j link show", log=log
        ).root
        return interfaces

    def delete(self, log: BoundLogger):
        cmd(f"ip link set {self.ifname} nomaster", log=log)
        cmd(f"ip link set {self.ifname} down", log=log)
        cmd(f"ip link delete {self.ifname}", log=log)


class BridgeLinkInfo(BaseModel):
    info_kind: Literal["bridge"]
    info_data: "BridgeInfo"


class BridgeInfo(BaseModel):
    bridge_id: str


class VXLANLinkInfo(BaseModel):
    info_kind: Literal["vxlan"]
    info_data: "VXLANInfo"


class VXLANInfo(BaseModel):
    id: int
    port: int
    learning: bool


class GenericLinkInfo(BaseModel):
    info_kind: str
    info_data: dict[str, Any]


class TunTap(BaseModel):
    ifname: str
    flags: tuple[str, ...]

    # root@host1 .../developer/fc.qemu # ip -j tuntap show | jq
    # [
    #   {
    #     "ifname": "tfe2345",
    #     "flags": [
    #       "tap",
    #       "one_queue",
    #       "vnet_hdr",
    #       "persist"
    #     ]
    #   },
    #   ...
    # ]

    @classmethod
    def list(cls, log: BoundLogger) -> list[Self]:
        interfaces = model_from_json_cmd(
            RootModel[list[cls]], "ip -j tuntap show", log=log
        ).root
        return interfaces


class Route(BaseModel):
    dst: IPvAnyInterface
    dev: str | None = None
    scope: str | None = None
    prefsrc: IPvAnyAddress | None = None
    flags: Sequence[str] = ()
    protocol: str
    metric: int | None = None
    gateway: IPvAnyAddress | None = None

    @model_validator(mode="before")
    @classmethod
    def translate_default_dst(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = cast(dict[str, str], data)
            if data["dst"] == "default":
                route = cast(DefaultRouteDict, data)
                gateway = ip_interface(route["gateway"])
                if gateway.version == 4:
                    data["dst"] = "0.0.0.0/0"
                else:
                    data["dst"] = "::/0"
        return data

    @classmethod
    def list(
        cls,
        family: Literal[4, 6] | None = None,
        *,
        log: BoundLogger,
        **filter_kw: str,
    ) -> list[Self]:
        family_arg = ""
        if family:
            family_arg = f"-{family}"
        filter_args = " ".join(f"{key} {filter_kw[key]}" for key in filter_kw)

        routes = model_from_json_cmd(
            RootModel[list[cls]],
            f"ip -j {family_arg} route show {filter_args}",
            log=log,
        ).root
        return routes
