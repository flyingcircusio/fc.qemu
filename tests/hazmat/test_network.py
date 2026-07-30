import json
from ipaddress import ip_interface
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import Mock

import pytest

from fc.qemu.config import Config, Interface, LinkType
from fc.qemu.hazmat import iproute2
from fc.qemu.hazmat.network import (
    ConfigureInterface,
    Network,
    ensure_tap_interface,
    locked,
    sorted_ipset,
)


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


def test_sorted_ipset():
    ips = [
        ip_interface("2001:db8::1/64"),
        ip_interface("192.168.1.2/24"),
        ip_interface("10.0.0.1/8"),
        ip_interface("2001:db8::2/64"),
    ]
    result = list(sorted_ipset(ips))
    assert result == [
        ip_interface("10.0.0.1/8"),
        ip_interface("192.168.1.2/24"),
        ip_interface("2001:db8::1/64"),
        ip_interface("2001:db8::2/64"),
    ]


def test_generate_config(tmp_path):
    # Needs to be an actual script, because of pydantic validation
    hook_script = tmp_path / "hook.sh"
    hook_script.write_text("#!/bin/sh\nexit 0\n")
    hook_script.chmod(0o755)

    cfg_data = {
        "agent": {
            "network": {
                "underlay_loopback": "127.0.0.1",
                "use_vhost": True,
                "hooks": {"tap-ifup-bridged": str(hook_script)},
            },
            "prefix": "/",
        },
        "name": "vm00",
        "id": 100,
        "interfaces": {
            "srv": {
                "mac": "52:54:00:12:34:56",
                "network_number": 1,
                "linktype": "bridged",
            },
        },
    }
    qemu = Config.model_validate(cfg_data)
    net = Network(qemu, Mock())
    config = net.generate_config()
    assert (
        config
        == f"""
[device]
  driver = "virtio-net-pci"
  netdev = "tsrv100"
  mac = "52:54:00:12:34:56"

[netdev "tsrv100"]
  type = "tap"
  ifname = "tsrv100"
  script = "{hook_script}" # BBB PL-135610 deprecated will be removed in the future.
  vhost = "on"
"""
    )

    cfg_data["agent"]["network"]["use_vhost"] = False
    qemu_no_vhost = Config.model_validate(cfg_data)
    net_no_vhost = Network(qemu_no_vhost, Mock())
    config_no_vhost = net_no_vhost.generate_config()
    assert (
        config_no_vhost
        == f"""
[device]
  driver = "virtio-net-pci"
  netdev = "tsrv100"
  mac = "52:54:00:12:34:56"

[netdev "tsrv100"]
  type = "tap"
  ifname = "tsrv100"
  script = "{hook_script}" # BBB PL-135610 deprecated will be removed in the future.

"""
    )


def test_prepare_bridged_network(tmp_path):
    qemu = Config.model_validate(
        {
            "agent": {
                "network": {"underlay_loopback": "127.0.0.2"},
                "prefix": str(tmp_path),
            },
            "name": "vm00",
            "id": 2348,
            "interfaces": {
                "srv": {
                    "mac": "00:00:00:00:00:03",
                    "network_number": 5,
                    "linktype": "bridged",
                },
            },
        }
    )
    network = Network(qemu, Mock())
    network.start()

    iface = iproute2.Interface.get("tsrv2348", Mock())
    assert iface
    assert iface.altnames == ("fcqemu-vm-2348-net-5",)
    assert iface.master == "brsrv"

    network.stop()

    assert not iproute2.Interface.get("tsrv2348", Mock())


def test_prepare_routed_network(tmp_path):
    qemu = Config.model_validate(
        {
            "agent": {
                "network": {"underlay_loopback": "127.0.0.2"},
                "prefix": str(tmp_path),
            },
            "name": "vm00",
            "id": 2346,
            "interfaces": {
                "pub": {
                    "mac": "00:00:00:00:00:01",
                    "network_number": 1,
                    "linktype": "routed",
                    "networks": {
                        "10.0.0.0/24": ["10.0.0.5"],
                        "2001:db8:1::/64": ["2001:db8:1::5"],
                    },
                },
            },
        }
    )
    network = Network(qemu, Mock())
    network.start()

    iface = iproute2.Interface.get("tpub2346", Mock())
    assert iface
    assert iface.altnames == ("fcqemu-vm-2346-net-1",)
    assert iface.master == "vrfpub"

    v4_routes = iproute2.Route.list(
        family=4, vrf="vrfpub", dev="tpub2346", log=Mock()
    )
    assert any(
        r.dst == ip_interface("10.0.0.5/32") and r.protocol == "fc-qemu"
        for r in v4_routes
    )

    v6_routes = iproute2.Route.list(
        family=6, vrf="vrfpub", dev="tpub2346", log=Mock()
    )
    assert any(
        r.dst == ip_interface("2001:db8:1::5/128") and r.protocol == "fc-qemu"
        for r in v6_routes
    )

    network.stop()

    assert not iproute2.Interface.get("tpub2346", Mock())


def test_prepare_dynamic_network(tmp_path):
    qemu = Config.model_validate(
        {
            "agent": {
                "network": {"underlay_loopback": "127.0.0.2"},
                "prefix": str(tmp_path),
            },
            "name": "vm00",
            "id": 2347,
            "interfaces": {
                "dyn1": {
                    "mac": "00:00:00:00:00:02",
                    "network_number": 42,
                    "linktype": "dynamic",
                },
            },
        }
    )
    network = Network(qemu, Mock())
    network.start()

    iface = iproute2.Interface.get("tdyn12347", Mock())
    assert iface
    assert iface.altnames == ("fcqemu-vm-2347-net-42",)
    assert iface.master == "brdyn1"

    bridge = iproute2.Interface.get("brdyn1", Mock())
    assert bridge
    assert bridge.linkinfo.info_kind == "bridge"

    vxlan = iproute2.Interface.get("vxdyn1", Mock())
    assert vxlan
    assert vxlan.linkinfo.info_kind == "vxlan"
    assert vxlan.master == "brdyn1"

    network.stop()

    assert not iproute2.Interface.get("tdyn12347", Mock())
    assert not iproute2.Interface.get("brdyn1", Mock())
    assert not iproute2.Interface.get("vxdyn1", Mock())


def test_locked_decorator(tmp_path):
    class DummyConfigurator(ConfigureInterface):
        called = False

        @locked
        def do_something(self):
            self.called = True

    interface = Interface(
        name="testlock",
        linktype=LinkType.bridged,
        network_abbreviation="srv",
        mac="00:00:00:00:00:00",
        network_number=1,
    )
    qemu = Config.model_validate(
        {
            "agent": {
                "network": {"underlay_loopback": "127.0.0.1"},
                "prefix": str(tmp_path),
            },
            "name": "vm00",
            "id": 100,
            "interfaces": {
                "srv": {
                    "mac": "00:00:00:00:00:00",
                    "network_number": 1,
                    "linktype": "bridged",
                },
            },
        }
    )
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    configurator = DummyConfigurator(qemu, interface, Mock())
    configurator.do_something()
    assert configurator.called
    assert configurator.lockfile.exists()
