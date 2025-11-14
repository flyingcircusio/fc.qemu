import shutil

import pytest

import tests.conftest

REQUIRED_BINARIES = set(
    [  # production
        "blkid",
        "ip",
        "mkfs.vfat",
        "mkfs.xfs",
        "mkswap",
        "mount",
        "parted",
        "partprobe",
        "pgrep",
        "qemu-system-x86_64",
        "rbd",
        "rbd-locktool",
        "sgdisk",
        "systemctl",
        "udevadm",
        "umount",
        "xfs_admin",
        "xfs_db",
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
        "losetup",
    ]
)


@pytest.mark.unit
def test_known_binaries_reachable():
    missing = set()
    for binary in REQUIRED_BINARIES:
        if not shutil.which(binary):
            missing.add(binary)
    assert missing == set()


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
    import structlog  # noqa
