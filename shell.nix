let
  # Intended to be run in an environment where a "proper" fc-nixos `dev-setup`
  # has been run.
  pkgs = import <fc> {};

  py = pkgs.python2Packages;
  py_structlog = py.buildPythonPackage rec {
    pname = "structlog";
    version = "16.1.0";
    src = py.fetchPypi {
      inherit pname version;
      sha256 = "00dywyg3bqlkrmbrfrql21hpjjjkc4zjd6xxjyxyd15brfnzlkdl";
    };
    propagatedBuildInputs = [ py.six ];
    doCheck = false;
  };

  py_consulate = py.buildPythonPackage rec {
    pname = "consulate";
    version = "1.1.0"; # unreleased version
    src = pkgs.fetchFromGitHub {
      owner = "gmr";
      repo = "consulate";
      rev = "c431de9e629614b49c61c334d5f491fea3a9c5a3";
      sha256 = "1jm8l3xl274xjamsf39zgn6zz00xq5wshhvqkncnyvhqw0597cqv";
    };
    propagatedBuildInputs = [
      py.requests
    ];
    meta = with pkgs.lib; {
      description = "Consulate is a Python client library and set of application for the Consul service discovery and configuration system.
";
      homepage = https://pypi.org/project/consulate/;
      license = licenses.publicDomain;
    };
  };

  fc-qemu = py.buildPythonApplication rec {
    name = "fc-qemu";
    version = "dev";
    namePrefix = "";
    src = ./.;
    dontCheck = true;
    dontStrip = true;

    propagatedBuildInputs = with pkgs; [
      py.requests
      py.future
      py.colorama
      py_structlog
      py_consulate
      py.psutil
      py.pyyaml
      py.setuptools
      qemu_kvm
      ceph
      gptfdisk
      parted
      xfsprogs
    ];
  };

in
  pkgs.mkShell {
    # nativeBuildInputs is usually what you want -- tools you need to run
    nativeBuildInputs = [ fc-qemu py.pytest py.pytest-xdist py.pytest-cov py.mock py.pytest-timeout ];
}
