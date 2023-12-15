import os
import shutil
import subprocess
import sys
import time
import traceback

import pkg_resources
import pytest
import structlog

import fc.qemu.agent
import fc.qemu.hazmat.qemu

from .agent import Agent


def pytest_collectstart(collector):
    from fc.qemu.sysconfig import sysconfig

    sysconfig.load_system_config()


@pytest.fixture(autouse=True)
def synthetic_root(request, monkeypatch, tmpdir):
    is_live = request.node.get_closest_marker("live")
    if is_live is not None:
        # This is a live test. Do not mock things.
        return
    monkeypatch.setattr(fc.qemu.hazmat.qemu.Qemu, "prefix", str(tmpdir))
    os.makedirs(str(tmpdir / "run"))
    os.makedirs(str(tmpdir / "etc/qemu/vm"))
    monkeypatch.setattr(Agent, "prefix", str(tmpdir))
    monkeypatch.setattr(fc.qemu.agent, "EXECUTABLE", "true")


@pytest.fixture(scope="session")
def setup_structlog():
    from . import util

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
        print(result)
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

    structlog.configure(
        processors=(
            ([structlog.processors.format_exc_info] if log_exceptions else [])
            + [test_logger]
        )
    )


@pytest.fixture(autouse=True)
def reset_structlog(setup_structlog):
    from . import util

    util.log_data = []
    util.test_log_options = {"show_methods": [], "show_events": []}


def pytest_assertrepr_compare(op, left, right):
    if left.__class__.__name__ == "Ellipsis":
        return left.compare(right).diff
    elif right.__class__.__name__ == "Ellipsis":
        return right.compare(left).diff


CALLED_BINARIES = set([])


@pytest.fixture(autouse=True)
def record_subprocess_calls(monkeypatch):
    original = subprocess.Popen.__init__

    def Popen_recording_init(self, *args, **kw):
        binary = args[0][0]
        if kw.get("shell"):
            binary = args[0].split()[0]
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
        CALLED_BINARIES.add(binary)
        return subprocess_run_orig(*args, **kw)

    monkeypatch.setattr(subprocess, "run", recording_subprocess_run)


@pytest.fixture
def clean_environment():
    def clean():
        subprocess.call(["pkill", "-f", "supervised-qemu"])
        subprocess.call(["pkill", "-f", "qemu-system-x86_64"])
        subprocess.call("rbd rm rbd.ssd/simplevm.swap", shell=True)
        subprocess.call("rbd snap purge rbd.ssd/simplevm.root", shell=True)
        subprocess.call("rbd rm rbd.ssd/simplevm.root", shell=True)
        subprocess.call("rbd rm rbd.ssd/simplevm.tmp", shell=True)
        time.sleep(1)

    clean()

    print(
        subprocess.check_output(
            "rbd ls rbd.ssd",
            shell=True,
        ).decode("ascii")
    )

    yield
    clean()

    print(
        subprocess.check_output(
            "free && sync && echo 3 > /proc/sys/vm/drop_caches && free",
            shell=True,
        ).decode("ascii")
    )
    print(
        subprocess.check_output(
            "ceph df",
            shell=True,
        ).decode("ascii")
    )
    print(
        subprocess.check_output(
            "ps auxf",
            shell=True,
        ).decode("ascii")
    )
    print(
        subprocess.check_output(
            "df -h",
            shell=True,
        ).decode("ascii")
    )
    print(
        subprocess.check_output(
            "rbd showmapped",
            shell=True,
        ).decode("ascii")
    )


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
def vm(clean_environment, monkeypatch, tmpdir):
    import fc.qemu.hazmat.qemu

    monkeypatch.setattr(fc.qemu.hazmat.qemu.Qemu, "guestagent_timeout", 0.1)
    fixtures = pkg_resources.resource_filename(__name__, "tests/fixtures")
    shutil.copy(fixtures + "/simplevm.yaml", "/etc/qemu/vm/simplevm.cfg")
    if os.path.exists("/etc/qemu/vm/.simplevm.cfg.staging"):
        os.unlink("/etc/qemu/vm/.simplevm.cfg.staging")
    vm = Agent("simplevm")
    vm.timeout_graceful = 1
    vm.__enter__()
    vm.qemu.qmp_timeout = 0.1
    vm.qemu.vm_expected_overhead = 128
    for snapshot in vm.ceph.root.snapshots:
        snapshot.remove()
    vm.qemu.destroy(kill_supervisor=True)
    vm.unlock()
    get_log()
    yield vm

    for snapshot in vm.ceph.root.snapshots:
        snapshot.remove()
    exc_info = sys.exc_info()
    vm.__exit__(*exc_info)
    if len(exc_info):
        print(traceback.print_tb(exc_info[2]))


def get_log():
    from fc.qemu import util

    result = "\n".join(util.log_data)
    util.log_data = []
    return result
