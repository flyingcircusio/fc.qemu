let
  # Pin nixpkgs, see pinning tutorial for more details
  pkgs = import <fc>;

in pkgs.nixosTest ({
  # NixOS tests are run inside a virtual machine, and here we specify system of the machine.
  system = "x86_64-linux";

  nodes = {

    kvm1 = { config, pkgs, ... }: {};

  };

  # Disable linting for simpler debugging of the testScript
  skipLint = true;

  testScript = ''
    import json
    import sys

    start_all()

    kvm1.wait_for_unit("default.target")
    kvm.succeed("fc-qemu ls")

  '';
})
