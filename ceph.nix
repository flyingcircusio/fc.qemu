{ config, lib, pkgs, ... }:

let

    cephconf = pkgs.writeText "ceph.conf" ''
[global]
fsid = e75c89b0-46d4-4f83-9e30-0407e70be8be
public network = 192.168.50.0/24
filestore merge threshold = 40
filestore split multiple = 8
max open files = 65535
mon host = host1, host2
osd heartbeat grace = 10
osd heartbeat interval = 4
osd pool default min size = 1
osd pool default size = 2
# place this between "nearfull" (0.85) and "full" (0.95)
osd backfill full ratio = 0.9

debug auth = 0
debug mon = 0
debug monc = 0
debug ms = 0
debug paxos = 0

auth cluster required = none
auth service required = none
auth client required = none

mon initial members = host1

[client]
log file = /var/log/ceph/client.log
rbd cache = true
rbd cache size = 67108864
rbd cache max dirty = 50331648
rbd cache target dirty = 33554432
rbd default format = 2

[mon]
mon addr = 192.168.50.4:6789,192.168.50.5:6789
mon data = /srv/ceph/mon/$cluster-$id
mon data avail crit = 2
mon data avail warn = 5
mon pg warn max per osd = 3000

[osd.${config.services.ceph.osdid}]
osd uuid = ${config.services.ceph.osduuid}
osd data = /srv/ceph/osd/ceph-${config.services.ceph.osdid}
filestore max sync interval = 30
    '';
in

{

  options = {

    services.ceph = {

      osdid = lib.mkOption {
        type = lib.types.str;
        default = false;
        description = ''
          Numeric ID of this host to suffix OSD, etc. with.
        '';
      };

      osduuid = lib.mkOption {
        type = lib.types.str;
        default = false;
        description = ''
          Numeric ID of this host to suffix OSD, etc. with.
        '';
      };
    };
  };

  config = {
    networking.extraHosts = ''
        192.168.50.4   host1
        192.168.50.5   host2
    '';

    environment.systemPackages = [
          pkgs.ceph
          pkgs.libceph
          pkgs.python
          pkgs.qemu
      ];

    environment.shellInit =
      ''
          export PYTHONPATH=/run/current-system/sw/lib/python2.7/site-packages

      '';

    systemd.services.ceph-mon = {
      wantedBy = [ "multi-user.target" ];
      path = [ pkgs.ceph ];
      preStart = ''
        mkdir -p /etc/ceph
        ln -sf ${cephconf} /etc/ceph/ceph.conf
        MONDIR="/srv/ceph/mon/ceph-${config.networking.hostName}"
        if [ ! -e $MONDIR/done ]; then
          mkdir -p $MONDIR
          rm -f /tmp/monmap
          monmaptool --create --add host1 192.168.50.4 --fsid e75c89b0-46d4-4f83-9e30-0407e70be8be /tmp/monmap
          ceph-mon --mkfs -i ${config.networking.hostName} --monmap /tmp/monmap
          touch $MONDIR/done
        fi
      '';
      serviceConfig = {
        ExecStart = "${pkgs.ceph}/bin/ceph-mon -d -i ${config.networking.hostName}";
        Restart = "always";
        RestartSec = "5s";
      };
    };

    systemd.services.ceph-osd = {
      wantedBy = [ "multi-user.target" ];
      path = [ pkgs.ceph ];
      preStart = ''
        mkdir -p /etc/ceph
        ln -sf ${cephconf} /etc/ceph/ceph.conf
        OSDDIR="/srv/ceph/osd/ceph-${config.services.ceph.osdid}"
        if [ ! -e $OSDDIR/done ]; then
          mkdir -p $OSDDIR
          ceph osd create ${config.services.ceph.osduuid} ${config.services.ceph.osdid}
          ceph-osd -i ${config.services.ceph.osdid} --mkfs --osd-uuid ${config.services.ceph.osduuid}
          ceph osd crush add-bucket ${config.networking.hostName} host
          ceph osd crush move ${config.networking.hostName} root=default
          ceph osd crush add osd.${config.services.ceph.osdid} 1.0 host=${config.networking.hostName}
          touch $OSDDIR/done
        fi
      '';
      serviceConfig = {
        ExecStart = "${pkgs.ceph}/bin/ceph-osd -d id=${config.services.ceph.osdid}";
        Restart = "always";
        RestartSec = "5s";
      };
    };
  };

}
