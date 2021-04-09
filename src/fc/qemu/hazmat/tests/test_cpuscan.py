from ..cpuscan import AMDHost, IntelHost, scan_cpus
from ..qemu import Qemu


def test_cpuscan_intel(monkeypatch):
    monkeypatch.setattr(Qemu, 'executable', '/bin/true')
    host = AMDHost()
    results = scan_cpus(host)
    for x in results:
        assert ',ssbd' not in x.cpu_arg


def test_cpuscan_amd(monkeypatch):
    monkeypatch.setattr(Qemu, 'executable', '/bin/true')
    host = IntelHost()
    results = scan_cpus(host)
    for x in results:
        assert ',amd-ssbd' not in x.cpu_arg


def test_cpuscan_detects_something(monkeypatch):
    monkeypatch.setattr(Qemu, 'executable', '/bin/true')
    assert scan_cpus() != []
