import contextlib
import copy
import datetime
import distutils.spawn
import fcntl
import importlib.resources
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
import typing
from codecs import decode
from multiprocessing.pool import ThreadPool
from pathlib import Path

import colorama
import consulate
import consulate.models.agent
import requests
import yaml

from . import directory, util
from .exc import (
    ConfigChanged,
    InvalidCommand,
    VMConfigNotFound,
    VMStateInconsistent,
)
from .hazmat.ceph import Ceph
from .hazmat.cpuscan import scan_cpus
from .hazmat.qemu import Qemu, detect_current_machine_type
from .incoming import IncomingServer
from .outgoing import Outgoing
from .sysconfig import sysconfig
from .timeout import TimeOut
from .util import GiB, MiB, locate_live_service, log


def _handle_consul_event(event):
    handler = ConsulEventHandler()
    handler.handle(event)


OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3

EXECUTABLE = sys.argv[0]  # make mockable

MAINTENANCE_TEMPFAIL = 75  # Retry shortly
MAINTENANCE_POSTPONE = 69  # Retry when the directory schedules a new window
MAINTENANCE_VOLUME = "rbd/.fc-qemu.maintenance"
LOCKTOOL_TIMEOUT_SECS = 30
UNLOCK_MAX_RETRIES = 5


def unwrap_consul_armour(value: str) -> object:
    # See test_consul.py:prepare_consul_event.
    # The directory is providing a somewhat weird (likely historical)
    # structure with an ASCII armour. Due to Python only encoding/decoding
    # base64 on binary strings we need to do this little dance here.
    value = value.encode("ascii")
    value = decode(value, "base64")
    value = value.decode("ascii")
    return json.loads(value)


class ConsulEventHandler(object):
    """This is a wrapper that processes various consul events and multiplexes
    them into individual method handlers.

    """

    def handle(self, event):
        """Actual handling of a single Consul event in a
        separate process."""
        try:
            log.debug("handle-key", key=event["Key"])
            prefix = event["Key"].split("/")[0]
            if not event["Value"]:
                log.debug("ignore-key", key=event["Key"], reason="empty value")
                return
            getattr(self, prefix)(event)
        except BaseException as e:  # noqa
            # This must be a bare-except as it protects threads and the main
            # loop from dying. It could be that I'm wrong, but I'm leaving this
            # in for good measure.
            log.exception(
                "handle-key-failed", key=event.get("Key", None), exc_info=True
            )
        log.debug("finish-handle-key", key=event.get("Key", None))

    def node(self, event):
        config = unwrap_consul_armour(event["Value"])

        config["consul-generation"] = event["ModifyIndex"]
        vm = config["name"]
        if config["parameters"]["machine"] != "virtual":
            log.debug(
                "ignore-consul-event",
                machine=vm,
                reason="is a physical machine",
            )
            return
        log_ = log.bind(machine=vm)
        agent = Agent(vm, config)
        log_.info("processing-consul-event", consul_event="node")
        if agent.stage_new_config():
            cmd = [EXECUTABLE, "-D", "ensure", vm]
            log_.info("launch-ensure", cmd=cmd)
            s = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            log_.debug("launch-ensure", subprocess_pid=s.pid)
            stdout, stderr = s.communicate()
            exit_code = s.wait()
            log_.debug("launch-ensure", exit_code=exit_code)
            if exit_code:
                # Avoid logging things twice. However, if it failed then
                # we might be missing output that was generated before setting
                # up logging.
                log_.debug("launch-ensure", output=stdout)
        else:
            log_.info(
                "ignore-consul-event", machine=vm, reason="config is unchanged"
            )

    def snapshot(self, event):
        value = unwrap_consul_armour(event["Value"])
        vm = value["vm"]
        snapshot = value["snapshot"]
        log_ = log.bind(snapshot=snapshot, machine=vm)
        try:
            agent = Agent(vm)
        except RuntimeError:
            log_.warning("snapshot-ignore", reason="failed loading config")
            return
        with agent:
            log_.info("snapshot")
            try:
                agent.snapshot(snapshot, keep=0)
            except InvalidCommand:
                # The VM isn't in a state to make a snapshot. This is important
                # information for regular users but not for consul - ignoring
                # it is the right choice here. This will happen regularly
                # when the VM is running on a different host.
                pass


def running(expected=True):
    def wrap(f):
        def checked(self, *args, **kw):
            if self.qemu.is_running() != expected:
                self.log.error(
                    f.__name__,
                    expected="VM {}running".format("" if expected else "not "),
                )
                raise InvalidCommand(
                    "Invalid command: VM must {}be running to use `{}`.".format(
                        "" if expected else "not ", f.__name__
                    )
                )
            return f(self, *args, **kw)

        return checked

    return wrap


def locked(blocking=True):
    # This is thread-safe *AS LONG* as every thread uses a separate instance
    # of the agent. Using multiple file descriptors will guarantee that the
    # lock can only be held once even within a single process.
    #
    # However, we ensure locking status even for re-entrant / recursive usage.
    # For that we keep a counter how often we successfully acquired the lock
    # and then unlock when we're back to zero.
    def lock_decorator(f):
        def locked_func(self, *args, **kw):
            # New lock file behaviour: lock with a global file that is really
            # only used for this purpose and is never replaced.
            self.log.debug("acquire-lock", target=str(self.lock_file))
            if not self._lock_file_fd:
                if not os.path.exists(self.lock_file):
                    open(self.lock_file, "a+").close()
                self._lock_file_fd = os.open(self.lock_file, os.O_RDONLY)
            mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0)
            try:
                fcntl.flock(self._lock_file_fd, mode)
            except IOError:
                # This happens in nonblocking mode and we just give up as
                # that's what's expected to speed up things.
                self.log.info(
                    "acquire-lock",
                    result="failed",
                    action="exit",
                    mode="nonblocking",
                )
                return os.EX_TEMPFAIL
            self._lock_count += 1
            self.log.debug(
                "acquire-lock",
                target=str(self.lock_file),
                result="locked",
                count=self._lock_count,
            )
            try:
                return f(self, *args, **kw)
            finally:
                self._lock_count -= 1
                self.log.debug(
                    "release-lock",
                    target=self.lock_file,
                    count=self._lock_count,
                )
                if self._lock_count == 0:
                    try:
                        fcntl.flock(self._lock_file_fd, fcntl.LOCK_UN)
                        self.log.debug(
                            "release-lock",
                            target=self.lock_file,
                            result="unlocked",
                        )
                    except:  # noqa
                        self.log.debug(
                            "release-lock",
                            exc_info=True,
                            target=self.lock_file,
                            result="error",
                        )
                    os.close(self._lock_file_fd)
                    self._lock_file_fd = None

        return locked_func

    return lock_decorator


def swap_size(memory):
    """Returns the swap partition size in bytes."""
    swap_mib = max(1024, 32 * math.sqrt(memory))
    return int(swap_mib) * MiB


def tmp_size(disk):
    """Returns the tmp partition size in bytes."""
    tmp_gib = max(5, math.sqrt(disk))
    return int(tmp_gib) * GiB


class Agent(object):
    """The agent to control a single VM."""

    # Attributes on this class can be overriden (in a controlled fashion
    # from the sysconfig module. See this class' __init__. The defaults
    # are here to support testing.

    prefix = Path("/")
    this_host = ""
    migration_ctl_address = None
    accelerator = ""
    machine_type = "pc-i440fx"
    vhost = False
    ceph_id = "admin"
    timeout_graceful = 10

    system_config_template = Path("etc/qemu/qemu.vm.cfg.in")
    # XXX the importlib schema requiring a context manager
    # is not helpful here.
    builtin_config_template = Path(__file__).parent / "qemu.vm.cfg.in"
    consul_token = None
    consul_generation = -1

    # The binary generation is used to signal into a VM that the host
    # environment has changed in a way that requires a _cold_ reboot, thus
    # asking the VM to shut down cleanly so we can start it again. Change this
    # introducing a new Qemu machine type, or providing security fixes that
    # can't be applied by live migration.
    # The counter should be increased through the platform management as
    # this may change independently from fc.qemu releases.
    binary_generation = 0

    # For upgrade-purposes we're running an old and a new locking mechanism
    # in step-lock. We used to lock the config file but we're using rename to
    # update it atomically. That's not compatible and will result in a consul
    # event replacing the file and then getting a lock on the new file while
    # there still is another process running. This will result in problems
    # accessing the QMP socket as that only accepts a single connection and
    # will then time out.
    _lock_count = 0
    _lock_file_fd = None
    _config_file_fd = None

    def __init__(self, name, enc=None):
        # Update configuration values from system or test config.
        self.log = log.bind(machine=name)

        self.__dict__.update(sysconfig.agent)

        self.name = name

        self.system_config_template = self.prefix / self.system_config_template

        # The ENC data is the parsed configuration from the config file, or
        # from consul (or from wherever). This is intended to stay true to
        # the config file format (not adding computed values or manipulating
        # them) so that we can generate a new config file based on this data
        # through `stage_new_config()`.
        if enc is not None:
            self.enc = enc
        else:
            self.enc = self._load_enc()

        self.consul = consulate.Consul(token=self.consul_token)
        self.contexts = ()

    @property
    def config_file(self):
        return self.prefix / "etc/qemu/vm" / f"{self.name}.cfg"

    @property
    def config_file_staging(self):
        return self.prefix / "etc/qemu/vm" / f".{self.name}.cfg.staging"

    @property
    def lock_file(self):
        return self.prefix / "run" / f"qemu.{self.name}.lock"

    def _load_enc(self):
        try:
            with self.config_file.open() as f:
                return yaml.safe_load(f)
        except IOError:
            if self.config_file_staging.exists():
                # The VM has been freshly created. Set up a boilerplate
                # configuration and let the actual config be established
                # through the 'ensure' command.
                return None
            else:
                self.log.error("unknown-vm")
                raise VMConfigNotFound(
                    "Could not load {}".format(self.config_file)
                )

    @classmethod
    def handle_consul_event(cls, input: typing.TextIO = sys.stdin):
        # Python in our environments defaults to UTF-8 and we generally
        # should be fine expecting a TextIO here as json load also expects
        # text input. It might be necessary at some point to explicitly
        # expect BinaryIO input and then enforce UTF-8 at this point.
        events = json.load(input)
        if not events:
            return
        log.info("start-consul-events", count=len(events))
        pool = ThreadPool(sysconfig.agent.get("consul_event_threads", 3))
        for event in sorted(events, key=lambda e: e.get("Key")):
            pool.apply_async(_handle_consul_event, (event,))
            time.sleep(0.05)
        pool.close()
        pool.join()
        log.info("finish-consul-events")

    @classmethod
    def _vm_agents_for_host(cls):
        for candidate in sorted((cls.prefix / "run").glob("qemu.*.pid")):
            name = candidate.name.replace("qemu.", "").replace(".pid", "")
            try:
                agent = Agent(name)
                yield agent
            except Exception:
                log.exception("load-agent", machine=name, exc_info=True)

    @classmethod
    def report_supported_cpu_models(self):
        variations = []
        for variation in scan_cpus():
            log.info(
                "supported-cpu-model",
                architecture=variation.model.architecture,
                id=variation.cpu_arg,
                description=variation.model.description,
            )
            variations.append(variation.cpu_arg)

        d = directory.connect()
        d.report_supported_cpu_models(variations)

    @classmethod
    def ls(cls):
        """List all VMs that this host knows about.

        This means specifically that we have status (PID) files for the
        VM in the expected places.

        Gives a quick status for each VM.
        """
        for vm in cls._vm_agents_for_host():
            with vm:
                running = vm.qemu.process_exists()

                if running:
                    vm_mem = vm.qemu.proc().memory_full_info()

                    log.info(
                        "online",
                        machine=vm.name,
                        cores=vm.cfg["cores"],
                        memory_booked="{:,.0f}".format(vm.cfg["memory"]),
                        memory_pss="{:,.0f}".format(vm_mem.pss / MiB),
                        memory_swap="{:,.0f}".format(vm_mem.swap / MiB),
                    )
                else:
                    log.info("offline", machine=vm.name)

    @classmethod
    def _ensure_maintenance_volume(self):
        try:
            log.debug("ensure-maintenance-volume")
            subprocess.run(
                ["rbd-locktool", "-q", "-i", MAINTENANCE_VOLUME],
                check=True,
                timeout=LOCKTOOL_TIMEOUT_SECS,
            )
        except subprocess.CalledProcessError:
            log.debug("creating maintenance volume")
            subprocess.run(["rbd", "create", "--size", "1", MAINTENANCE_VOLUME])

    @classmethod
    def _get_maintenance_lock_info(self):
        try:
            return subprocess.check_output(
                ["rbd-locktool", "-i", MAINTENANCE_VOLUME]
            ).decode("utf-8", errors="replace")
        except Exception as e:
            return f"<unknown due to error: {e}>"

    @classmethod
    def maintenance_enter(cls) -> None:
        """Prepare the host for maintenance mode.

        process exit codes signal success or (temporary) failure
        """
        log.debug("enter-maintenance")
        try:
            cls._ensure_maintenance_volume()
            log.debug("acquire-maintenance-lock")
            subprocess.run(
                ["rbd-locktool", "-l", MAINTENANCE_VOLUME],
                check=True,
                timeout=LOCKTOOL_TIMEOUT_SECS,
            )
        # locking can block on a busy cluster, causing the whole agent (and all
        # other agent operations waiting for the global agent lock) to be
        # stuck
        except subprocess.TimeoutExpired:
            # We cannot know whether the lock has succeeded despite the
            # timeout, so attempt an unlock again.
            log.debug(
                "acquire-maintenance-lock",
                result="timeout",
                lock_info=cls._get_maintenance_lock_info(),
            )
            cls.maintenance_leave()
            sys.exit(MAINTENANCE_TEMPFAIL)
        # already locked by someone else
        except subprocess.CalledProcessError:
            log.debug(
                "acquire-maintenance-lock",
                result="already locked",
                lock_info=cls._get_maintenance_lock_info(),
            )
            sys.exit(MAINTENANCE_TEMPFAIL)

        d = directory.connect()
        host = socket.gethostname()
        for attempt in range(3):
            log.info("request-evacuation")
            evacuated = d.evacuate_vms(host)
            if not evacuated:
                # need to call evacuate_vms again to arrive at the empty set
                break
            log.info("evacuation-started", vms=evacuated)
            time.sleep(5)

        log.info("evacuation-pending")
        # Trigger a gratuitous event handling cycle to help speed up the
        # migration.
        subprocess.call(["systemctl", "reload", "consul"])

        # Monitor whether there are still VMs running.
        timeout = TimeOut(300, interval=3)
        while timeout.tick():
            process = subprocess.Popen(
                ["pgrep", "-f", "qemu-system-x86_64"], stdout=subprocess.PIPE
            )
            process.wait()
            assert process.stdout is not None
            num_procs = len(process.stdout.read().splitlines())
            log.info(
                "evacuation-running",
                vms=num_procs,
                timeout_remaining=timeout.remaining,
            )
            if num_procs == 0:
                # We made it: no VMs remaining, so we can proceed with the
                # maintenance.
                log.info("evacuation-success")
                sys.exit(0)
            time.sleep(10)

        log.info("evacuation-timeout", action="retry maintenance")
        sys.exit(MAINTENANCE_TEMPFAIL)

    @classmethod
    def maintenance_leave(cls):
        log.debug("leave-maintenance")
        last_exc = None
        for _ in range(UNLOCK_MAX_RETRIES):
            try:
                cls._ensure_maintenance_volume()
                log.debug("release-maintenance-lock")
                subprocess.run(
                    ["rbd-locktool", "-q", "-u", MAINTENANCE_VOLUME],
                    check=True,
                    timeout=LOCKTOOL_TIMEOUT_SECS,
                )
            except subprocess.TimeoutExpired as e:
                print(f"WARNING: Maintenance leave timed out at {e.cmd}.")
                last_exc = e
                time.sleep(
                    LOCKTOOL_TIMEOUT_SECS / 5
                )  # cooldown time for cluster
                continue
            break
        else:
            print(
                "WARNING: All maintenance leave attempts have timed out, "
                "the fc.qemu maintenance lock might not be properly unlocked."
            )
            # deliberately re-raise the exception, as this situation shall be checked by
            # an operator
            raise (
                last_exc
                if last_exc
                else RuntimeError("fc-qemu maintenance unlock failed")
            )

    @classmethod
    def shutdown_all(cls) -> None:
        """Shut down all VMs cleanly.

        Runs the shutdowns in parallel to speed up host reboots.
        """
        vms = []

        for vm in cls._vm_agents_for_host():
            with vm:
                running = vm.qemu.process_exists()
                if running:
                    vms.append(vm)

        log.info("shutdown-all", count=len(vms))

        if not vms:
            return

        if sys.stdout.isatty():
            print(
                colorama.Fore.RED,
                "The following VMs will be shut down: ",
                colorama.Style.RESET_ALL,
            )
            print("\t" + ",".join(vm.name for vm in vms))
            print()
            while True:
                choice = input(
                    colorama.Style.BRIGHT
                    + colorama.Fore.RED
                    + "[DANGER] Are you really sure to shut down all {} VMs? (yes/no)\n".format(
                        len(vms)
                    )
                    + colorama.Style.RESET_ALL
                )
                if choice == "yes":
                    break
                elif choice == "no":
                    return
                else:
                    print("Please type 'yes' or 'no'.")

        def stop_vm(vm):
            # Isolate the stop call into separate fc-qemu
            # processes to ensure reliability.
            log.info("shutdown", vm=vm.name)
            process = subprocess.Popen([EXECUTABLE, "stop", vm.name])
            process.wait()

        pool = ThreadPool(5)
        for vm in vms:
            pool.apply_async(stop_vm, (vm,))
            time.sleep(0.5)
        pool.close()
        pool.join()
        log.info("shutdown-all", result="finished")

    @classmethod
    def check(cls):
        """Perform a health check of this host from a Qemu perspective.

        Checks:

        * no VM exceeds their PSS by guest memory + 2x expected overhead
        * VMs have very little swap (<1 GiB)
        * the total of the VMs PSS does not exceed
          total of guest + expected overhead

        Sets exit code according to the Nagios specification.

        """
        vms = list(cls._vm_agents_for_host())

        large_overhead_vms = []
        swapping_vms = []
        total_guest_and_overhead = 0
        expected_guest_and_overhead = 0

        # individual VMs ok?
        for vm in vms:
            with vm:
                try:
                    vm_mem = vm.qemu.proc().memory_full_info()
                except Exception:
                    # It's likely that the process went away while we analyzed
                    # it. Ignore.
                    continue
                if vm_mem.swap > 1 * GiB:
                    swapping_vms.append(vm)
                expected_size = (
                    vm.cfg["memory"] * MiB
                    + 2 * vm.qemu.vm_expected_overhead * MiB
                )
                expected_guest_and_overhead += (
                    vm.cfg["memory"] * MiB + vm.qemu.vm_expected_overhead * MiB
                )
                total_guest_and_overhead += vm_mem.pss
                if vm_mem.pss > expected_size:
                    large_overhead_vms.append(vm)

        output = []
        result = OK
        if large_overhead_vms:
            result = WARNING
            output.append(
                "VMs with large overhead: "
                + ",".join(x.name for x in large_overhead_vms)
            )
        if swapping_vms:
            result = WARNING
            output.append(
                "VMs swapping:" + ",".join(x.name for x in swapping_vms)
            )
        if total_guest_and_overhead > expected_guest_and_overhead:
            result = CRITICAL
            output.append("High total overhead")

        if result is OK:
            output.insert(0, "OK")
        elif result is WARNING:
            output.insert(0, "WARNING")
        elif result is CRITICAL:
            output.insert(0, "CRITICAL")
        else:
            output.insert(0, "UNKNOWN")

        output.insert(1, "{} VMs".format(len(vms)))
        output.insert(
            2, "{:,.0f} MiB used".format(total_guest_and_overhead / MiB)
        )
        output.insert(
            3, "{:,.0f} MiB expected".format(expected_guest_and_overhead / MiB)
        )

        print(" - ".join(output))

        return result

    def stage_new_config(self):
        """Save the current config on the agent into a staging config file.

        This method is safe to call outside of the agent's context manager.

        """
        # Ensure the config directory exists
        self.config_file.parent.mkdir(parents=True, exist_ok=True)

        # Ensure the staging file exists.
        self.config_file_staging.touch()
        staging_lock = os.open(self.config_file_staging, os.O_RDONLY)
        fcntl.flock(staging_lock, fcntl.LOCK_EX)
        self.log.debug(
            "acquire-staging-lock",
            target=self.config_file_staging,
            result="locked",
        )
        try:
            update_staging_config = False
            # Verify generation of config to protect against lost updates.
            with self.config_file_staging.open("r") as current_staging:
                try:
                    current_staging_config = yaml.safe_load(current_staging)
                    current_staging_generation = current_staging_config[
                        "consul-generation"
                    ]
                except Exception:
                    self.log.debug("inconsistent-staging-config")
                    # Inconsistent staging configs should be updated
                    update_staging_config = True
                else:
                    # Newer staging configs should be updated
                    update_staging_config = (
                        self.enc["consul-generation"]
                        > current_staging_generation
                    )

            if update_staging_config:
                self.log.debug("save-staging-config")
                # Update the file in place to avoid lock breaking.
                with self.config_file_staging.open("w") as new_staging:
                    yaml.safe_dump(self.enc, new_staging)
                self.config_file_staging.chmod(0o644)

            # Do we need to activate this config?
            activate_staging_config = False
            try:
                with self.config_file.open("r") as current_active:
                    current_active_config = yaml.safe_load(current_active)
                current_active_generation = current_active_config[
                    "consul-generation"
                ]
            except Exception:
                self.log.debug("inconsistent-active-config")
                # Inconsistent staging configs should be updated
                activate_staging_config = True
            else:
                # Newer staging configs should be updated
                activate_staging_config = (
                    self.enc["consul-generation"] > current_active_generation
                )
            return activate_staging_config
        finally:
            fcntl.flock(staging_lock, fcntl.LOCK_UN)
            os.close(staging_lock)
            self.log.debug(
                "release-staging-lock",
                target=self.config_file_staging,
                result="released",
            )

    @locked()
    def activate_new_config(self):
        """Activate the current staged config (if any).

        After calling this method, the agent's context manager must be
        re-entered to activate the changed configuration.

        """
        if not self.config_file_staging.exists():
            self.log.debug("check-staging-config", result="none")
            return False

        staging_lock = os.open(self.config_file_staging, os.O_RDONLY)
        fcntl.flock(staging_lock, fcntl.LOCK_EX)
        self.log.debug(
            "acquire-staging-lock",
            target=self.config_file_staging,
            result="locked",
        )
        try:
            # Verify generation of config to protect against lost updates.
            with self.config_file_staging.open("r") as current_staging:
                try:
                    current_staging_config = yaml.safe_load(current_staging)
                    staging_generation = current_staging_config[
                        "consul-generation"
                    ]
                except Exception:
                    self.log.debug(
                        "update-check", result="inconsistent", action="purge"
                    )
                    self.config_file_staging.unlink(missing_ok=True)
                    return False
                else:
                    if staging_generation <= self.consul_generation:
                        # Stop right here, do not write a new config if the
                        # existing one is newer (or as new) already.
                        self.log.debug(
                            "update-check",
                            result="stale-update",
                            action="ignore",
                            update=staging_generation,
                            current=self.consul_generation,
                        )
                        # The old staging file needs to stay around so that
                        # the consul writer knows whether to launch an ensure
                        # agent or not.
                        return False
                self.log.debug(
                    "update-check",
                    result="update-available",
                    action="update",
                    update=staging_generation,
                    current=self.consul_generation,
                )

            # The config seems consistent and newer, lets update.
            # We can replace the config file because that one is protected
            # by the global VM lock.
            shutil.copy2(self.config_file_staging, self.config_file)
            self.enc = self._load_enc()
            return True
        finally:
            fcntl.flock(staging_lock, fcntl.LOCK_UN)
            os.close(staging_lock)
            self.log.debug(
                "release-staging-lock",
                target=self.config_file_staging,
                result="released",
            )

    def has_new_config(self):
        if not self.config_file_staging.exists():
            self.log.debug("check-staging-config", result="none")
            return False

        staging_lock = os.open(self.config_file_staging, os.O_RDONLY)
        fcntl.flock(staging_lock, fcntl.LOCK_EX)
        self.log.debug(
            "acquire-staging-lock",
            target=self.config_file_staging,
            result="locked",
        )
        try:
            # Verify generation of config to protect against lost updates.
            with self.config_file_staging.open("r") as current_staging:
                try:
                    current_staging_config = yaml.safe_load(current_staging)
                    staging_generation = current_staging_config[
                        "consul-generation"
                    ]
                except Exception:
                    self.log.debug(
                        "update-check", result="inconsistent", action="purge"
                    )
                    self.config_file_staging.unlink(missing_ok=True)
                    return False
                else:
                    if staging_generation <= self.consul_generation:
                        # Stop right here, do not write a new config if the
                        # existing one is newer (or as new) already.
                        self.log.debug(
                            "update-check",
                            result="stale-update",
                            action="ignore",
                            update=staging_generation,
                            current=self.consul_generation,
                        )
                        # The old staging file needs to stay around so that
                        # the consul writer knows whether to launch an ensure
                        # agent or not.
                        return False
                self.log.debug(
                    "update-check",
                    result="update-available",
                    action="update",
                    update=staging_generation,
                    current=self.consul_generation,
                )
            return True
        finally:
            fcntl.flock(staging_lock, fcntl.LOCK_UN)
            os.close(staging_lock)
            self.log.debug(
                "release-staging-lock",
                target=self.config_file_staging,
                result="released",
            )

    def _update_from_enc(self):
        # This copy means we can't manipulate `self.cfg` to update ENC data,
        # which is OK. We did this at some point and we're adding computed
        # data to the `cfg` structure, that we do not want to accidentally
        # reflect back into the config file.
        self.cfg = copy.copy(self.enc["parameters"])
        self.cfg["name"] = self.enc["name"]
        self.cfg["root_size"] = self.cfg["disk"] * (1024**3)
        self.cfg["swap_size"] = swap_size(self.cfg["memory"])
        self.cfg["tmp_size"] = tmp_size(self.cfg["disk"])
        self.cfg["ceph_id"] = self.ceph_id
        self.cfg["cpu_model"] = self.cfg.get("cpu_model", "qemu64")
        self.cfg["binary_generation"] = self.binary_generation
        self.consul_generation = self.enc["consul-generation"]
        self.qemu = Qemu(self.cfg)
        self.ceph = Ceph(self.cfg, self.enc)
        self.contexts = [self.qemu, self.ceph]
        for attr in ["migration_ctl_address"]:
            setattr(self, attr, getattr(self, attr).format(**self.cfg))
        for candidate in [
            self.system_config_template,
            self.builtin_config_template,
        ]:
            if candidate.exists():
                self.vm_config_template = candidate
                break
        else:
            raise RuntimeError("Could not find Qemu config file template.")

    def __enter__(self):
        # Allow updating our config by exiting/entering after setting new ENC
        # data.
        if self.enc is None:
            return
        self._update_from_enc()
        for c in self.contexts:
            c.__enter__()

    def __exit__(self, exc_value, exc_type, exc_tb):
        for c in self.contexts:
            try:
                c.__exit__(exc_value, exc_type, exc_tb)
            except Exception:
                self.log.exception("leave-subsystems", exc_info=True)

    def ensure(self):
        # Run the config activation code at least once, but then only if there
        # are still config changes around. We do this because multiple updates
        # may be piling up and we want to catch up as quickly as possible but
        # we only stage the updates and don't want to spawn an agent for each
        # update of each VM. So if an agent is already running it checks again
        # after it did something and then keeps going. OTOH agents that are
        # spawned after staging a new config will give up immediately when the
        # notice another agent running.

        # The __exit__/__enter__ dance is to simplify handling this special
        # case where we have to handle this method differently as it keeps
        # entering/exiting while cycling through the config changes. This keeps
        # the API and unit tests a bit cleaner even though we enter/exit one
        # time too many.
        self.__exit__(None, None, None)
        try:
            first = True
            while self.has_new_config() or first:
                self.log.info(
                    "running-ensure", generation=self.consul_generation
                )
                first = False
                try:
                    locking_code = self.ensure_()
                    if locking_code == os.EX_TEMPFAIL:
                        # We didn't get the lock, so someone else is
                        # already around who will pick up the (additional)
                        # changed config later.
                        break
                except ConfigChanged:
                    # Well then. Let's try this again.
                    continue
        finally:
            self.__enter__()
        self.log.debug("changes-settled")

    # This needs to be non-blocking at least to allow triggering an `fc-qemu
    # ensure` after a VM has spontaneously exited in the `run_supervised` code
    # but maybe also due to other situations where we want to avoid
    # deadlocks.
    @locked(blocking=False)
    def ensure_(self):
        self.activate_new_config()
        with self:
            # Host assignment is a bit tricky: we decided to not interpret an
            # *empty* cfg['kvm_host'] as "should not be running here" for the
            # sake of not accidentally causing downtime.
            try:
                if not self.cfg["online"]:
                    # Wanted offline.
                    self.ensure_offline()

                elif self.cfg["kvm_host"]:
                    if self.cfg["kvm_host"] != self.this_host:
                        # Wanted online, but explicitly on a different host.
                        self.ensure_online_remote()
                    else:
                        # Wanted online, and it's OK if it's running here, but
                        # I'll only start it if it really is wanted here.
                        self.ensure_online_local()

                self.raise_if_inconsistent()
            except VMStateInconsistent:
                # Last-resort seat-belt to verify that we ended up in a
                # consistent state. Inconsistent states result in the VM being
                # forcefully terminated.
                self.log.error(
                    "inconsistent-state", action="destroy", exc_info=True
                )
                self.qemu.destroy(kill_supervisor=True)

    def ensure_offline(self):
        if self.qemu.is_running():
            self.log.info(
                "ensure-state", wanted="offline", found="online", action="stop"
            )
            self.stop()
        else:
            self.log.info(
                "ensure-state",
                wanted="offline",
                found="offline",
                action="none",
            )

    def ensure_online_remote(self):
        if not self.qemu.is_running():
            # This cleans up potential left-overs from a previous migration.
            self.ceph.stop()
            self.consul_deregister()
            self.cleanup()
            return

        migration_errors = self.outmigrate()
        if migration_errors:
            # Stop here, do not clean up. This is fine as we may not have
            # made contact with our destination host. We keep trying later.
            return

        # Ensure ongoing maintenance for a VM that was potentially outmigrated.
        # But where the outmigration may have left bits and pieces.
        self.ceph.stop()
        self.consul_deregister()
        self.cleanup()

    def ensure_online_local(self):
        # Ensure state
        agent_likely_ready = True
        if not self.qemu.is_running():
            current_host = self._requires_inmigrate_from()
            if current_host:
                self.log.info(
                    "ensure-state",
                    wanted="online",
                    found="offline",
                    action="inmigrate",
                    remote=current_host,
                )
                exitcode = self.inmigrate()
                if exitcode:
                    # This is suboptimal: I hate error returns,
                    # but the main method is also a command. If we did
                    # not succeed in migrating, then I also don't want the
                    # consul registration to happen.
                    return
            else:
                self.log.info(
                    "ensure-state",
                    wanted="online",
                    found="offline",
                    action="start",
                )
                self.start()
                agent_likely_ready = False
        else:
            self.log.info(
                "ensure-state", wanted="online", found="online", action=""
            )

        # Perform ongoing adjustments of the operational parameters of the
        # running VM.
        self.consul_register()
        self.ensure_online_disk_size()
        self.ensure_online_disk_throttle()
        self.ensure_watchdog()
        self.ceph.ensure()
        # Be aggressive/opportunistic about re-acquiring locks in case
        # they were taken away.
        self.ceph.lock()
        if agent_likely_ready:
            # This requires guest agent interaction and we should only
            # perform this when we haven't recently booted the machine to
            # reduce the time we're unnecessarily waiting for timeouts.
            self.ensure_thawed()
            self.mark_qemu_binary_generation()
            self.mark_qemu_guest_properties()

    def cleanup(self):
        """Removes various run and tmp files."""
        self.qemu.clean_run_files()
        for tmp in self.config_file.parent.glob(self.config_file.name + "?*"):
            tmp.unlink(missing_ok=True)

    def ensure_thawed(self):
        self.log.info("ensure-thawed", volume="root")
        try:
            self.qemu.thaw()
        except Exception as e:
            self.log.error("ensure-thawed-failed", reason=str(e))

    def mark_qemu_guest_properties(self):
        props = {
            "binary_generation": self.binary_generation,
            "cpu_model": self.cfg["cpu_model"],
            "rbd_pool": self.cfg["rbd_pool"],
        }
        self.log.info(
            "mark-qemu-guest-properties",
            properties=props,
        )
        try:
            self.qemu.write_file(
                "/run/qemu-guest-properties-current",
                (json.dumps(props)).encode("utf-8"),
            )
        except Exception as e:
            self.log.error("mark-qemu-guest-properties", reason=str(e))

    def mark_qemu_binary_generation(self):
        self.log.info(
            "mark-qemu-binary-generation", generation=self.binary_generation
        )
        try:
            self.qemu.write_file(
                "/run/qemu-binary-generation-current",
                (str(self.binary_generation) + "\n").encode("ascii"),
            )
        except Exception as e:
            self.log.error("mark-qemu-binary-generation", reason=str(e))

    def ensure_online_disk_size(self):
        """Trigger block resize action for the root disk."""
        target_size = self.cfg["root_size"]
        current_size = self.ceph.volumes["root"].size
        if current_size >= target_size:
            self.log.info(
                "check-disk-size",
                wanted=target_size,
                found=current_size,
                action="none",
            )
            return
        self.log.info(
            "check-disk-size",
            wanted=target_size,
            found=current_size,
            action="resize",
        )
        self.qemu.resize_root(target_size)

    def ensure_online_disk_throttle(self):
        """Ensure throttling settings."""
        target = self.cfg.get(
            "iops", self.qemu.throttle_by_pool.get(self.cfg["rbd_pool"], 250)
        )
        devices = self.qemu.block_info()
        for device in list(devices.values()):
            current = device["inserted"]["iops"]
            if current != target:
                self.log.info(
                    "ensure-throttle",
                    device=device["device"],
                    target_iops=target,
                    current_iops=current,
                    action="throttle",
                )
                self.qemu.block_io_throttle(device["device"], target)
            else:
                self.log.info(
                    "ensure-throttle",
                    device=device["device"],
                    target_iops=target,
                    current_iops=current,
                    action="none",
                )

    def ensure_watchdog(self, action="none"):
        """Ensure watchdog settings."""
        self.log.info("ensure-watchdog", action=action)
        self.qemu.watchdog_action(action)

    @property
    def svc_name(self):
        """Consul service name."""
        return "qemu-{}".format(self.name)

    def consul_register(self):
        """Register running VM with Consul."""
        self.log.debug("consul-register")
        self.consul.agent.service.register(
            self.svc_name,
            address=self.this_host,
            check=consulate.models.agent.Check(
                name="qemu-process",
                args=[
                    "/bin/sh",
                    "-c",
                    "test -e /proc/$(< /run/qemu.{}.pid )/mem || exit 2".format(
                        self.name
                    ),
                ],
                interval="5s",
            ),
        )

    def consul_deregister(self):
        """De-register non-running VM with Consul."""
        try:
            if self.svc_name not in self.consul.agent.services():
                return
            self.log.info("consul-deregister")
            self.consul.agent.service.deregister("qemu-{}".format(self.name))
        except requests.exceptions.ConnectionError:
            pass
        except Exception:
            self.log.exception("consul-deregister-failed", exc_info=True)

    @locked()
    @running(False)
    def start(self):
        self.ceph.start()
        self.generate_config()
        self.qemu.start()
        self.consul_register()
        self.ensure_online_disk_throttle()
        self.ensure_watchdog()
        # We exit here without releasing the ceph lock in error cases
        # because the start may have failed because of an already running
        # process. Removing the lock in that case is dangerous. OTOH leaving
        # the lock is never dangerous. Lets go with that.

    @contextlib.contextmanager
    def frozen_vm(self):
        """Ensure a VM has a non-changing filesystem, that is clean enough
        to be mounted with a straight-forward journal replay.

        This is implemented using the 'fs-freeze' API through the Qemu guest
        agent.

        As we can't indicate the cleanliness of a non-running
        (potentially crashed) VM we do only signal a successful freeze iff
        the freeze happened and succeeded.

        """
        frozen = False
        has_exception = False
        try:
            if self.qemu.is_running():
                self.log.info("freeze", volume="root")
                try:
                    self.qemu.freeze()
                    frozen = True
                except Exception as e:
                    self.log.error(
                        "freeze-failed",
                        reason=str(e),
                        action="continue",
                        machine=self.name,
                    )
            yield frozen
        except Exception:
            # requirement of the contextlib.contextmanager: otherwise
            # the finally clause will trap the exception here and it
            # will be missed on the outside
            has_exception = True
        finally:
            if frozen:
                # If we didn't freeze before then we can't be sure about
                # reliability of communicating with the agent. There are
                # other measures in place (e.g. during scrubbing) that will
                # try to unfreeze later.
                self.ensure_thawed()
            if has_exception:
                raise

    @locked()
    @running(True)
    def snapshot(self, snapshot, keep=0):
        """Guarantees a _consistent_ snapshot to be created.

        If we can't properly freeze the VM then whoever needs a (consistent)
        snapshot needs to figure out whether to go forward with an
        inconsistent snapshot.

        """
        if keep:
            until = util.today() + datetime.timedelta(days=keep)
            snapshot = snapshot + "-keep-until-" + until.strftime("%Y%m%d")
        if snapshot in [
            x.snapname for x in self.ceph.volumes["root"].snapshots
        ]:
            self.log.info("snapshot-exists", snapshot=snapshot)
            return
        self.log.info("snapshot-create", name=snapshot)
        with self.frozen_vm() as frozen:
            if frozen:
                self.ceph.volumes["root"].snapshots.create(snapshot)
            else:
                self.log.error("snapshot-ignore", reason="not frozen")
                raise RuntimeError("VM not frozen, not making snapshot.")

    # This must be locked because we're going to use the
    # QMP socket and that only supports talking to one person at a time.
    # Alternatively we'd had to connect/disconnect and do weird things
    # for every single command ...
    @locked()
    def status(self):
        """Determine status of the VM."""
        try:
            if self.qemu.is_running():
                status = 0
                self.log.info("vm-status", result="online")
                for device in list(self.qemu.block_info().values()):
                    self.log.info(
                        "disk-throttle",
                        device=device["device"],
                        iops=device["inserted"]["iops"],
                    )
            else:
                status = 1
                self.log.info("vm-status", result="offline")
        except VMStateInconsistent:
            self.log.exception("vm-status", result="inconsistent")
        self.ceph.status()
        consul = locate_live_service(self.consul, "qemu-" + self.name)
        if consul:
            self.log.info(
                "consul", service=consul["Service"], address=consul["Address"]
            )
        else:
            self.log.info("consul", service="<not registered>")

        return status

    def telnet(self):
        """Open telnet connection to the VM monitor."""
        self.log.info("connect-via-telnet")
        telnet = distutils.spawn.find_executable("telnet")
        os.execv(telnet, ("telnet", "localhost", str(self.qemu.monitor_port)))

    @locked()
    @running(True)
    def stop(self):
        timeout = TimeOut(self.timeout_graceful, interval=3)
        self.log.info("graceful-shutdown")
        try:
            self.qemu.graceful_shutdown()
        except (socket.error, RuntimeError):
            pass
        while timeout.tick():
            self.log.debug("checking-offline", remaining=timeout.remaining)
            if not self.qemu.is_running():
                self.log.info("vm-offline")
                self.ceph.stop()
                self.consul_deregister()
                self.cleanup()
                self.log.info("graceful-shutdown-completed")
                break
        else:
            self.log.warn("graceful-shutdown-failed", reason="timeout")
            self.kill()

    @locked()
    def restart(self):
        self.log.info("restart-vm")
        self.stop()
        self.start()

    @locked()
    @running(True)
    def kill(self):
        self.log.info("kill-vm")
        timeout = TimeOut(15, interval=1, raise_on_timeout=True)
        self.qemu.destroy(kill_supervisor=True)
        while timeout.tick():
            if not self.qemu.is_running():
                self.log.info("killed-vm")
                self.ceph.stop()
                self.consul_deregister()
                self.cleanup()
                break
        else:
            self.log.warning("kill-vm-failed", note="Check lock consistency.")

    def _requires_inmigrate_from(self):
        """Check whether an inmigration makes sense.

        This makes sense if the VM isn't running locally and (Consul knows
        about a running VM or the VM is locked remotely).

        Returns the name of the remote host the VM should be migrated from
        or None if inmigration doesn't make sense.

        """
        existing = locate_live_service(self.consul, "qemu-" + self.name)

        if existing and existing["Address"] != self.this_host:
            # Consul knows about a running VM. Lets try a migration.
            return existing["Address"]

        if self.ceph.is_unlocked():
            # Consul doesn't know about a running VM and no volume is locked.
            # It doesn't make sense to live migrate this VM.
            return None

        if self.ceph.locked_by_me():
            # Consul doesn't know about a running VM and the volume is
            # locked by me, so it doesn't make sense to live migrate the VM.
            return None

        # The VM seems to be locked somewhere else, try to migrate it from
        # there.
        return self.ceph.locked_by()

    @locked()
    @running(False)
    def inmigrate(self):
        self.log.info("inmigrate")
        server = IncomingServer(self)
        exitcode = server.run()
        if not exitcode:
            self.consul_register()
        self.log.info("inmigrate-finished", exitcode=exitcode)
        return exitcode

    @locked()
    @running(True)
    def outmigrate(self):
        self.log.info("outmigrate")
        # re-register in case services got lost during Consul restart
        self.consul_register()
        client = Outgoing(self)
        exitcode = client()
        if not exitcode:
            # XXX I think this can lead to inconsistent behaviour
            # if the local VM is destroyed during migration?
            self.consul_deregister()
        self.log.info("outmigrate-finished", exitcode=exitcode)
        return exitcode

    @locked()
    def lock(self):
        self.log.info("assume-all-locks")
        for vol in self.ceph.volumes:
            vol.lock()

    @locked()
    @running(False)
    def unlock(self):
        self.log.info("release-all-locks")
        self.ceph.stop()

    @running(False)
    def force_unlock(self):
        self.log.info("break-all-locks")
        self.ceph.force_unlock()

    # Helper methods
    def raise_if_inconsistent(self):
        """Raise an VMStateInconsistent error if the VM state is not
        consistent.

        Either all of the following or none of the following must be true:

        - The process belonging to the PID we know exists.
        - The QMP monitor reliably tells us that the Qemu status of the
          VM/CPUs is "running"
        - We have locked the volumes in Ceph.

        If they are not the same, then the state is considered inconsistent
        and this method will return False.

        """
        state = VMStateInconsistent()
        state.qemu = self.qemu.is_running()
        state.proc = bool(self.qemu.proc())
        state.ceph_lock = self.ceph.locked_by_me()
        self.log.debug(
            "check-state-consistency",
            is_consistent=state.is_consistent(),
            qemu=state.qemu,
            proc=state.proc,
            ceph_lock=state.ceph_lock,
        )
        if not state.is_consistent():
            raise state

    # CAREFUL: changing anything in this config files will require maintenance
    # w/ reboot of all VMs in the infrastructure. Need to increase the
    # binary generation for that, though.
    def generate_config(self):
        """Generate a new Qemu config (and options) for a freshly
        starting VM.

        The configs are intended to be non-host-specific.

        This two-step behaviour is needed to support migrating VMs
        and keeping arguments consistent while allowing to localize arguments
        to the host running or receiving the VM.

        """
        self.log.debug("generate-config")
        # WARNING: those names embedded in double curly braces must stay
        # compatible between older and newer fc.qemu versions to allow
        # upgrade/downgrade live migrations!
        self.qemu.args = [
            "-nodefaults",
            "-only-migratable",
            "-cpu {cpu_model},enforce",
            # Watch out: kvm.name is used for sanity checking critical actions.
            "-name {name},process=kvm.{name}",
            "-chroot {{chroot}}",
            "-runas nobody",
            "-serial file:{serial_file}",
            "-display vnc={{vnc}}",
            "-pidfile {{pidfile}}",
            "-vga std",
            # We use this '-m' flag to find what a running VM is actually
            # using at the moment. If this flag is changed then that code must
            # be adapted as well. This is used in incoming.py and qemu.py.
            "-m {memory}",
            "-readconfig {{configfile}}",
        ]
        self.qemu.args = [a.format(**self.cfg) for a in self.qemu.args]

        vhost = '  vhost = "on"' if self.vhost else ""

        netconfig = []
        for net, net_config in sorted(self.cfg["interfaces"].items()):
            ifname = "t{}{}".format(net, self.cfg["id"])
            netconfig.append(
                """
[device]
  driver = "virtio-net-pci"
  netdev = "{ifname}"
  mac = "{mac}"

[netdev "{ifname}"]
  type = "tap"
  ifname = "{ifname}"
  script = "/etc/kvm/kvm-ifup"
  downscript = "/etc/kvm/kvm-ifdown"
{vhost}
""".format(
                    ifname=ifname, mac=net_config["mac"], vhost=vhost
                )
            )

        with self.vm_config_template.open() as f:
            tpl = f.read()
        accelerator = (
            '  accel = "{}"'.format(self.accelerator)
            if self.accelerator
            else ""
        )
        machine_type = detect_current_machine_type(self.machine_type)
        self.qemu.config = tpl.format(
            accelerator=accelerator,
            machine_type=machine_type,
            network="".join(netconfig),
            ceph=self.ceph,
            **self.cfg,
        )
