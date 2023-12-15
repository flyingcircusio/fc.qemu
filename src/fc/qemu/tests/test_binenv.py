import shutil

import pytest

import fc.qemu.conftest

KNOWN_BINARIES = set(
    [
        "mkfs.xfs",
        "mount",
        "partprobe",
        "pgrep",
        "qemu-system-x86_64",
        "rbd",
        "rbd-locktool",
        "sgdisk",
        "systemctl",
        "umount",
    ]
)


@pytest.mark.unit
def test_known_binaries_reachable():
    for binary in KNOWN_BINARIES:
        assert shutil.which(binary)


@pytest.mark.live
def test_no_unexpected_binaries():
    assert fc.qemu.conftest.CALLED_BINARIES == KNOWN_BINARIES


@pytest.mark.unit
def test_ensure_critical_module_imports():
    import rados  # noqa
