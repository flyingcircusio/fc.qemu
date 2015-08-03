{ config, lib, pkgs, ... }: with config;

{

    imports = [
        /vagrant/consul.nix
        /vagrant/ceph.nix
    ];

    programs.ssh.extraConfig = ''
        UserKnownHostsFile /dev/null
        StrictHostKeyChecking no
        '';

    # Those packages are only installed as binaries (and includes and libs)
    # to support building software that links to them. They do not indicate
    # that the typical services (e.g. server processes) are installed.
    environment.systemPackages = [
        pkgs.gcc
    ];

    users.extraUsers.root.openssh.authorizedKeys.keys = [
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC8dJ9QJJu2Ro+3cp0BTX8Q7tzi5xuKTfkRVCtbxjC0070kRGiJKNXSIWR8VXXf23+v3WTzBFZjVGKQhaoEjoCi20UbgoRmoqv3j1wxmKxFYwmGnCAQpIbRTajXyXaE9Rby0V2D6XIvZaMUT2YBJ39f0nczfJRK0WQMvimFumUFQBxCAwKODi9UeZ7vG5BRU6I1gppBFecVp9W/i5vLvVeu+DKBo1hgdSdJ13U4zABxfLHRo/kJkbzSPfOD/dxu2vo7XTxb7VEOQxlxx+kZI/BQ4breGY74NbAfypALZbd4Ic3zQHBLFZPB85AMtvKG6hEgSYC85YZ2SH76Ko1PbIAL root@host1"
    ];

    jobs.fcio-qemu-stubs-base = {
        description = "Create stub directories for the FC IO qemu environment";
        task = true;

        startOn = "started networking";

        script =
            ''
                install -d -o vagrant /etc/qemu/vm
            '';
    };


}
