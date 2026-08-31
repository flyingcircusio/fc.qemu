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
        "notfound",
        "echo",
    ]
)


# `notfound` has to stay unresolvable: test_util.test_json_cmd_call_cmd_error
# uses it to provoke a CalledProcessError.
UNAVAILABLE_BINARIES = set(["notfound"])


def missing_binaries(binaries):
    return set(binary for binary in binaries if not shutil.which(binary))


@pytest.mark.unit
def test_known_binaries_reachable():
    assert missing_binaries(REQUIRED_BINARIES) == set()


@pytest.mark.live
def test_known_binaries_reachable_live():
    # The live tests rely on additional tools that fc.qemu itself never calls,
    # and are tracked as the package's nativeCheckInputs.
    # Those only have to be reachable where live tests actually run - requiring
    # them in the package's checkPhase, which can only run the unit tests, would
    # mean carrying them in the closure for nothing.
    assert (
        missing_binaries(
            (REQUIRED_BINARIES | TEST_BINARIES) - UNAVAILABLE_BINARIES
        )
        == set()
    )


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
