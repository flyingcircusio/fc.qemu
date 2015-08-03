{ config, lib, pkgs, ... }: with config;

{

    networking.extraHosts = ''
        127.0.0.1   consul-ext.service.services.vgr.consul.local
    '';

    # Consul
    services.consul.enable = true;
    services.consul.extraConfig = {
        acl_master_token = "4369DAF2-6D0B-4AC8-BB32-94DE29B7FE1E";
        server = true;
        bootstrap = true;
        datacenter = "services";
        acl_default_policy = "deny";
        start_join = ["192.168.50.4"];
    };
    services.consul.webUi = true;
    services.consul.interface.bind = "eth1";

}
