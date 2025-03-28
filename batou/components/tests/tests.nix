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
    # This is a horrible dance for two reasons:
    # 1. pytest doesn't properly inherit the PATH environment from the propagatedBuildInputs.
    #    We might want to reconsider using an additional buildPythonApplication with pytest
    #    added to the primary dependencies (propagatedBuildInputs)
    # 2. There's a bug(?) in stdenv.mkDerivation that causes external access to the
    #    attribute's items to end up with their .dev outputs ... -_-
    let
      testPackages = ([ testPackage ] ++
       (map
        (x: builtins.removeAttrs x [ "outputSpecified" ])
        testPackage.propagatedBuildInputs) ++
      testPackage.nativeCheckInputs);
      PYTHONPATH = testPackage.py.makePythonPath testPackages;
      PATH = lib.makeBinPath testPackages;
    in
    [
      (pkgs.writeShellScriptBin "run-tests" ''
        set -o pipefail
        export PYTHONPATH="${PYTHONPATH}"
        export PATH="${PATH}"
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
