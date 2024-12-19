import shutil

import pytest

import tests.conftest

KNOWN_BINARIES = set(
    [
        "blkid",
        "mkfs.xfs",
        "mkswap",
        "mount",
        "partprobe",
        "pgrep",
        "qemu-system-x86_64",
        "rbd",
        "rbd-locktool",
        "sgdisk",
        "systemctl",
        "umount",
        "xfs_admin",
    ]
)


@pytest.mark.unit
def test_known_binaries_reachable():
    for binary in KNOWN_BINARIES:
        assert shutil.which(binary)


@pytest.mark.live
def test_no_unexpected_binaries():
    # This needs to be last tu ensure we tracked all subprocess calls.
    assert tests.conftest.CALLED_BINARIES == KNOWN_BINARIES


@pytest.mark.unit
def test_ensure_critical_module_imports():
    import rados  # noqa
    import structlog  # noqa
