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
