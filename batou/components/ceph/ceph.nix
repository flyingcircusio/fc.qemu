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
in
{

  services.consul.extraConfig = {
    bootstrap_expect = lib.mkForce 1;
  };
  flyingcircus.roles.consul_server = {
    enable = true;
    publicAddress = "host1";
  };

  services.nginx.virtualHosts."host1.fe.test.fcio.net" = {
    enableACME = lib.mkForce false;
    addSSL = true;
    sslCertificateKey = "/var/run/nginx/self-signed.key";
    sslCertificate = "/var/run/nginx/self-signed.crt";
  };
  systemd.services.nginx.serviceConfig.ExecStartPre = (
    pkgs.writeShellScript "setup-private-key" ''
      set -ex
      ${pkgs.openssl}/bin/openssl req -nodes -x509 -newkey rsa:4096 -keyout /var/run/nginx/self-signed.key -out /var/run/nginx/self-signed.crt -sha256 -days 365 -subj '/CN=host1.fe.test.fcio.net'
    ''
  );

  environment.systemPackages = [
    (pkgs.writeShellScriptBin "cleanup-ceph" ''
      systemctl stop fc-ceph-mon
      systemctl stop fc-ceph-mgr
      systemctl stop fc-ceph-osd@0.service
      umount /srv/ceph/mgr/ceph-host1
      umount /srv/ceph/mon/ceph-host1
      umount /srv/ceph/osd/ceph-0
      vgremove vgjnl00 -y
      vgremove vgosd-0 -y
      losetup -D
      rm -rf /ceph
    '')
  ];

  # Try to disable as many cronjobs as possible as they're really just in the
  # way in the test suite.
  systemd.timers.fc-ceph-load-vm-images.enable = lib.mkForce false;
  systemd.timers.fc-ceph-mon-update-client-keys.enable = lib.mkForce false;
  systemd.timers.fc-ceph-clean-deleted-vms.enable = lib.mkForce false;
  systemd.timers.fc-ceph-purge-old-snapshots.enable = lib.mkForce false;

  flyingcircus.roles.ceph_osd.network = fclib.network.srv;

  systemd.services.fc-ceph-mon.wantedBy = lib.mkForce [];
  systemd.services.fc-ceph-mgr.wantedBy = lib.mkForce [];

  systemd.services.fc-ceph-mon.wants = lib.mkForce [ fclib.network.srv.addressUnit ];
  systemd.services.fc-ceph-mon.after = lib.mkForce [ fclib.network.srv.addressUnit ];
  systemd.services.fc-ceph-mgr.wants = lib.mkForce [ fclib.network.srv.addressUnit ];
  systemd.services.fc-ceph-mgr.after = lib.mkForce [ fclib.network.srv.addressUnit ];
  systemd.services.fc-ceph-osds-all.wants = lib.mkForce [ fclib.network.srv.addressUnit ];
  systemd.services.fc-ceph-osds-all.after = lib.mkForce [ fclib.network.srv.addressUnit ];
  systemd.services."fc-ceph-osd@".wants = lib.mkForce [ fclib.network.srv.addressUnit ];
  systemd.services."fc-ceph-osd@".after = lib.mkForce [ fclib.network.srv.addressUnit ];

  flyingcircus.roles.ceph_osd = {
    enable = true;
    cephRelease = "nautilus";
  };
  flyingcircus.roles.ceph_mon = {
    enable = true;
    cephRelease = "nautilus";
  };
  flyingcircus.static.ceph.fsids.test.test = "d118a9a4-8be5-4703-84c1-87eada2e6b60";
  flyingcircus.services.ceph.extraSettings = {
    monClockDriftAllowed = 1;
  };
  flyingcircus.services.ceph.client = {
    mons = [ "host1" ];
    network = fclib.network.srv;
    fsId = "20cd8cd8-4854-469b-a9c0-daa8ce4c0dff";
  };


}
