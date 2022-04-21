import pytest

from ..cpuscan import AbstractHost, AMDHost, IntelHost, scan_cpus


@pytest.mark.timeout(60)
@pytest.mark.slow
def test_cpuscan_intel():
    host = AMDHost()
    host.CPU_MODELS = ["qemu64-v1"]
    results = scan_cpus(host)
    for x in results:
        assert ",ssbd" not in x.cpu_arg
    assert "qemu64-v1" in [x.cpu_arg for x in results]


@pytest.mark.timeout(60)
@pytest.mark.slow
def test_cpuscan_amd():
    host = IntelHost()
    host.CPU_MODELS = ["qemu64-v1"]
    results = scan_cpus(host)
    for x in results:
        assert ",amd-ssbd" not in x.cpu_arg
    assert "qemu64-v1" in [x.cpu_arg for x in results]


@pytest.mark.timeout(60)
@pytest.mark.slow
def test_cpuscan_detects_something():
    host = AbstractHost()
    results = scan_cpus(host)
    assert [x.cpu_arg for x in results] == ["qemu64-v1"]
