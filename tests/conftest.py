import errno
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
from typing import List
from unittest.mock import patch

import mock
import pytest
import rados
import rbd
import structlog

import fc.qemu.agent
import fc.qemu.hazmat.qemu
import fc.qemu.logging
from fc.qemu.agent import Agent
from fc.qemu.hazmat import volume
from fc.qemu.hazmat.ceph import Ceph, RootSpec, VolumeSpecification
from fc.qemu.util import GiB

########################################################
# ceph fixtures


class RadosMock(object):
    def __init__(self, conffile, name):
        self.conffile = conffile
        self.name = name
        self._ioctx = {}
        self.__connected__ = False

    def connect(self):
        assert not self.__connected__
        self.__connected__ = True

    def open_ioctx(self, pool):
        if pool not in self._ioctx:
            self._ioctx[pool] = IoctxMock(pool)
        return self._ioctx[pool]

    def shutdown(self):
        assert self.__connected__
        self.__connected__ = False

    def list_pools(self):
        return ["rbd", "data", "rbd.ssd", "rbd.hdd", "rbd.rgw.foo"]


class IoctxMock(object):
    """Mock access to a pool."""

    def __init__(self, name: str):
        # the rados implementation takes the name as a str, but later returns
        # that attribute as bytes
        self.name = name.encode("ascii")
        self.rbd_images = {}
        self._snapids = 0

    def _rbd_create(self, name, size):
        assert name not in self.rbd_images
        self.rbd_images[name] = dict(size=size, lock=None)

    def _rbd_create_snap(self, name, snapname):
        self._snapids += 1
        fullname = name + "@" + snapname
        assert fullname not in self.rbd_images
        self.rbd_images[fullname] = snap = self.rbd_images[name].copy()
        snap["lock"] = None
        snap["snapid"] = self._snapids

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
            raise rbd.ImageNotFound(self.name)

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
                raise rbd.ImageBusy(errno.EBUSY, "Image is busy")
            return
        raise RuntimeError("unsupported mock path")

    def lock_shared(self, cookie, tag):
        assert not self.closed
        lock = self.ioctx.rbd_images[self.name]["lock"]
        if lock is None:
            self.ioctx.rbd_images[self.name]["lock"] = {
                "tag": tag,
                "exclusive": False,
                "lockers": [("client.xyz", cookie, "127.0.0.1:9999")],
            }
            return
        else:
            if lock["exclusive"]:
                raise rbd.ImageBusy("already exclusively locked")
            if lock["tag"] != tag:
                raise rbd.ImageBusy("wrong tag")
            for l_client, l_cookie, l_addr in list(lock["lockers"]):
                if l_cookie != cookie:
                    lock["lockers"].append(
                        ("client.xyz", cookie, "127.0.0.1:9999")
                    )
                else:
                    raise rbd.ImageExists()
            # XXX we every only calls this from the same host so we never
            # actually get multiple lockers, just valid noops.
            return
        raise RuntimeError("unsupported mock path")

    def list_lockers(self):
        assert not self.closed
        if self.name not in self.ioctx.rbd_images:
            raise rbd.ImageNotFound(self.name)
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
    del env["PYTHONPATH"]
    env.pop("CEPH_ARGS", None)

    def call(cmd):
        print(f"$ {cmd}")
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
    call("ceph osd pool set rbd size 1")
    call("ceph osd pool set rbd min_size 1")
    call("ceph osd pool create rbd.ssd 32")
    call("ceph osd pool set rbd.ssd size 1")
    call("ceph osd pool set rbd.ssd min_size 1")
    call("ceph osd pool create rbd.hdd 32")
    call("ceph osd pool set rbd.hdd size 1")
    call("ceph osd pool set rbd.hdd min_size 1")
    call("ceph osd lspools")
    call("rbd pool init rbd.ssd")
    call("rbd pool init rbd.hdd")
    call("rbd create --size 500 rbd.hdd/fc-21.05-dev")
    call("rbd map rbd.hdd/fc-21.05-dev")
    call("sgdisk /dev/rbd0 -o -a 2048 -n 1:8192:0 -c 1:ROOT -t 1:8300")
    call("partprobe")
    call("mkfs.xfs /dev/rbd0p1")
    call("rbd unmap /dev/rbd0")
    call("rbd snap create rbd.hdd/fc-21.05-dev@v1")
    call("rbd snap protect rbd.hdd/fc-21.05-dev@v1")
    call("rbd create -s 1M rbd/.maintenance")


@pytest.fixture
def ceph_mock(request, monkeypatch, tmp_path):
    is_live = request.node.get_closest_marker("live")
    if is_live is not None:
        # This is a live test. Perform a real Ceph setup.
        # We expect our roles and software to be installed, but
        # no Ceph cluster bootstrapping to have been performed.
        ceph_live_setup()
        yield
        return

    def ensure_presence(self):
        VolumeSpecification.ensure_presence(self)

    def image_map(self):
        if self.device:
            return
        self.device = tmp_path / self.fullname.replace("/", "-")
        raw = self.device.with_name(f"{self.device.name}.raw")
        if not raw.exists():
            with raw.open("wb") as f:
                f.seek(self.size - 1)
                f.write(b"\0")
                f.close()
        raw.rename(self.device)

        # create an implicit first partition as we can't really do the
        # partprobe dance.
        raw = self.part1dev.with_name(f"{self.part1dev.name}.raw")
        with raw.open("wb") as f:
            f.seek(self.size - 1)
            f.write(b"\0")
            f.close()
        raw.rename(self.part1dev)

    def image_unmap(self):
        if self.device is None:
            return
        self.device.rename(self.device.with_name(f"{self.device.name}.raw"))
        self.part1dev.rename(
            self.part1dev.with_name(f"{self.part1dev.name}.raw")
        )
        self.device = None

    monkeypatch.setattr(rados, "Rados", RadosMock)
    monkeypatch.setattr(rbd, "RBD", RBDMock)
    monkeypatch.setattr(rbd, "Image", ImageMock)
    monkeypatch.setattr(volume.Image, "map", image_map)
    monkeypatch.setattr(volume.Image, "unmap", image_unmap)
    monkeypatch.setattr(RootSpec, "ensure_presence", ensure_presence)
    yield


@pytest.fixture
def ceph_inst(ceph_mock):
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


@pytest.fixture(autouse=True)
def clean_rbd_pools(request, kill_vms, ceph_mock):
    is_live = request.node.get_closest_marker("live")
    if not is_live:
        return

    def images_to_clean() -> List[str]:
        images = []
        for pool in ["rbd.hdd", "rbd.ssd"]:
            with subprocess.Popen(
                ["rbd", "ls", pool],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as proc:
                stdout, stderr = proc.communicate(
                    timeout=5
                )  # Add timeout for safety
            for line in stdout.splitlines():
                image = line.strip().decode("ascii")
                if not image or "-" in image:  # empty line or base image
                    continue
                images.append(f"{pool}/{image}")

        return images

    passes = 0
    while images := images_to_clean():
        if passes > 1:
            # Use temporary blacklisting to ensure all watchers are gone.
            ips = [socket.gethostbyname(host) for host in ["host1", "host2"]]
            for ip in ips:
                subprocess.run(
                    f"ceph osd blacklist add {ip}",
                    shell=True,
                )
            # We might be waiting for images stuck with watchers that need
            # to time out ...
            time.sleep(5)
            for ip in ips:
                print(f"blacklist rm {ip}")
                subprocess.run(
                    f"ceph osd blacklist rm {ip}",
                    shell=True,
                )
            time.sleep(5)
        for image in images:
            subprocess.run(
                f"rbd snap purge {image}",
                shell=True,
            )
            subprocess.run(
                f"rbd migration abort {image}",
                shell=True,
            )
            subprocess.run(f"rbd rm {image}", shell=True)
        passes += 1

    # Unmap all mapped devices
    for rbd_dev in Path("/dev/").glob("rbd?"):
        subprocess.run(["rbd", "unmap", str(rbd_dev)])
    subprocess.check_call(
        ["rbd-locktool", "-vvvv", "-u", "-f", "rbd/.maintenance"],
    )


########################################################
# qemu/kvm related fixtures


@pytest.fixture
def vm(clean_environment, monkeypatch, tmpdir):
    import fc.qemu.hazmat.qemu

    monkeypatch.setattr(fc.qemu.hazmat.qemu.Qemu, "guestagent_timeout", 0.1)
    simplevm_cfg = Path(__file__).parent / "fixtures" / "simplevm.yaml"
    shutil.copy(simplevm_cfg, "/etc/qemu/vm/simplevm.cfg")
    Path("/etc/qemu/vm/.simplevm.cfg.staging").unlink(missing_ok=True)

    vm = Agent("simplevm")
    vm.timeout_graceful = 1
    vm.__enter__()
    vm.qemu.qmp_timeout = 0.1
    vm.qemu.vm_expected_overhead = 128

    if vm.qemu.is_running():
        vm.kill()

    for open_volume in vm.ceph.opened_volumes:
        for snapshot in open_volume.snapshots:
            snapshot.remove()
    vm.unlock()
    for service in vm.consul.agent.services():
        try:
            vm.consul.agent.service.deregister(vm.svc_name)
        except Exception:
            vm.log.exception("consul-deregister-failed", exc_info=True)

    get_log()

    yield vm

    exc_info = sys.exc_info()

    for open_volume in vm.ceph.opened_volumes:
        for snapshot in open_volume.snapshots:
            snapshot.remove()

    if vm.qemu.is_running():
        vm.kill()

    for service in vm.consul.agent.services():
        try:
            vm.consul.agent.service.deregister(vm.svc_name)
        except Exception:
            vm.log.exception("consul-deregister-failed", exc_info=True)

    vm.__exit__(*exc_info)
    if len(exc_info):
        print(traceback.print_tb(exc_info[2]))


@pytest.fixture(autouse=True)
def kill_vms(request, call_host2, ceph_mock):
    is_live = request.node.get_closest_marker("live")
    if not is_live:
        yield
        return

    assert_call(["pkill", "-A", "-f", "supervised-qemu"], exit_codes=[0, 1])
    assert_call(["pkill", "-A", "-f", "qemu-system-x86_64"], exit_codes=[0, 1])
    call_host2(["pkill", "-A", "-f", "supervised-qemu"], exit_codes=[0, 1])
    call_host2(["pkill", "-A", "-f", "qemu-system-x86_64"], exit_codes=[0, 1])
    subprocess.call("fc-qemu force-unlock simplevm", shell=True)
    yield
    assert_call(["pkill", "-A", "-f", "supervised-qemu"], exit_codes=[0, 1])
    assert_call(["pkill", "-A", "-f", "qemu-system-x86_64"], exit_codes=[0, 1])
    call_host2(["pkill", "-A", "-f", "supervised-qemu"], exit_codes=[0, 1])
    call_host2(["pkill", "-A", "-f", "qemu-system-x86_64"], exit_codes=[0, 1])

    subprocess.call("fc-qemu force-unlock simplevm", shell=True)


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
def clean_environment():
    logpath = Path("/var/log/vm")
    if logpath.glob("*"):
        subprocess.run("rm /var/log/vm/*", shell=True)
    yield
    print(getoutput("free"))
    print(getoutput("ceph df"))
    print(getoutput("ps auxf"))
    print(getoutput("df -h"))
    print(getoutput("rbd showmapped"))
    print(getoutput("journalctl --since -30s"))
    if list(logpath.glob("*")):
        print(getoutput("tail -n 50 /var/log/vm/*"))
        subprocess.run("rm /var/log/vm/*", shell=True)


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

    def test_logger(logger, method_name, event):
        stack = event.pop("stack", None)
        exc = event.pop("exception", None)
        event_name = event.pop("event", "")
        event_prefix = os.path.basename(event_name) if event_name else " "
        result = []
        if "output_line" in event:
            result = fc.qemu.logging.prefix(
                event_prefix, event["output_line"].strip()
            )
        else:
            output = event.pop("output", None)

            result = []
            if event_name:
                result.append(event_name)
            for key in sorted(event):
                result.append("{}={}".format(key, str(event[key]).strip()))
            result = " ".join(result)

            if output:
                result += fc.qemu.logging.prefix(event_prefix, output)

        # Ensure we get something to read on stdout in case we have errors.
        reltime = time.time() - util.test_log_start
        print(f"{reltime:08.4f} {result}")
        if stack:
            print(stack)
        if exc:
            print(exc)

        # Allow tests to inspect only methods and events they are interested
        # in. This reduces noise in our test outputs and comparisons and
        # reduces fragility.
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
    util.test_log_options = {"show_methods": [], "show_events": []}


@pytest.fixture()
@patch('fc.qemu.directory.connect', autospec=True)
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


def pytest_collection_modifyitems(items):
    """Modifies test items in place to ensure test modules run in a given order."""
    # A few tests need to be run very very late as they track the behaviour of
    # the other tests.
    sorted_items = items.copy()
    random.shuffle(sorted_items)
    sorted_items.sort(
        key=lambda item: 1 if list(item.iter_markers("last")) else 0
    )
    items[:] = sorted_items


def pytest_assertrepr_compare(op, left, right):
    if left.__class__.__name__ == "Ellipsis":
        return left.compare(right).diff
    elif right.__class__.__name__ == "Ellipsis":
        return right.compare(left).diff
