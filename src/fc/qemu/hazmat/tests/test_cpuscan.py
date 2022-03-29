import pytest

from ..cpuscan import AMDHost, IntelHost, scan_cpus


@pytest.mark.timeout(60)
@pytest.mark.slow
def test_cpuscan_intel():
    host = AMDHost()
    results = scan_cpus(host)
    for x in results:
        assert ",ssbd" not in x.cpu_arg


@pytest.mark.timeout(60)
@pytest.mark.slow
def test_cpuscan_amd():
    host = IntelHost()
    results = scan_cpus(host)
    for x in results:
        assert ",amd-ssbd" not in x.cpu_arg


@pytest.mark.timeout(60)
@pytest.mark.slow
def test_cpuscan_detects_something():
    assert scan_cpus() != []
