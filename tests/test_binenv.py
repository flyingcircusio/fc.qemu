import shutil

import pytest

import tests.conftest

REQUIRED_BINARIES = set(
    [  # production
        "blkid",
        "mkfs.xfs",
        "mkswap",
        "mount",
        "partprobe",
        "parted",
        "pgrep",
        "qemu-system-x86_64",
        "rbd",
        "rbd-locktool",
        "sgdisk",
        "systemctl",
        "umount",
        "xfs_admin",
        "xfs_db",
        "parted",
    ]
)

TEST_BINARIES = set(
    [  # Test fixtures
        "ceph",
        "df",
        "fc-qemu",
        "file",
        "free",
        "journalctl",
        "pkill",
        "ps",
        "rm",
        "scp",
        "sed",
        "ssh",
        "supervised-qemu",
        "tail",
        "top",
        "true",
    ]
)


@pytest.mark.unit
def test_known_binaries_reachable():
    for binary in REQUIRED_BINARIES:
        assert shutil.which(binary)


@pytest.mark.live
@pytest.mark.last
def test_no_unexpected_binaries():
    # This needs to be last to ensure we tracked all subprocess calls.
    unexpected_binaries = (
        tests.conftest.CALLED_BINARIES - REQUIRED_BINARIES - TEST_BINARIES
    )
    assert not unexpected_binaries


@pytest.mark.unit
def test_ensure_critical_module_imports():
    import rados  # noqa
    import structlog  # noqa
