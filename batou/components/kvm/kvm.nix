{
  config,
  lib,
  pkgs,
  ...
}:

# This file needs to be kept (somewhat) in sync with our
# `kvm_host_ceph-nautilus.nix` in the platform.
let
  fclib = config.fclib;
  testPackage = pkgs.fc.qemu-nautilus.overrideAttrs (old: {
    version = "dev";
    # builtins.toPath (testPath + "/.")
    # for tests:
    src = /home/developer/fc.qemu/.;
  });
  testkey = {
    priv = ''
      -----BEGIN OPENSSH PRIVATE KEY-----
      b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
      QyNTUxOQAAACDEL3cs6kZncaVSHZ+DvTMkiohC3j7MP3ad7Jh40Js6twAAAJjFq84bxavO
      GwAAAAtzc2gtZWQyNTUxOQAAACDEL3cs6kZncaVSHZ+DvTMkiohC3j7MP3ad7Jh40Js6tw
      AAAEDbcHXRiL0+aMh1TaEhnXKqjVpOru/jyfW1Zb6ENAGOcsQvdyzqRmdxpVIdn4O9MySK
      iELePsw/dp3smHjQmzq3AAAAEG1hY2llakBta2ctcmF6ZXIBAgMEBQ==
      -----END OPENSSH PRIVATE KEY-----
    '';
    pub = ''
      ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMQvdyzqRmdxpVIdn4O9MySKiELePsw/dp3smHjQmzq3 testkey@localhost
    '';
  };
in
{

  flyingcircus.roles.kvm_host = {
    package = testPackage;
    network = fclib.network.srv;

    enableS3Proxy = false;

    # We want migrations to be slowish so we can test enough code
    # that monitors the migration. Try to push it past 60 seconds.
    migrationBandwidth = 22500;

    # Use the default flags defined by fc-qemu regardless of
    # what the platform sets or the fc-qemu unit tests will fail.
    mkfsXfsFlags = null;
  };

  systemd.services.fc-qemu-scrub.wantedBy = lib.mkForce [ ];
  systemd.timers.fc-qemu-scrub.enable = false;
  systemd.services.fc-qemu-report-cpus.enable = false;
  systemd.services.fc-qemu-report-cpus.wantedBy = lib.mkForce [ ];

  environment.etc."kvm/kvm-ifup" = {
    text = lib.mkForce ''
      #!${pkgs.stdenv.shell}
      echo "if up"
    '';
  };
  environment.etc."kvm/kvm-ifdown" = {
    text = lib.mkForce ''
      #!${pkgs.stdenv.shell}
      echo "if down"
    '';
  };
  environment.etc."kvm/kvm-ifup-vrf" = {
    text = lib.mkForce ''
      #!${pkgs.stdenv.shell}
      ${pkgs.iproute2}/bin/ip link set $1 master vrfpub
      ${pkgs.iproute2}/bin/ip link set $1 up
    '';
  };
  environment.etc."kvm/kvm-ifdown-vrf" = {
    text = lib.mkForce ''
      #!${pkgs.stdenv.shell}
      ${pkgs.iproute2}/bin/ip link set $1 nomaster
    '';
  };

  flyingcircus.services.ceph.client = {
    mons = [ "host1" ];
    network = fclib.network.srv;
    fsId = "20cd8cd8-4854-469b-a9c0-daa8ce4c0dff";
  };

  environment.etc = {
    "ssh_key" = {
      text = testkey.priv;
      mode = "0400";
    };
    "ssh_key.pub" = {
      text = testkey.pub;
      mode = "0444";
    };
  };

  users.users.root = {
    openssh.authorizedKeys.keys = [
      testkey.pub
    ];
  };

}
