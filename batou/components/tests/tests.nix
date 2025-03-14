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
  testPackage = config.flyingcircus.roles.kvm_host.package;
in
{

  environment.systemPackages =
    let
      testPackages = ([ testPackage ] ++ testPackage.propagatedBuildInputs ++ testPackage.checkInputs);
      PYTHONPATH = testPackage.py.makePythonPath testPackages;
      PATH = lib.makeBinPath testPackages;
    in
    [
      (pkgs.writeShellScriptBin "run-tests" ''
        set -o pipefail
        export PYTHONPATH="${PYTHONPATH}"
        export PATH="${PATH}:${pkgs.openssh}/bin:${pkgs.gnused}/bin"
        cd ${testPackage.src}
        pytest -vv --cov-config=/etc/coveragerc --cov-append -c ${testPackage.src}/pytest.ini "$@"
      '')
    ];

  environment.etc."coveragerc".text = ''
    [run]
    data_file = /tmp/coverage/data

    [html]
    directory = /tmp/coverage/html
  '';

  environment.sessionVariables = {
    FCQEMU_NO_TTY = "true";
  };

  systemd.services.fake-directory = rec {
    description = "A fake directory";
    wantedBy = [ "multi-user.target" ];
    wants = [ "network.target" ];
    after = wants;

    environment = {
      PYTHONUNBUFFERED = "1";
    };

    serviceConfig = {
      Type = "simple";
      Restart = "always";
      ExecStart = "${pkgs.python3Full}/bin/python ${./fakedirectory.py}";
    };
  };

}
