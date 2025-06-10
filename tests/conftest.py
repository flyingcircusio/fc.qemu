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
from typing import List
from unittest.mock import patch

import mock
import pytest
import structlog

import fc.qemu.agent
import fc.qemu.hazmat.qemu
import fc.qemu.logging
from fc.qemu.agent import Agent
from fc.qemu.hazmat import libceph
from fc.qemu.hazmat.ceph import Ceph, RootSpec, VolumeSpecification
from fc.qemu.util import GiB

########################################################
# ceph fixtures


class RadosMock(object):
    tmp_path: Path

    def __init__(self, conffile, name, log):
        self.conffile = conffile
        self.name = name
        self._ioctx = {}

    def open_ioctx(self, pool):
        if pool not in self._ioctx:
            self._ioctx[pool] = IoctxMock(pool, self.tmp_path)
        return self._ioctx[pool]

    def list_pools(self):
        return ["rbd", "data", "rbd.ssd", "rbd.hdd", "rbd.rgw.foo"]


class IoctxMock(object):
    """Mock access to a pool."""

    def __init__(self, name: str, tmp_path: Path):
        # the rados implementation takes the name as a str, but later returns
        # that attribute as bytes
        self.tmp_path = tmp_path
        self.name = name
        self.rbd_images = {}
        self._snapids = 0

    def _rbd_create(self, name, size):
        assert name not in self.rbd_images
        self.rbd_images[name] = image = dict(size=size, lock=None)
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

    monkeypatch.setattr(libceph, "Rados", RadosMock)
    monkeypatch.setattr(libceph, "RBD", RBDMock)
    monkeypatch.setattr(libceph, "Image", ImageMock)
    monkeypatch.setattr(RootSpec, "ensure_presence", ensure_presence)
    RadosMock.tmp_path = tmp_path
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
                print2(f"ceph osd blacklist add {ip}")
                subprocess.run(
                    f"ceph osd blacklist add {ip}",
                    shell=True,
                )
            # We might be waiting for images stuck with watchers that need
            # to time out ...
            time.sleep(5)
            for ip in ips:
                print2(f"blacklist rm {ip}")
                subprocess.run(
                    f"ceph osd blacklist rm {ip}",
                    shell=True,
                )
            time.sleep(5)

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


########################################################
# qemu/kvm related fixtures


def named_vm(name, request, clean_environment, monkeypatch, tmpdir):
    import fc.qemu.hazmat.qemu

    monkeypatch.setattr(fc.qemu.hazmat.qemu.Qemu, "guestagent_timeout", 0.1)
    monkeypatch.setattr(fc.qemu.hazmat.qemu, "FREEZE_TIMEOUT", 1)
    monkeypatch.setattr(fc.qemu.hazmat.guestagent, "SYNC_TIMEOUT", 1)

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

    if vm.qemu.is_running():
        vm.kill()

    for open_volume in vm.ceph.opened_volumes:
        for snapshot in open_volume.snapshots:
            snapshot.remove()
    vm.force_unlock()
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
    if logpath.glob("*"):
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
