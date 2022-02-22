let
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
    version = "0.6.0";
    src = py.fetchPypi {
      inherit pname version;
      sha256 = "0myp20l7ckpf8qszhkfvyzvnlai8pbrhwx30bdr8psk23vkkka3q";
    };
    propagatedBuildInputs = [
      py.requests
    ];
    meta = with pkgs.lib; {
      description = "Consulate is a Python client library and set of application for the Consul service discovery and configuration system.
";
      homepage = http://packages.python.org/murmurhash3;
      license = licenses.publicDomain;
    };
  };
in
with pkgs;
py.buildPythonApplication rec {
  name = "fc-qemu";
  version = "dev";
  namePrefix = "";
  src = ./.;
  dontCheck = true;
  dontStrip = true;
  propagatedBuildInputs = [
      py.requests
      py.future
      py.colorama
      py_structlog
      py_consulate
      py.psutil
      py.pyyaml
      py.setuptools
      ceph
  ];
}
