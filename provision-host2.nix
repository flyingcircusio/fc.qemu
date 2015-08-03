{ config, lib, pkgs, ... }:

{

    imports = [
        /vagrant/provision.nix
    ];

    networking.interfaces.eth1.ip4 = [
        { address = "192.168.50.5"; prefixLength = 24; }
    ];

    networking.hostName = "host2";

    services.ceph.osdid = "2";
    services.ceph.osduuid = "131FB4E6-C8D3-43EE-A07A-D01BCCE34D10";

}
