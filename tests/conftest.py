import errno
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from subprocess import check_call, getoutput
from typing import List, Literal
from unittest.mock import Mock, patch

import mock
import pytest
import structlog
from structlog.typing import WrappedLogger

import fc.qemu.agent
import fc.qemu.hazmat.qemu
import fc.qemu.logging
from fc.qemu.agent import Agent
from fc.qemu.hazmat import libceph
from fc.qemu.hazmat.ceph import (
    Ceph,
    RootSpec,
    VMLockMgr,
    VolumeSpecification,
)
from fc.qemu.hazmat.iproute2 import TunTap
from fc.qemu.sysconfig import sysconfig
from fc.qemu.util import GiB, cmd

########################################################
# ceph fixtures


class RadosMock(object):
    tmp_path: Path
    # The feature set images are born with, mirroring the cluster's
    # `rbd_default_features`. fc-nixos pins this to 1 (layering only), so
    # images start out on the legacy rbd lock path and only move to the rados
    # lock once `Ceph._ensure_image_features` enables `exclusive-lock`.
    # Tests that need pre-existing flags override this.
    default_features: list[str] = ["layering"]

    def __init__(self, conffile, name, log):
        self.conffile = conffile
        self.name = name
        self._ioctx = {}
        # Advisory rados locks on sidecar objects, keyed by
        # (pool, namespace, object name) -> {lock name: (client, cookie, addr)}
        self._objects: dict[tuple[str, str, str], dict[str, tuple]] = {}

    def open_ioctx(self, pool):
        if pool not in self._ioctx:
            self._ioctx[pool] = IoctxMock(
                pool, self.tmp_path, self.default_features
            )
        return self._ioctx[pool]

    def list_pools(self):
        return ["rbd", "data", "rbd.ssd", "rbd.hdd", "rbd.rgw.foo", "fcio"]

    ####################################################
    # rados objects and their advisory locks

    def create_obj(self, pool, obj_name, namespace=""):
        key = (pool, namespace, obj_name)
        if key in self._objects:
            raise libceph.ImageExists(obj_name)
        self._objects[key] = {}

    def lock_exclusive(self, pool, obj_name, lock_name, cookie, namespace=""):
        locks = self._objects.setdefault((pool, namespace, obj_name), {})
        locker = locks.get(lock_name)
        if locker is not None and locker[1] != cookie:
            raise libceph.ImageBusy(errno.EBUSY, "Image is busy")
        # Taking our own lock again is fine, the real `rados lock get` fails
        # but `Rados.lock_exclusive` looks the lock up and accepts it as ours.
        locks[lock_name] = (
            "client." + self.name.replace("client.", ""),
            cookie,
            "127.0.0.1:0/1",
        )

    def break_lock(
        self,
        pool,
        obj_name,
        lock_name,
        cookie,
        locker_client_name,
        namespace="",
    ):
        # The real implementation swallows a missing object or lock, as that is
        # the state we wanted to reach anyway.
        locks = self._objects.get((pool, namespace, obj_name))
        if not locks:
            return
        locks.pop(lock_name, None)

    def lock_list_by_name(self, pool, obj_name, lock_name, namespace=""):
        locks = self._objects.get((pool, namespace, obj_name), {})
        locker = locks.get(lock_name)
        return {"lockers": [] if locker is None else [locker]}


class IoctxMock(object):
    """Mock access to a pool."""

    def __init__(
        self, name: str, tmp_path: Path, default_features: list[str] = None
    ):
        # the rados implementation takes the name as a str, but later returns
        # that attribute as bytes
        self.tmp_path = tmp_path
        self.name = name
        self.rbd_images = {}
        self._snapids = 0
        self.default_features = (
            ["layering"] if default_features is None else default_features
        )

    def _rbd_create(self, name, size):
        assert name not in self.rbd_images
        self.rbd_images[name] = image = dict(
            size=size, lock=None, features=list(self.default_features)
        )
        image["path"] = path = self.tmp_path / f"{self.name}-{name}.raw"
        if not path.exists():
            with path.open("wb") as f:
                f.seek(size - 1)
                f.write(b"\0")
                f.close()

    def _rbd_create_snap(self, name, snapname):
        fullname = name + "@" + snapname
        assert fullname not in self.rbd_images
        self._snapids += 1
        self.rbd_images[fullname] = snap = self.rbd_images[name].copy()
        snap["lock"] = None
        snap["snapid"] = self._snapids
        # `.copy()` is shallow, so give the snapshot its own feature list
        # instead of aliasing the image's.
        snap["features"] = list(snap["features"])
        base_path = self.rbd_images[name]["path"]
        snap["path"] = self.tmp_path / f"{self.name}-{name}-{snapname}.raw"
        with base_path.open("rb") as source:
            count = source.seek(0, os.SEEK_END)
            source.seek(0)
            with snap["path"].open("wb") as dest:
                os.copy_file_range(source.fileno(), dest.fileno(), count)

    def _rbd_remove(self, name):
        # XXX prohibit while snapshots exist and if locked/opened
        if name in self.rbd_images:
            del self.rbd_images[name]

    def _rbd_remove_snap(self, name, snapname):
        fullname = name + "@" + snapname
        if fullname in self.rbd_images:
            del self.rbd_images[fullname]

    def close(self):
        pass


class RBDMock(object):
    def list(self, ioctx):
        return list(ioctx.rbd_images.keys())

    def create(self, ioctx, name, size):
        ioctx._rbd_create(name, size)

    def remove(self, ioctx, name):
        ioctx._rbd_remove(name)


class ImageMock(object):
    def __init__(self, ioctx, name, snapname=None):
        self.ioctx = ioctx
        self.name = name
        self.snapname = snapname
        self.closed = False

        self._name = self.name
        if self.snapname:
            self._name += "@" + self.snapname

        if self._name not in ioctx.rbd_images:
            raise libceph.ImageNotFound(self.name)

    def size(self):
        assert not self.closed
        return self.ioctx.rbd_images[self.name]["size"]

    def resize(self, size):
        assert not self.closed
        self.ioctx.rbd_images[self.name]["size"] = size

    def lock_exclusive(self, cookie):
        assert not self.closed
        lock = self.ioctx.rbd_images[self.name]["lock"]
        if lock is None:
            self.ioctx.rbd_images[self.name]["lock"] = {
                "tag": None,
                "exclusive": True,
                "lockers": [("client.xyz", cookie, "127.0.0.1:9999")],
            }
            return
        else:
            assert lock["exclusive"]
            if not lock["lockers"][0][1] == cookie:
                raise libceph.ImageBusy(errno.EBUSY, "Image is busy")
            return
        raise RuntimeError("unsupported mock path")

    def list_lockers(self):
        assert not self.closed
        if self.name not in self.ioctx.rbd_images:
            raise libceph.ImageNotFound(self.name)
        lock = self.ioctx.rbd_images[self.name]["lock"]
        return [] if lock is None else lock

    def list_snaps(self):
        assert not self.closed
        result = []
        for image, data in list(self.ioctx.rbd_images.items()):
            if image.startswith(self.name + "@"):
                snap = {
                    "id": data["snapid"],
                    "size": data["size"],
                    "name": image.split("@")[1],
                }
                result.append(snap)
        return result

    def create_snap(self, snapname):
        self.ioctx._rbd_create_snap(self.name, snapname)

    def remove_snap(self, snapname):
        self.ioctx._rbd_remove_snap(self.name, snapname)

    def unlock(self, cookie):
        assert not self.closed
        lock = self.ioctx.rbd_images[self.name]["lock"]
        if lock:
            assert lock["lockers"][0][1] == cookie
            self.ioctx.rbd_images[self.name]["lock"] = None

    def break_lock(self, client, cookie):
        assert not self.closed
        lock = self.ioctx.rbd_images[self.name]["lock"]
        if lock:
            assert lock["lockers"][0][0] == client
            assert lock["lockers"][0][1] == cookie
        self.ioctx.rbd_images[self.name]["lock"] = None

    def close(self):
        self.closed = True

    def map(self):
        assert not self.closed
        image = self.ioctx.rbd_images[self._name]
        if not image.get("mapped_device"):
            raw = image["path"]
            snap_readonly = ["-r"] if "snapid" in image else []
            image["mapped_device"] = Path(
                subprocess.check_output(
                    ["losetup", "-f", "--show"] + snap_readonly + [raw]
                )
                .decode("ascii")
                .strip()
            )
        return image["mapped_device"]

    def unmap(self):
        assert not self.closed
        image = self.ioctx.rbd_images[self._name]
        if not (device := image.pop("mapped_device")):
            return
        subprocess.check_output(["losetup", "-d", device]).strip()

    @property
    def features(self) -> list[str]:
        # Mirrors `libceph.Image.features`, which is a cached_property
        # returning a list of feature *names*.
        assert not self.closed
        return self.ioctx.rbd_images[self._name]["features"]

    #: librbd's `update_features` also enables dependencies of the requested
    #: feature: fast-diff pulls in object-map, which pulls in exclusive-lock.
    #: however, fast-diff and object-map are interlocked in ceph so that
    #: enabling one implicitly enables the other.
    FEATURE_DEPENDENCIES: dict[str, set[str]] = {
        "fast-diff": set(("object-map", "exclusive-lock")),
        "object-map": set(("fast-diff", "exclusive-lock")),
    }

    def enable_feature(self, feature: str):
        assert not self.closed
        features = self.ioctx.rbd_images[self._name]["features"]
        if feature in features:
            # Reproduce ceph's non-idempotence here, otherwise a regression in
            # the caller's pre-check would pass the mocked suite and only blow
            # up against a real cluster.
            raise subprocess.CalledProcessError(
                returncode=22,
                cmd=f"rbd feature enable {self._name} {feature}",
                output=(
                    "rbd: failed to update image features: one or more "
                    "requested features are already enabled\n"
                    "(22) Invalid argument"
                ),
            )
        deps = [feature]
        implied = []
        while deps:
            feat = deps.pop(0)
            implied.append(feat)
            if nextdeps := self.FEATURE_DEPENDENCIES.get(feat):
                deps.extend(
                    f for f in nextdeps if f not in implied and f not in deps
                )

        features.extend(f for f in implied if f not in features)


def setup_loopback_device(path, size):
    with path.open("w") as f:
        f.seek(4 * GiB)
        f.truncate()
    return (
        subprocess.check_output(["losetup", "--show", "-f", path])
        .decode("ascii")
        .strip()
    )


def ceph_live_rebuild():
    check_call("fc-ceph deactivate fc-ceph-mon")
    check_call("fc-ceph deactivate fc-ceph-mgr")
    check_call("fc-ceph deactivate osd all --no-safety-check")

    check_call("vgremove vgjnl00")
    check_call("losetup -D")
    check_call("rm -rf /ceph")


def ceph_live_setup():
    # This is a convergent setup and DOES NOT clean up after itself
    # in nixos tests this will happen during every test anyway, due to the
    # temporary nature of the VMs.
    # For the batou-based development environments
    root = Path("/ceph")
    if root.exists():
        # XXX This could be improved to make things more convergent.
        return
    root.mkdir()

    journal_disk = root / "disk-journal"
    journal_loopback = setup_loopback_device(journal_disk, 4 * GiB)

    osd_disk = root / "disk-osd"
    osd_loopback = setup_loopback_device(osd_disk, 4 * GiB)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("CEPH_ARGS", None)

    def call(cmd):
        print(f"$ {cmd}")
        with open("/tmp/test.log", "a") as f:
            print(f"$ {cmd}", file=f)
        check_call(cmd, env=env, shell=True)

    call(f"fc-ceph osd prepare-journal {journal_loopback}")
    call("fc-ceph mon create --no-encrypt --size 500m --bootstrap-cluster")

    # Give the monitor a chance to come up, otherwise the next commands have a high chance
    # of getting stuck.
    counter = 0
    while (
        subprocess.run(
            ["ceph", "-s", "--connect-timeout", "1"], env=env
        ).returncode
        == 1
    ):
        if counter >= 10:
            raise RuntimeError()
        counter += 1
        print(
            subprocess.getoutput(["tail", "/var/log/ceph/ceph-mon.host1.log"])
        )

    call(
        "fc-ceph keys mon-update-single-client host1 ceph_osd,ceph_mon,kvm_host salt-for-host-dhkasjy9"
    )
    call(
        "fc-ceph keys mon-update-single-client host2 kvm_host salt-for-host-dhkasjy9"
    )
    call("fc-ceph mgr create --no-encrypt --size 500m")
    call(f"fc-ceph osd create-bluestore --no-encrypt {osd_loopback}")
    call("ceph osd crush move host1 root=default")
    call("ceph osd pool create rbd 32")
    # not really necessary anymore, as this is handled by the ceph.conf osdPoolDefaultSize, but let's keep it for clarity
    # ceph is only set up on host 1, not host 2
    call("ceph osd pool set rbd size 1 --yes-i-really-mean-it")
    call("ceph osd pool set rbd min_size 1")
    call("ceph osd pool create rbd.ssd 32")
    call("ceph osd pool set rbd.ssd size 1 --yes-i-really-mean-it")
    call("ceph osd pool set rbd.ssd min_size 1")
    call("ceph osd pool create rbd.hdd 32")
    call("ceph osd pool set rbd.hdd size 1 --yes-i-really-mean-it")
    call("ceph osd pool set rbd.hdd min_size 1")
    call("ceph osd pool create fcio 32")
    call("ceph osd pool set fcio min_size 1")
    call("ceph osd pool application enable fcio orchestration")
    call("ceph osd lspools")
    call("rbd pool init rbd")
    call("rbd pool init rbd.ssd")
    call("rbd pool init rbd.hdd")
    # PL-134255: pin the legacy feature set (1 = layering) so that images are
    # born without `exclusive-lock` and the rbd lock tests stay reachable once
    # the platform raises its own rbdDefaultFeatures to 61. Redundant while
    # fc-nixos still pins 1 itself. Drop with the rbd lock path.
    call("ceph config set client rbd_default_features 1")
    call("rbd create --size 500 rbd.hdd/fc-21.05-dev")
    # XXX: `--exclusive` or not should not matter here, assuming this setup
    # is run without concurrency
    call("rbd map rbd.hdd/fc-21.05-dev")
    call("sgdisk /dev/rbd0 -o -a 2048 -n 1:8192:0 -c 1:ROOT -t 1:8300")
    call("partprobe")
    call("udevadm settle")
    call("mkfs.xfs /dev/rbd0p1")
    call("rbd unmap /dev/rbd0")
    call("rbd snap create rbd.hdd/fc-21.05-dev@v1")
    call("rbd snap protect rbd.hdd/fc-21.05-dev@v1")
    call("rbd create -s 1M rbd/.maintenance")


# PL-134255: the whole lock_mode axis goes away with the rbd lock path, at
# which point every test runs what is now `lock_mode="rados"`.
TestEnvironmentImageFeatures = Literal["rbd", "rados"]


#: Which lock mechanism a test exercises. The test environment's image features
# are set up such that the locking code is supposed to detect and use the
# corresponding lock methods. Images are always born without
#: `exclusive-lock` (as on a real cluster), so the mechanism follows purely
#: from whether fc.qemu is configured to enable that feature at cold boot:
#:
#:  "rbd"   - flag off, images stay layering-only -> per-volume rbd locks
#:  "rados" - flag on, `Ceph._ensure_image_features` enables the feature
#:            during `start()` -> single per-VM rados lock
#:
#: This is the shipped default configuration vs. the state after the rollout.
#: Override per test with `@both_lock_modes` or an explicit `parametrize`.
@pytest.fixture
def lock_mode() -> TestEnvironmentImageFeatures:
    return "rados"


#: Run a test against both lock mechanisms. Remove with PL-134255.
both_lock_modes = pytest.mark.parametrize("lock_mode", ["rbd", "rados"])

#: Pin a test to the legacy per-volume rbd locks. Everything marked with this
#: gets deleted wholesale with PL-134255.
rbd_lock_mode = pytest.mark.parametrize("lock_mode", ["rbd"])


########################################################
# `update-lock-method` expectations
#
# `Ceph._update_lock_method` logs its decision from three places: on every
# `attach_volumes` (i.e. every `Agent.__enter__` that attaches), at the top of
# `Ceph.start()`, and once more after `_ensure_image_features()`. Only the last
# one carries a decision and reflects the lock path that follows. That
# is the one tests pin, while the other two are tolerated noise via the `*_ANY`
# wildcards.
#
# PL-134255: the whole set goes with the rbd lock path, at which point the
# method itself is gone.

#: Before any image exists. With no volume lacking `exclusive-lock`, this
#: settles on the rados lock regardless of the configured `lock_mode`.
UPDATE_LOCK_METHOD_NO_IMAGES = """\
update-lock-method exclusive_lock_imgs=[] lock_mode=LockMode.RADOS machine=simplevm rbd_lock_imgs=[] subsystem=ceph
"""

#: All four volumes present, in `Ceph.volumes` order.
UPDATE_LOCK_METHOD_RBD = """\
update-lock-method exclusive_lock_imgs=[] lock_mode=LockMode.RBD machine=simplevm rbd_lock_imgs=['simplevm.root', 'simplevm.swap', 'simplevm.tmp', 'simplevm.cidata'] subsystem=ceph
"""
UPDATE_LOCK_METHOD_RADOS = """\
update-lock-method exclusive_lock_imgs=['simplevm.root', 'simplevm.swap', 'simplevm.tmp', 'simplevm.cidata'] lock_mode=LockMode.RADOS machine=simplevm rbd_lock_imgs=[] subsystem=ceph
"""

#: Any of the above, for the calls that only report whichever images happen to
#: exist at the time: nothing on a cold boot, the previous boot's feature state
#: later on.
UPDATE_LOCK_METHOD_ANY = """\
update-lock-method ...
"""
#: The same, as rendered by `fc-qemu -v` for the subprocess tests.
CLI_UPDATE_LOCK_METHOD_ANY = """\
simplevm         ceph update-lock-method...
"""


def update_lock_method(lock_mode: TestEnvironmentImageFeatures) -> str:
    """The decision `Ceph.start()` settles on for this lock mechanism."""
    return (
        UPDATE_LOCK_METHOD_RBD
        if lock_mode == "rbd"
        else UPDATE_LOCK_METHOD_RADOS
    )


@pytest.fixture
def ceph_mock(request, monkeypatch, tmp_path, lock_mode):
    # PL-134255: `Ceph.__init__` copies `sysconfig.ceph` into its instance
    # dict, so this one lever reaches both the directly constructed `Ceph` in
    # the hazmat tests and the one `Agent` builds internally.
    monkeypatch.setitem(
        sysconfig.ceph, "ensure_exclusive_lock", lock_mode != "rbd"
    )

    is_live = request.node.get_closest_marker("live")
    if is_live is not None:
        # This is a live test, running against the real Ceph cluster that
        # `pytest_collection_finish` has bootstrapped. Do not mock anything.
        # Other fixtures depend on us to order themselves after this decision.
        yield
        return

    def ensure_presence(self):
        VolumeSpecification.ensure_presence(self)

    monkeypatch.setattr(libceph, "Rados", RadosMock)
    monkeypatch.setattr(libceph, "RBD", RBDMock)
    monkeypatch.setattr(libceph, "Image", ImageMock)
    monkeypatch.setattr(RootSpec, "ensure_presence", ensure_presence)
    RadosMock.tmp_path = tmp_path
    monkeypatch.setattr(
        RadosMock, "default_features", list(RadosMock.default_features)
    )
    yield


@pytest.fixture
def ceph_inst(request, ceph_mock):
    cfg = {
        "resource_group": "test",
        "rbd_pool": "rbd.hdd",
        "name": "simplevm",
        "disk": 10,
        "tmp_size": 1024 * 1024,
        "swap_size": 1024 * 1024,
        "root_size": 1024 * 1024,
        "cidata_size": 1024 * 1024,
        "binary_generation": 2,
    }
    enc = {"parameters": {"environment_class_type": "nixos"}}
    ceph = Ceph(cfg, enc)
    is_live = request.node.get_closest_marker("live")
    if is_live is not None:
        ceph.CREATE_VM = "rbd create rbd.hdd/{name} --size 10G"
    else:
        ceph.CREATE_VM = "echo {name}"
    ceph.MKFS_XFS = "-q -f -K"
    ceph.__enter__()
    try:
        yield ceph
    finally:
        ceph.__exit__(None, None, None)


@pytest.fixture
def ceph_inst_cloudinit_enc(ceph_inst):
    ceph_inst.cfg["tmp_size"] = 500 * 1024 * 1024
    ceph_inst.cfg["cidata_size"] = 10 * 1024 * 1024
    ceph_inst.enc = {
        "name": "simplevm",
        "parameters": {
            "environment_class": "Ubuntu",
            "environment_class_type": "cloudinit",
            "resource_group": "test",
            "disk": 10,
            "interfaces": {
                "pub": {
                    "bridged": False,
                    "gateways": {
                        "203.0.113.0/24": "293.0.113.1",
                        "2001:db8:300:2::/64": "2001:db8:300:2::1",
                        "2001:db8:500:2::/64": "2001:db8:500:2::1",
                    },
                    "mac": "02:00:00:02:1d:e4",
                    "networks": {
                        "203.0.113.0/24": ["203.0.113.10"],
                        "2001:db8:300:2::/64": [],
                        "2001:db8:500:2::/64": ["2001:db8:500:2::5"],
                    },
                    "nics": [
                        {"external_label": "fe", "mac": "02:00:00:02:1d:e4"}
                    ],
                    "policy": "puppet",
                    "routed": False,
                },
            },
        },
    }
    yield ceph_inst
    Path("/etc/qemu/users/test.json").unlink(missing_ok=True)


def print2(*args, **kw):
    print(*args, file=sys.stderr, **kw)
    with open("/tmp/test.log", "a") as f:
        print(*args, file=f, **kw)


@pytest.fixture(autouse=True)
def clean_rbd_pools(request, kill_vms, ceph_mock):
    is_live = request.node.get_closest_marker("live")
    if not is_live:
        return

    def images_to_clean() -> List[str]:
        images = []
        for pool in ["rbd.hdd", "rbd.ssd"]:
            print2(f"rbd ls {pool}")
            with subprocess.Popen(
                ["rbd", "ls", pool],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as proc:
                try:
                    # Add timeout for safety
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    raise
            for line in stdout.splitlines():
                image = line.strip().decode("ascii")
                if not image or "-" in image:  # empty line or base image
                    continue
                images.append(f"{pool}/{image}")

        return images

    passes = 0
    while images := images_to_clean():
        if passes > 1:
            # Use temporary blocklisting to ensure all watchers are gone.
            ips = [socket.gethostbyname(host) for host in ["host1", "host2"]]
            for ip in ips:
                print2(f"ceph osd blocklist add {ip}")
                subprocess.run(
                    f"ceph osd blocklist add {ip}",
                    shell=True,
                )
            # We might be waiting for images stuck with watchers that need
            # to time out ...
            time.sleep(5)
            for ip in ips:
                print2(f"blocklist rm {ip}")
                subprocess.run(
                    f"ceph osd blocklist rm {ip}",
                    shell=True,
                )
            time.sleep(5)

        psauxf = subprocess.run(
            ["ps", "auxf"],
            capture_output=True,
            text=True,
            shell=False,
        )
        print2(psauxf.stdout)

        for image in images:
            # Clean up any remaining locks before deleting the image.
            print2(f"rbd --format json lock ls {image}")
            lock_ls_proc = subprocess.run(
                ["rbd", "--format", "json", "lock", "ls", image],
                capture_output=True,
                text=True,
                shell=False,
            )
            if lock_ls_proc.stdout.strip():
                try:
                    locks = json.loads(lock_ls_proc.stdout)
                    for lock_details in locks:
                        locker = lock_details["locker"]
                        lock_id = lock_details["id"]
                        print2(f"rbd lock rm {image} {lock_id} {locker}")
                        subprocess.run(
                            ["rbd", "lock", "rm", image, lock_id, locker],
                            shell=False,
                            check=True,
                        )
                except json.JSONDecodeError:
                    print2(
                        f"Warning: could not parse JSON for rbd lock ls {image}: {lock_ls_proc.stdout}"
                    )
                except subprocess.CalledProcessError as e:
                    print2(f"Warning: rbd lock rm failed for {image}: {e}")

            print2(f"rbd snap purge {image}")
            subprocess.run(
                f"rbd snap purge {image}",
                shell=True,
            )
            print2(f"rbd migration abort {image}")
            subprocess.run(
                f"rbd migration abort {image}",
                shell=True,
            )
            print2(f"rbd rm {image}")
            subprocess.run(f"rbd rm {image}", shell=True)
        passes += 1

    # Unmap all mapped devices
    for rbd_dev in Path("/dev/").glob("rbd?"):
        subprocess.run(["rbd", "unmap", str(rbd_dev)])
    subprocess.check_call(
        ["rbd-locktool", "-vvvv", "-u", "-f", "rbd/.maintenance"],
    )

    # Drop the per-VM rados lock sidecar objects. A leftover object is
    # harmless - `VMLockMgr.ensure` is idempotent - but a lock still held on
    # one would block the next test. Removing the object drops the lock.
    pool = sysconfig.ceph["orchestration_pool"]
    namespace = VMLockMgr.NAMESPACE
    print2(f"rados -p {pool} -N {namespace} ls")
    sidecars = subprocess.run(
        ["rados", "-p", pool, "-N", namespace, "ls"],
        capture_output=True,
        text=True,
    )
    for line in sidecars.stdout.splitlines():
        if not (obj := line.strip()):
            continue
        print2(f"rados -p {pool} -N {namespace} rm {obj}")
        subprocess.run(
            ["rados", "-p", pool, "-N", namespace, "rm", obj],
        )


########################################################
# qemu/kvm related fixtures


def named_vm(name, request, clean_environment, monkeypatch, tmpdir):
    import fc.qemu.hazmat.qemu

    monkeypatch.setattr(fc.qemu.hazmat.qemu.Qemu, "guestagent_timeout", 0.1)

    cfg = Path(__file__).parent / "fixtures" / f"{name}.yaml"
    shutil.copy(
        cfg, fc.qemu.hazmat.qemu.Qemu.prefix / f"etc/qemu/vm/{name}.cfg"
    )
    Path(
        fc.qemu.hazmat.qemu.Qemu.prefix / f"etc/qemu/vm/.{name}.cfg.staging"
    ).unlink(missing_ok=True)

    vm = Agent(name)
    vm.timeout_graceful = 1
    vm.__enter__()
    vm.qemu.qmp_timeout = 0.1
    vm.qemu.vm_expected_overhead = 128

    def cleanup():
        if p := vm.qemu.proc():
            parent = p.parent()
            if parent and "supervised-qemu-wrapped" in parent.cmdline()[1]:
                parent.terminate()
            p.terminate()

        for open_volume in vm.ceph.opened_volumes:
            for snapshot in open_volume.snapshots:
                snapshot.remove()

        for service in vm.consul.agent.services():
            try:
                vm.consul.agent.service.deregister(vm.svc_name)
            except Exception:
                vm.log.exception("consul-deregister-failed", exc_info=True)

    cleanup()
    vm.force_unlock()
    get_log()

    yield vm

    exc_info = sys.exc_info()

    cleanup()

    vm.__exit__(*exc_info)
    if len(exc_info):
        print(traceback.print_tb(exc_info[2]))


@pytest.fixture
def vm(request, clean_environment, monkeypatch, tmpdir):
    yield from named_vm(
        "simplevm", request, clean_environment, monkeypatch, tmpdir
    )


@pytest.fixture
def vm_with_pub(request, clean_environment, monkeypatch, tmpdir):
    yield from named_vm(
        "simplepubvm", request, clean_environment, monkeypatch, tmpdir
    )


@pytest.fixture(autouse=True)
def kill_vms(request, ceph_mock):
    is_live = request.node.get_closest_marker("live")
    if not is_live:
        yield
        return

    assert_call(["pkill", "-A", "-f", "supervised-qemu"], exit_codes=[0, 1])
    assert_call(["pkill", "-A", "-f", "qemu-system-x86_64"], exit_codes=[0, 1])
    yield
    assert_call(["pkill", "-A", "-f", "supervised-qemu"], exit_codes=[0, 1])
    assert_call(["pkill", "-A", "-f", "qemu-system-x86_64"], exit_codes=[0, 1])


@pytest.fixture
def kill_vms_host2(call_host2):
    call_host2(["pkill", "-A", "-f", "supervised-qemu"], exit_codes=[0, 1])
    call_host2(["pkill", "-A", "-f", "qemu-system-x86_64"], exit_codes=[0, 1])
    yield
    call_host2(["pkill", "-A", "-f", "supervised-qemu"], exit_codes=[0, 1])
    call_host2(["pkill", "-A", "-f", "qemu-system-x86_64"], exit_codes=[0, 1])


########################################################
# generic fixtures/cleanups


@pytest.fixture(autouse=True)
def clean_tmpdir_with_flakefinder(tmpdir, pytestconfig):
    """The `tmpdir` is normally not cleaned out for debugging purposes.

    Running with flakefinder causes the tmpdir to quickly grow too big.
    So, if flakefinder is active, we clean it out to avoid running out of
    disk space.
    """
    yield
    if pytestconfig.getoption("flake_finder_enable") > 0:
        shutil.rmtree(tmpdir)


@pytest.fixture
def clean_environment(request):
    logpath = Path("/var/log/vm")
    if list(logpath.glob("*")):
        subprocess.run("rm /var/log/vm/*", shell=True)
    yield
    print(getoutput("free"))
    print(getoutput("ps auxf"))
    print(getoutput("df -h"))
    print(getoutput("journalctl --since -30s"))
    if list(logpath.glob("*")):
        print(getoutput("tail -n 50 /var/log/vm/*"))
        subprocess.run("rm /var/log/vm/*", shell=True)

    is_live = request.node.get_closest_marker("live")
    if is_live:
        print(getoutput("ceph df"))
        print(getoutput("rbd showmapped"))


@pytest.fixture(autouse=True)
def synthetic_root(request, monkeypatch, tmp_path):
    is_live = request.node.get_closest_marker("live")
    if is_live is not None:
        # This is a live test. Do not mock things.
        return

    (tmp_path / "run").mkdir()
    (tmp_path / "etc/qemu/vm").mkdir(parents=True)
    monkeypatch.setattr(fc.qemu.hazmat.qemu.Qemu, "prefix", tmp_path)
    monkeypatch.setattr(Agent, "prefix", tmp_path)
    monkeypatch.setattr(fc.qemu.agent, "EXECUTABLE", "true")


def assert_call(*args, exit_codes=[0], **kw):
    return_code = subprocess.call(*args, **kw)
    assert return_code in exit_codes


@pytest.fixture
def call_host2():
    def call(*args, exit_codes=[0], **kw):
        args = list(args)
        args[0] = (
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-i",
            "/etc/ssh_key",
            "host2",
        ) + tuple(args[0])
        assert_call(*args, exit_codes=exit_codes, **kw)

    return call


########################################################
# logging


@pytest.fixture(scope="session")
def setup_structlog():
    from fc.qemu import util

    # set to True to temporarily get detailed tracebacks
    log_exceptions = True

    def test_logger(
        logger: WrappedLogger,
        method_name: str,
        event: fc.qemu.logging.EventDict,
    ):
        stack = event.pop("stack", None)
        exc = event.pop("exception", None)
        event_name = event.pop("event", "")
        event_prefix = os.path.basename(event_name) if event_name else " "

        result: str
        # In reality we log via `output` but in the tests we log via `output_line` to help
        # with diagnosis on partially completed/hanging calls.
        event.pop("output", None)
        if "output_line" in event:
            result = fc.qemu.logging.prefix(
                event_prefix, event.pop("output_line").rstrip()
            )
        else:
            result_: list[str] = []
            if event_name:
                result_.append(event_name)
            for key in sorted(event):
                result_.append("{}={}".format(key, str(event[key]).strip()))
            result = " ".join(result_)

        # Ensure we get something to read on stdout in case we have errors.
        reltime = time.time() - util.test_log_start
        with open("/tmp/test.log", "a") as f:
            print(f"{reltime:08.4f} {result}")
            print(f"{reltime:08.4f} {result}", file=f)
            if stack:
                print(stack)
                print(stack, file=f)
            if exc:
                print(exc)
                print(exc, file=f)

        # Allow tests to inspect only methods and events they are interested
        # in. This reduces noise in our test outputs and comparisons and
        # reduces fragility.
        hide_subsystems = util.test_log_options["hide_subsystems"]
        if hide_subsystems and event.get("subsystem") in hide_subsystems:
            raise structlog.DropEvent

        show_methods = util.test_log_options["show_methods"]
        if show_methods and method_name not in show_methods:
            raise structlog.DropEvent

        show_events = util.test_log_options["show_events"]
        if show_events:
            for show in show_events:
                if show in event_name:
                    break
            else:
                raise structlog.DropEvent

        util.log_data.append(result)
        if log_exceptions:
            if stack:
                util.log_data.extend(stack.splitlines())
            if exc:
                util.log_data.extend(exc.splitlines())
        raise structlog.DropEvent

    def test_log_print(*args):
        """A helper for tests to insert output into the stdout log.

        This adds the same timestamps as the default output for the
        log and avoids this to become part of the output content that
        we run assertions on.
        """
        reltime = time.time() - util.test_log_start
        print(f"{reltime:08.4f}", *args)
        with open("/tmp/test.log", "a") as f:
            print(f"{reltime:08.4f}", *args, file=f)

    util.test_log_print = test_log_print

    structlog.configure(
        processors=(
            ([structlog.processors.format_exc_info] if log_exceptions else [])
            + [test_logger]
        )
    )


@pytest.fixture(autouse=True)
def reset_structlog(setup_structlog):
    from fc.qemu import util

    util.log_data = []
    util.test_log_start = time.time()
    util.test_log_options = {
        "show_methods": [],
        "show_events": [],
        "hide_subsystems": ["libceph"],
    }


@pytest.fixture()
@patch("fc.qemu.directory.connect", autospec=True)
def directory_mock(directory_connect_mock):
    directory_mock = mock.Mock()
    directory_connect_mock.side_effect = lambda _: directory_mock
    yield directory_mock


def get_log():
    from fc.qemu import util

    result = "\n".join(util.log_data)
    util.log_data = []
    return result


########################################################
# subprocess call tracking

CALLED_BINARIES = set([])


@pytest.fixture(autouse=True)
def record_subprocess_calls(monkeypatch):
    original = subprocess.Popen.__init__

    def Popen_recording_init(self, *args, **kw):
        binary = args[0][0]
        if kw.get("shell"):
            binary = args[0].split()[0]
        if not binary.startswith("/"):
            CALLED_BINARIES.add(binary)
        return original(self, *args, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", Popen_recording_init)


@pytest.fixture(autouse=True)
def record_subprocess_run(monkeypatch):
    subprocess_run_orig = subprocess.run

    def recording_subprocess_run(*args, **kw):
        binary = args[0][0]
        if kw.get("shell"):
            binary = args[0].split()[0]
        if not binary.startswith("/"):
            CALLED_BINARIES.add(binary)
        return subprocess_run_orig(*args, **kw)

    monkeypatch.setattr(subprocess, "run", recording_subprocess_run)


########################################################
# pytest integration


def pytest_collectstart(collector):
    from fc.qemu.sysconfig import sysconfig

    sysconfig.load_system_config()


def pytest_collection_finish(session):
    """Bootstrap the live Ceph cluster before any test starts running.

    This has to happen outside the per-test protocol: pytest-timeout installs
    its timer in `pytest_runtest_protocol`, which covers setup, call and
    teardown as a single budget. Bootstrapping a session-scoped fixture is no
    alternative, as these run during the first requesting test's setup phase and
    therefore charge the whole cluster setup (a minute or more) to whichever
    test happens to run first. `pytest_collection_finish` runs after collection
    is complete and before the first test, so no timer is active yet.

    We use this hook rather than `pytest_collection_modifyitems` because `-k`
    and `-m` deselection is itself implemented as a `modifyitems` hook, so the
    item list is only final here.
    """
    if session.config.getoption("collectonly"):
        return
    if not any(item.get_closest_marker("live") for item in session.items):
        # Nothing needs a real cluster, so don't build one. This keeps unit
        # test runs working on machines without the Ceph roles installed.
        return
    try:
        ceph_live_setup()
    except subprocess.CalledProcessError as e:
        output = e.output or e.stderr or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise pytest.UsageError(
            f"Ceph fixture bootstrap failed: {e.cmd} returned {e.returncode}.\n"
            f"{output}\n"
            "/ceph now exists but the cluster is incomplete. Tear it down "
            "via `cleanup-ceph` before retrying - otherwise the next run takes "
            "the already-bootstrapped shortcut and fails in confusing ways."
        )


def pytest_collection_modifyitems(items):
    """Modifies test items in place to ensure test modules run in a given order."""
    # A few tests need to be run very very late as they track the behaviour of
    # the other tests.
    sorted_items = items.copy()
    random.shuffle(sorted_items)

    def sort_key(item):
        if list(item.iter_markers("last")):
            return 1
        if list(item.iter_markers("first")):
            return -1
        return 0

    sorted_items.sort(key=sort_key)
    items[:] = sorted_items


def pytest_assertrepr_compare(op, left, right):
    if left.__class__.__name__ == "Ellipsis":
        return left.compare(right).diff
    elif right.__class__.__name__ == "Ellipsis":
        return right.compare(left).diff


@pytest.fixture(autouse=True)
def cleanup_tap_devices():
    for tuntap in TunTap.list(Mock()):
        cmd(f"ip l delete dev {tuntap.ifname}", Mock())
    yield
    try:
        for tuntap in TunTap.list(Mock()):
            cmd(f"ip l delete dev {tuntap.ifname}", Mock())
    except Exception:
        # Optimistic here: some tests patch out `cmd` and this may
        # not have been cleaned up yet (e.g. test_rbd_unexpected_exception_does_not_cause_image_not_found)
        pass
