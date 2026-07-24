import json
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import Mock

import pytest

from fc.qemu.config import Config, Interface, LinkType
from fc.qemu.hazmat import iproute2
from fc.qemu.hazmat.network import Network, ensure_tap_interface


def test_interface_info_lo():
    info = iproute2.Interface.get("lo", Mock())
    assert info
    assert info.ifname == "lo"
    assert info.altnames == ()


def test_interface_info_missing():
    assert iproute2.Interface.get("nointerface", Mock()) is None
    with pytest.raises(CalledProcessError):
        iproute2.Interface.get("not a valid name", Mock())


def test_interface_ensure():
    interface = Interface(
        name="test",
        linktype=LinkType.bridged,
        network_abbreviation="srv",
        mac="00:00:00:00:00:00",
        network_number=3,
    )
    info = ensure_tap_interface(interface, "sometest", Mock())
    assert info.altnames == ("sometest",)

    info = ensure_tap_interface(interface, "someothertest", Mock())
    assert info.altnames == ("sometest", "someothertest")


def test_prepare_network():
    qemu = Config.model_validate(
        {
            "agent": {
                "network": {"underlay_loopback": "127.0.0.2"},
                "prefix": "/",
            },
            "name": "vm00",
            "id": 2345,
            "interfaces": {
                "srv": {"mac": "00:00:00:00:00:00", "network_number": 3},
                "fe": {"mac": "00:00:00:00:00:00:", "network_number": 2},
            },
        }
    )
    network = Network(qemu, Mock())
    network.start()

    tuntap = iproute2.TunTap.list(Mock())
    tuntap.sort(key=lambda x: x.ifname)
    assert len(tuntap) == 2

    assert tuntap[0].ifname == "tfe2345"
    iface = iproute2.Interface.get("tfe2345", Mock())
    assert iface
    assert iface.altnames == ("fcqemu-vm-2345-net-2",)

    assert tuntap[1].ifname == "tsrv2345"
    iface = iproute2.Interface.get("tsrv2345", Mock())
    assert iface
    assert iface.altnames == ("fcqemu-vm-2345-net-3",)

    network.stop()

    assert not iproute2.Interface.get("tsrv2345", Mock())
    assert not iproute2.Interface.get("tfe2345", Mock())


def test_parse_loopback_dummy_interface():
    model = iproute2.Interface.model_validate(
        {
            "ifindex": 6,
            "ifname": "ul-loopback",
            "flags": ["BROADCAST", "NOARP", "UP", "LOWER_UP"],
            "mtu": 9216,
            "qdisc": "noqueue",
            "operstate": "UNKNOWN",
            "linkmode": "DEFAULT",
            "group": "default",
            "txqlen": 1000,
            "link_type": "ether",
            "address": "66:c2:e6:48:39:99",
            "broadcast": "ff:ff:ff:ff:ff:ff",
            "promiscuity": 0,
            "allmulti": 0,
            "min_mtu": 0,
            "max_mtu": 0,
            "linkinfo": {"info_kind": "dummy"},
            "inet6_addr_gen_mode": "eui64",
            "num_tx_queues": 1,
            "num_rx_queues": 1,
            "gso_max_size": 65536,
            "gso_max_segs": 65535,
            "tso_max_size": 65536,
            "tso_max_segs": 65535,
            "gro_max_size": 65536,
            "gso_ipv4_max_size": 65536,
            "gro_ipv4_max_size": 65536,
        }
    )
    assert model.ifname == "ul-loopback"
    assert isinstance(model.linkinfo, iproute2.GenericLinkInfo)
    assert model.linkinfo.info_data == {}
    assert model.linkinfo.info_kind == "dummy"


def test_parse_sample_interfaces():
    interfaces = (
        Path(__file__).parent.parent / "fixtures" / "iproute2-interfaces.json"
    )
    for i in json.load(interfaces.open()):
        iproute2.Interface.model_validate(i)
