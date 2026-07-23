import os
from enum import Enum
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, field_validator, model_validator
from pydantic.networks import IPvAnyAddress, IPvAnyNetwork

from fc.qemu.typing import InterfaceDict


class Config(BaseModel):
    """A VM config based on the ENC parameters and extended
    with additional computed options.
    """

    # General agent config
    agent: "Agent"

    # VM-specific config
    id: int
    interfaces: dict[str, "Interface"]

    @model_validator(mode="before")
    @classmethod
    def add_interface_names(
        cls, model: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        for net, net_dict in model["interfaces"].items():
            net_dict["name"] = f"t{net}{model['id']}"
            net_dict["network_abbreviation"] = net
        return model


class Agent(BaseModel):
    network: "Network"
    prefix: Path  # usually `/` but can be switched for unit testing


class Network(BaseModel):
    use_vhost: bool = False
    hooks: dict[str, Path] = {}
    underlay_loopback: IPv4Address

    @field_validator("hooks")
    @classmethod
    def hook_scripts_must_exists_and_be_executable(
        cls, v: dict[str, Path]
    ) -> dict[str, Path]:
        for p in v.values():
            if not p.exists():
                raise ValueError(f"Network hook script {p} does not exist.")
            if not os.access(p, os.X_OK):
                raise ValueError(f"Network hook script {p} is not executable.")
        return v


class LinkType(str, Enum):
    # Keep in sync with directory LinkType
    bridged = "bridged"
    routed = "routed"
    dynamic = "dynamic"


class Interface(BaseModel):
    linktype: LinkType
    mac: str
    name: str  # computed by parent model
    network_abbreviation: str
    network_number: int
    networks: dict[IPvAnyNetwork, list[IPvAnyAddress]] = {}

    # XXX missing in directory API
    mtu: int = 1500

    @model_validator(mode="before")
    @classmethod
    def migrate_linktype(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = cast(InterfaceDict, data)
        if "linktype" not in data:
            data["linktype"] = "routed" if data.get("routed") else "bridged"
        return data

    @field_validator("networks", mode="after")
    @classmethod
    def ensure_consistent_networks(
        cls, networks: dict[IPvAnyNetwork, list[IPvAnyAddress]]
    ) -> dict[IPvAnyNetwork, list[IPvAnyAddress]]:
        for network, addresses in networks.items():
            for address in addresses:
                assert address in network
        return networks
