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
from unittest import mock
from unittest.mock import Mock, patch

import psutil
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
    VMLockManager,
    VolumeSpecification,
)
from fc.qemu.hazmat.iproute2 import TunTap
from fc.qemu.sysconfig import sysconfig
from fc.qemu.timeout import TimeOut
from fc.qemu.util import GiB, cmd

########################################################
# ceph fixtures


class RadosMock:
    tmp_path: Path
    # The feature set images are born with, mirroring the cluster's
    # `rbd_default_features`. fc-nixos pins this to 1 (layering only), so every
    # image starts out layering-only and the expected features are enabled by
    # `VolumeSpecification.ensure_features` - at cold boot via `Ceph.start()`,
    # or online via `Ceph.ensure()`. That makes the upgrade path the default
    # the whole suite exercises. Tests that need a different starting point
    # override this.
    default_features: list[str] = ["layering"]

    def __init__(self, conffile, name, log):
        self.conffile = conffile
        self.name = name
        self._ioctx = {}
        # Advisory rados locks on sidecar objects, keyed by
        # (pool, namespace, object name) -> {lock name: RadosLocker}
        self._objects: dict[
            tuple[str, str, str], dict[str, libceph.RadosLocker]
        ] = {}
        self._lock_nonce = 0

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
        if locker is not None and locker.cookie != cookie:
            raise libceph.ImageBusy(errno.EBUSY, "Image is busy")
        # Taking our own lock again is fine, the real `rados lock get` fails
        # but `Rados.lock_exclusive` looks the lock up and accepts it as ours.
        # The address carries a per-connection nonce on a real cluster, which
        # `Ceph.auth_cookie` depends on to tell lock holders apart.
        self._lock_nonce += 1
        locks[lock_name] = libceph.RadosLocker(
            name="client." + self.name.replace("client.", ""),
            cookie=cookie,
            addr=f"127.0.0.1:0/{self._lock_nonce}",
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

    def lock_info(self, pool, obj_name, lock_name, namespace=""):
        locks = self._objects.get((pool, namespace, obj_name), {})
        locker = locks.get(lock_name)
        return libceph.RadosLockInfo(
            name=lock_name,
            type="exclusive",
            tag="",
            lockers=[] if locker is None else [locker],
        )


class IoctxMock:
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
            size=size,
            lock=None,
            features=list(self.default_features),
            flags=[],
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
        # `.copy()` is shallow, so give the snapshot its own feature and flag
        # lists instead of aliasing the image's.
        snap["features"] = list(snap["features"])
        snap["flags"] = list(snap["flags"])
        base_path = self.rbd_images[name]["path"]
        snap["path"] = self.tmp_path / f"{self.name}-{name}-{snapname}.raw"
        shutil.copyfile(base_path, snap["path"])

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


class RBDMock:
    def list(self, ioctx):
        return list(ioctx.rbd_images.keys())

    def create(self, ioctx, name, size):
        ioctx._rbd_create(name, size)

    def remove(self, ioctx, name):
        ioctx._rbd_remove(name)


class ImageMock:
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

    @property
    def _image(self) -> dict:
        return self.ioctx.rbd_images[self._name]

    def resize(self, size):
        assert not self.closed
        self.ioctx.rbd_images[self.name]["size"] = size

    def lock_exclusive(self, cookie):
        assert not self.closed
        lock = self.ioctx.rbd_images[self.name]["lock"]
        if lock is None:
            self.ioctx.rbd_images[self.name]["lock"] = libceph.RbdLockInfo(
                id=cookie, locker="client.xyz", address="127.0.0.1:9999"
            )
            return
        if lock.id != cookie:
            raise libceph.ImageBusy(errno.EBUSY, "Image is busy")

    def list_lockers(self) -> list:
        assert not self.closed
        if self.name not in self.ioctx.rbd_images:
            raise libceph.ImageNotFound(self.name)
        lock = self.ioctx.rbd_images[self.name]["lock"]
        return [] if lock is None else [lock]

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
        # `libceph.Image.unlock` looks the cookie up in `rbd lock list` first
        # and refuses if it isn't there, rather than asking rbd to remove a
        # lock that doesn't exist.
        if lock is None or lock.id != cookie:
            raise libceph.ImageBusy(errno.EBUSY, "Lock cookie not found")
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
    def _image(self) -> dict:
        return self.ioctx.rbd_images[self._name]

    @property
    def info(self) -> libceph.RbdInfo:
        # Mirrors `libceph.Image.info`, except that the real one is a
        # `cached_property` invalidated by `ensure_feature`/`resize`. Reading
        # live state here keeps the mock simple; the caching behaviour itself
        # is covered against the real class in
        # `test_ensure_feature_invalidates_the_info_cache`.
        assert not self.closed
        image = self._image
        return libceph.RbdInfo(
            size=image["size"],
            features=set(image["features"]),
            flags=set(image["flags"]),
        )

    def refresh(self):
        # A no-op here: `info` reads live state rather than caching it. The
        # real one drops a `cached_property`.
        pass

    #: librbd's `update_features` also enables dependencies of the requested
    #: feature: fast-diff pulls in object-map, which pulls in exclusive-lock.
    #: however, fast-diff and object-map are interlocked in ceph so that
    #: enabling one implicitly enables the other.
    FEATURE_DEPENDENCIES: dict[str, set[str]] = {
        "fast-diff": set(("object-map", "exclusive-lock")),
        "object-map": set(("fast-diff", "exclusive-lock")),
    }

    def _enable_feature(self, feature: str):
        """The raw `rbd feature enable`, warts included.

        `libceph.Image.ensure_feature` is the idempotent wrapper around this.
        """
        assert not self.closed
        features = self._image["features"]
        if feature in features:
            # Reproduce ceph's non-idempotence here, otherwise a regression in
            # `ensure_feature`'s pre-check would pass the mocked suite and
            # only blow up against a real cluster.
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
        flags = self._image["flags"]
        if "object-map" in implied and "object map invalid" not in flags:
            # Enabling object-map on an image that already holds data leaves
            # the map unbuilt, which ceph reports as a flag until someone runs
            # `rbd object-map rebuild`.
            flags.append("object map invalid")

    def ensure_feature(self, feature: str):
        assert not self.closed
        if feature in self._image["features"]:
            return
        self._enable_feature(feature)

    def ensure_object_map(self) -> bool:
        """Rebuild the object map if it is flagged invalid.

        The real one shells out to `rbd object-map rebuild` and tolerates the
        rebuild continuing in the background, see
        `tests/hazmat/test_libceph.py`.
        """
        assert not self.closed
        flags = self._image["flags"]
        if "object map invalid" not in flags:
            return False
        flags.remove("object map invalid")
        return True


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
    # PL-134255: pin the legacy feature set (1 = layering), matching what
    # fc-nixos pins. Images are therefore born without `exclusive-lock` and
    # every start has to run them through
    # `VolumeSpecification.ensure_features`, which is exactly the upgrade path
    # we want exercised. Keep this even if the platform raises its own
    # rbdDefaultFeatures.
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


########################################################
# which lock protects which volume
#
#: The volumes that end up on the rados lock, in `Ceph.volumes` order.
EXCLUSIVE_LOCK_VOLUMES = ["root", "swap", "tmp"]

#: The volume that keeps its advisory rbd lock.
RBD_LOCK_VOLUMES = ["cidata"]


@pytest.fixture
def ceph_mock(request, monkeypatch, tmp_path):
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


def make_legacy_images(ceph: Ceph, locked_by: str | None = None):
    """Reproduce the state a pre-PL-134255 host leaves behind.

    That host knows nothing about image features or the rados sidecar lock: it
    creates the images with whatever `rbd_default_features` gives (layering
    only, as pinned by fc-nixos) and holds a per-volume advisory rbd lock on
    every one of them, cidata included.

    This is all that makes a host "old" as far as the ceph layer is concerned,
    so it is also all we need to exercise both halves of the upgrade - no
    second host running old software required:

    `locked_by` defaults to this host, which is the in-place upgrade: the VM
    was started here by the old software and the new one takes over. Pass
    another host to model a VM that is still running over there.
    """
    locked_by = locked_by or ceph.CEPH_LOCK_HOST
    for spec in ceph.specs.values():
        spec.ensure_presence()
        volume = spec.volume
        assert volume, spec.name
        assert volume.features == {"layering"}, (
            f"{volume.fullname} was not born layering-only: "
            f"{volume.features}. Check `rbd_default_features`."
        )
        volume.rbdimage.lock_exclusive(locked_by)
    assert ceph.vm_lock.locker() is None, (
        "an old host cannot have taken the rados lock"
    )


@pytest.fixture
def legacy_images(ceph_inst):
    """`ceph_inst` with the four volumes in pre-upgrade shape."""
    make_legacy_images(ceph_inst)
    yield ceph_inst
    ceph_inst.force_unlock()


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

    def images_to_clean() -> list[str]:
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

        if passes:
            # The first attempt did not get rid of the images, so something is
            # still holding them - show what. Only from the second pass on:
            # dumping the whole process tree for every live test buries the
            # rest of the output
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
    # harmless - `VMLockManager._ensure_lock_object` is idempotent - but a lock
    # still held on one would block the next test. Removing the object drops
    # the lock.
    pool = sysconfig.ceph["orchestration_pool"]
    namespace = VMLockManager.NAMESPACE
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


def running_fc_qemu_processes() -> list[psutil.Process]:
    """Local `fc-qemu` processes, excluding our own.

    Matches the basename of the first two argv entries rather than using
    `pkill -f fc-qemu`: that pattern would be fine, but one written with a
    `.` instead of a `-` silently matches pytest's own command line, because
    the checkout lives in a directory called `fc.qemu`.
    """
    result = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
            if any(Path(arg).name == "fc-qemu" for arg in cmdline[:2]):
                result.append(proc)
        except psutil.Error:  # pragma: no cover - the process went away
            continue
    return result


@pytest.fixture(autouse=True)
def no_leftover_fc_qemu(request, kill_vms):
    """Fail the test that leaves an `fc-qemu` process behind.

    A leaked `fc-qemu` keeps `/run/qemu.<vm>.lock` for as long as it runs -
    up to an hour for an outmigrate that cannot reach its destination - and
    every later test that touches that VM then blocks in `flock` until its own
    timeout. That turns one broken test into a whole cascade of unrelated
    failures, so catch it where it happens.
    """
    is_live = request.node.get_closest_marker("live")
    yield
    if not is_live:
        return
    # Supervisors spawn `fc-qemu ensure` in the background, so give whatever
    # is still running a moment to finish on its own before calling it a leak.
    timeout = TimeOut(10, interval=0.5, raise_on_timeout=False)
    while timeout.tick():
        if not (leftover := running_fc_qemu_processes()):
            return
    for proc in leftover:
        print2(f"killing leaked fc-qemu {proc.pid}: {proc.info['cmdline']}")
        proc.kill()
    pytest.fail(
        f"{request.node.name} leaked {len(leftover)} fc-qemu process(es): "
        + "; ".join(" ".join(p.info["cmdline"] or []) for p in leftover)
    )


@pytest.fixture
def kill_vms_host2(call_host2):
    # `fc-qemu` as well as the VMs: an inmigrate server that outlives its test
    # keeps holding host2's VM lock and stays registered in consul as
    # `vm-inmigrate-<vm>` for up to an hour, which derails every later test
    # that touches the same VM.
    def pkill():
        for pattern in ["supervised-qemu", "qemu-system-x86_64", "fc-qemu"]:
            call_host2(["pkill", "-A", "-f", pattern], exit_codes=[0, 1])

    pkill()
    yield
    pkill()


@pytest.fixture
def reap_subprocesses():
    """Register long-running subprocesses to be killed at teardown.

    Tests that spawn `fc-qemu` and wait for it have to clean up even when they
    do not reach the end of their body - a pytest-timeout fires as an
    exception inside the test, so only teardown is guaranteed to run. A
    surviving `fc-qemu` holds `/run/qemu.<vm>.lock` for as long as it keeps
    retrying, which blocks every subsequent test on the same VM.
    """
    procs: list[subprocess.Popen] = []
    yield procs.append
    for proc in procs:
        if proc.poll() is not None:
            continue
        print2(f"reaping leftover subprocess {proc.pid}: {proc.args}")
        proc.kill()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            print2(f"subprocess {proc.pid} did not die")


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
                result_.append(f"{key}={str(event[key]).strip()}")
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
