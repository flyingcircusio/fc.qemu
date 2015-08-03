{ config, lib, pkgs, ... }:

{

    imports = [
        /vagrant/provision.nix
    ];

    networking.interfaces.eth1.ip4 = [
        { address = "192.168.50.4"; prefixLength = 24; }
    ];

    networking.hostName = "host1";

    services.ceph.osdid = "1";
    services.ceph.osduuid = "51C6E301-531F-4595-8369-9083CBFC2AA3";

}
