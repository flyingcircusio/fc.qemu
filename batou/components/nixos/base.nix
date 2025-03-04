{
  config,
  lib,
  pkgs,
  ...
}:
let
  fclib = config.fclib;
in
{

  flyingcircus.encServices = [
    {
      address = "host1";
      ips = [ "10.12.0.6" ];
      location = "test";
      service = "consul_server-server";
    }
    {
      address = "host1";
      ips = [ "10.12.0.6" ];
      location = "test";
      service = "ceph_mon-mon";
    }
  ];

  systemd.timers.logrotate.enable = lib.mkForce false;
  flyingcircus.agent.enable = lib.mkForce false;

  networking.extraHosts = ''
    10.12.0.6 host1.srv.test.gocept.net host1
    10.12.0.10 host2.srv.test.gocept.net host2
  '';

  flyingcircus.static.ceph.fsids.test.test = "d118a9a4-8be5-4703-84c1-87eada2e6b60";

  flyingcircus.services.consul.advertiseAddr = lib.head fclib.network.srv.v4.addresses;
  flyingcircus.services.consul.bindAddr = lib.head fclib.network.srv.v4.addresses;
  flyingcircus.services.consul.dc = "test";

  system.activationScripts.updateTransientHostname = ''
    ${pkgs.systemd}/bin/hostnamectl set-hostname --transient $(${pkgs.systemd}/bin/hostnamectl status --static)
    '';

}
