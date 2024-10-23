"""Low-level interface to Qemu commands."""

import datetime
import fcntl
import os
import socket
import subprocess
import time
from codecs import encode
from pathlib import Path

import psutil
import yaml

from ..exc import QemuNotRunning, VMStateInconsistent
from ..sysconfig import sysconfig
from ..timeout import TimeOut
from ..util import ControlledRuntimeException, log
from .guestagent import ClientError, GuestAgent
from .qmp import QEMUMonitorProtocol as Qmp
from .qmp import QMPConnectError

# Freeze requests may take a _long_ _long_ time and the default
# timeout of 3 seconds will cause everything to explode when
# the guest takes too long. We've seen 16 seconds as a regular
# period in some busy and large machines. I'm being _very_
# generous using a 5 minute timeout here. We've seen it get stuck longer
# than 2 minutes and the agent is very stubborn in those cases and really
# doesn't like if the client goes away ...
# This is a global variable so we can instrument it during testing.
FREEZE_TIMEOUT = 300


class InvalidMigrationStatus(Exception):
    pass


def detect_current_machine_type(
    prefix: str, encoding="ascii", errors="replace"
):
    """Given a machine type prefix, e.g. 'pc-i440fx-' return the newest
    current machine on the available Qemu system.

    Newest in this case means the first item in the list as given by Qemu.
    """
    result = subprocess.check_output(
        [Qemu.executable, "-machine", "help"], encoding=encoding, errors=errors
    )
    for line in result.splitlines():
        if line.startswith(prefix):
            return line.split()[0]
    raise KeyError("No machine type found for prefix `{}`".format(prefix))


def locked_global(f):
    LOCK = Path("run/fc-qemu.lock")

    # This is thread-safe *AS LONG* as every thread uses a separate instance
    # of the agent. Using multiple file descriptors will guarantee that the
    # lock can only be held once even within a single process.
    def locked(self, *args, **kw):
        lock = self.prefix / LOCK
        self.log.debug("acquire-global-lock", target=lock)
        if not self._global_lock_fd:
            if not lock.exists():
                lock.touch()
            self._global_lock_fd = os.open(lock, os.O_RDONLY)
        self.log.debug("global-lock-acquire", target=lock, result="locked")

        fcntl.flock(self._global_lock_fd, fcntl.LOCK_EX)
        self._global_lock_count += 1
        self.log.debug(
            "global-lock-status", target=lock, count=self._global_lock_count
        )
        try:
            return f(self, *args, **kw)
        finally:
            self._global_lock_count -= 1
            self.log.debug(
                "global-lock-status",
                target=lock,
                count=self._global_lock_count,
            )
            if self._global_lock_count == 0:
                self.log.debug("global-lock-release", target=lock)
                fcntl.flock(self._global_lock_fd, fcntl.LOCK_UN)
                self.log.debug("global-lock-release", result="unlocked")

    return locked


class Qemu(object):
    prefix = Path("/")
    executable = "qemu-system-x86_64"

    # Attributes on this class can be overriden (in a controlled fashion
    # from the sysconfig module. See this class' __init__. The defaults
    # are here to support testing.

    cfg = None
    require_kvm = True
    migration_address = None
    max_downtime = 1.0  # seconds
    # 0.8 * 10 Gbit/s in bytes/s
    migration_bandwidth = int(0.8 * 10 * 10**9 / 8)

    guestagent_timeout = 3.0
    # QMP runs in the main thread and can block. Our original 15s timeout
    # is definitely too short. Many discussions mention that 5 minutes have
    # stabilized the situation even under adverse situations.
    qmp_timeout = 5 * 60
    thaw_retry_timeout = 2
    vm_max_total_memory = 0  # MiB: maximum amount of booked memory (-m)
    # on this host
    vm_expected_overhead = 0  # MiB: expected amount of PSS overhead per VM

    # The non-hosts-specific config configuration of this Qemu instance.
    args = ()
    config = ""

    # Host-specific qemu configuration
    chroot = Path("srv/vm/{name}")
    vnc = "localhost:1"

    MONITOR_OFFSET = 20000

    pid_file = Path("run/qemu.{name}.pid")
    config_file = Path("run/qemu.{name}.cfg")
    config_file_in = Path("run/qemu.{name}.cfg.in")
    arg_file = Path("run/qemu.{name}.args")
    arg_file_in = Path("run/qemu.{name}.args.in")
    qmp_socket = Path("run/qemu.{name}.qmp.sock")
    serial_file = Path("var/log/vm/{name}.log")

    _global_lock_fd = None
    _global_lock_count = 0

    migration_lock_file = Path("run/qemu.migration.lock")
    _migration_lock_fd = None

    def __init__(self, vm_cfg):
        # Update configuration values from system or test config.
        self.__dict__.update(sysconfig.qemu)

        self.cfg = vm_cfg
        # expand template keywords in configuration variables
        for f in ["migration_address"]:
            setattr(self, f, getattr(self, f).format(**vm_cfg))

        # expand template keywords in paths and apply prefix
        for f in [
            "pid_file",
            "config_file",
            "config_file_in",
            "arg_file",
            "arg_file_in",
            "qmp_socket",
            "migration_lock_file",
            "chroot",
            "serial_file",
        ]:
            expanded = self.prefix / str(getattr(self, f)).format(**vm_cfg)
            setattr(self, f, expanded)
            vm_cfg[f] = expanded

        # We are running qemu with chroot which causes us to not be able to
        # resolve names. :( See #13837.
        a = self.migration_address.split(":")
        if a[0] == "tcp":
            a[1] = socket.gethostbyname(a[1])
        self.migration_address = ":".join(a)
        self.name = self.cfg["name"]
        self.monitor_port = self.cfg["id"] + self.MONITOR_OFFSET
        self.guestagent = GuestAgent(self.name, timeout=self.guestagent_timeout)

        self.log = log.bind(machine=self.name, subsystem="qemu")

    __qmp = None

    @property
    def qmp(self):
        if self.__qmp is None:
            qmp = Qmp(str(self.qmp_socket), self.log)
            qmp.settimeout(self.qmp_timeout)
            try:
                qmp.connect()
            except socket.error:
                # We do not log this as this does happen quite regularly and
                # is usually fine as the VM wasn't started (yet).
                pass
            else:
                self.__qmp = qmp
        return self.__qmp

    def __enter__(self):
        pass

    def __exit__(self, exc_value, exc_type, exc_tb):
        self.guestagent.disconnect()
        if self.__qmp:
            self.__qmp.close()
            self.__qmp = None

    def proc(self):
        """Qemu processes as psutil.Process object.

        Returns None if the PID file does not exist or the process is
        not running.
        """
        try:
            with self.pid_file.open() as p:
                # pid file may contain trailing lines with garbage
                for line in p:
                    proc = psutil.Process(int(line))
                    marker = "{name},process=kvm.{name}".format(name=self.name)
                    # Do not use proc.name() here - it's only 16 bytes ...
                    if marker not in proc.cmdline():
                        break
                    return proc
        except (IOError, OSError, ValueError, psutil.NoSuchProcess):
            pass

    def prepare_log(self):
        log_dir = self.prefix / Path("/var/log/vm")
        if not log_dir.is_dir():
            raise RuntimeError("Expected directory /var/log/vm to exist.")
        log_file = log_dir / f"{self.name}.log"
        if log_file.exists():
            alt_marker = datetime.datetime.now().isoformat()
            alternate = log_dir / f"{self.name}-{alt_marker}.log"
            log_file.rename(alternate)

    def _current_vms_booked_memory(self):
        """Determine the amount of booked memory (MiB) from the

        currently running VM processes.
        """
        total = 0
        for proc in psutil.process_iter():
            try:
                pinfo = proc.as_dict(attrs=["pid", "name", "cmdline"])
            except psutil.NoSuchProcess:
                continue

            if not pinfo["name"].startswith("kvm."):
                continue
            if not (
                pinfo["cmdline"] and pinfo["cmdline"][0] == "qemu-system-x86_64"
            ):
                continue
            try:
                m_flag = pinfo["cmdline"].index("-m")
                memory = int(pinfo["cmdline"][m_flag + 1])
            except (ValueError, KeyError):
                self.log.debug(
                    "unexpected-cmdline",
                    cmdline=format(" ".join(pinfo["cmdline"])),
                )
                raise ControlledRuntimeException(
                    "Can not determine used memory for {}".format(
                        " ".join(pinfo["cmdline"])
                    )
                )
            total += memory + self.vm_expected_overhead
        return total

    def _verify_memory(self):
        """Verify that we do not accidentally run more VMs than we can
        physically bear.

        This is a protection to avoid starting new VMs while some old VMs
        that the directory assumes have been migrated or stopped already
        are still running. This can cause severe performance penalties and may
        also kill VMs under some circumstances.

        Also, if VMs should exhibit extreme overhead, we protect against
        starting additional VMs even if our inventory says we should be
        able to run them.

        If no limit is configured then we start VMs based on actual
        availability only.

        """
        current_booked = self._current_vms_booked_memory()  # MiB
        required = self.cfg["memory"] + self.vm_expected_overhead  # MiB

        available_real = psutil.virtual_memory().available / (1024 * 1024)
        limit_booked = self.vm_max_total_memory
        available_bookable = limit_booked - current_booked

        if (limit_booked and available_bookable < required) or (
            available_real < required
        ):
            self.log.error(
                "insufficient-host-memory",
                bookable=available_bookable,
                available=available_real,
                required=required,
            )
            raise ControlledRuntimeException(
                "Insufficient bookable memory to start VM."
            )

        self.log.debug(
            "sufficient-host-memory",
            bookable=available_bookable,
            available_real=available_real,
            required=required,
        )

    # This lock protects checking the amount of available memory and actually
    # starting the VM. This ensures that no other process checks at the same
    # time and we end up using the free memory twice.
    @locked_global
    def _start(self, additional_args=()):
        if self.require_kvm and not Path("/dev/kvm").exists():
            self.log.error("missing-kvm-support")
            raise ControlledRuntimeException(
                "Refusing to start without /dev/kvm support."
            )

        self._verify_memory()

        self.prepare_config()
        self.prepare_log()
        try:
            args = list(self.local_args) + list(additional_args)
            # We do not daemonize any longer and even want this to happen
            # when migrating VMs.
            # We also force the internal stderr log, even though I haven't
            # seen this being useful, yet.
            args = [x for x in args if x != "-daemonize"]
            args = [x for x in args if not x.startswith("-D ")]
            args.append("-D /var/log/vm/{}.qemu.internal.log".format(self.name))

            cmd = "{} {}".format(self.executable, " ".join(args))
            self.log.info("start-qemu")
            self.log.debug(
                self.executable,
                local_args=self.local_args,
                additional_args=additional_args,
            )
            # We explicitly close all fds for the child to avoid inheriting fd
            # locks accidentally and indefinitely.
            qemu_log = "/var/log/vm/{}.supervisor.log".format(self.name)
            cmdline = ["supervised-qemu", cmd, self.name, qemu_log]
            self.log.debug("exec", cmd=" ".join(cmdline))
            p = subprocess.Popen(
                cmdline,
                close_fds=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="ascii",
                errors="replace",
            )
            stdout, stderr = p.communicate()
            # FIXME: prettier logging
            self.log.debug("supervised-qemu-stdout", output=stdout)
            self.log.debug("supervised-qemu-stderr", output=stderr)
            if p.returncode != 0:
                raise QemuNotRunning(p.returncode, stdout, stderr)
        except QemuNotRunning:
            # Did not start. Not running.
            self.log.exception("qemu-failed")
            raise

    def start(self):
        self._start()
        timeout = TimeOut(10, 0.25, raise_on_timeout=True)
        while timeout.tick():
            if self.is_running():
                return

    def freeze(self):
        try:
            # This request may take a _long_ _long_ time and the default
            # timeout of 3 seconds will cause everything to explode when
            # the guest takes too long. We've seen 16 seconds as a regular
            # period in some busy and large machines. So we increase this
            # to a lot more and also perform a gratuitous thaw in case
            # we error out.
            self.guestagent.cmd("guest-fsfreeze-freeze", timeout=FREEZE_TIMEOUT)
        except ClientError:
            self.log.debug("guest-fsfreeze-freeze-failed", exc_info=True)
            self.guestagent.cmd("guest-fsfreeze-thaw", fire_and_forget=True)
        assert self.guestagent.cmd("guest-fsfreeze-status") == "frozen"

    def thaw(self):
        try:
            self.guestagent.cmd("guest-fsfreeze-thaw")
            result = self.guestagent.cmd("guest-fsfreeze-status")
            if result != "thawed":
                raise RuntimeError("Unexpected thaw result: {}".format(result))
        except Exception:
            self.log.warning("guest-fsfreeze-thaw-failed", exc_info=True)
            raise

    def write_file(self, path, content: bytes):
        if not isinstance(content, bytes):
            raise TypeError("Expected bytes, got string.")
        try:
            handle = self.guestagent.cmd("guest-file-open", path=path, mode="w")
            self.guestagent.cmd(
                "guest-file-write",
                handle=handle,
                # The ASCII armour needs to be turned into text again, because the
                # JSON encoder doesn't handle bytes-like objects.
                **{"buf-b64": encode(content, "base64").decode("ascii")},
            )
            self.guestagent.cmd("guest-file-close", handle=handle)
        except ClientError:
            self.log.error("guest-write-file", exc_info=True)

    def inmigrate(self):
        self._start([f"-incoming {self.migration_address}"])

        timeout = TimeOut(30, 1, raise_on_timeout=True)
        while self.qmp is None:
            timeout.tick()

        status = self.qmp.command("query-status")
        assert not status["running"], status
        assert status["status"] == "inmigrate", status
        return self.migration_address

    def migrate(self, address):
        """Initiate actual (out-)migration"""
        self.log.debug("migrate")
        self.qmp.command(
            "migrate-set-capabilities",
            capabilities=[
                {"capability": "xbzrle", "state": False},
                {"capability": "auto-converge", "state": True},
            ],
        )
        self.qmp.command(
            "migrate-set-parameters",
            **{
                "compress-level": 0,
                "downtime-limit": int(self.max_downtime * 1000),  # ms
                "max-bandwidth": self.migration_bandwidth,
            },
        )
        self.qmp.command("migrate", uri=address)
        self.log.debug(
            "migrate-parameters",
            **self.qmp.command("query-migrate-parameters"),
        )

    def poll_migration_status(self, timeout=30):
        """Monitor ongoing migration.

        Every few seconds, the migration status is queried from the Qemu
        monitor. It is yielded to the calling context to provide a hook
        for communicating status updates.

        """
        timeout = TimeOut(timeout, 1, raise_on_timeout=True)
        while timeout.tick():
            if timeout.interval < 10:
                timeout.interval *= 1.4142
            info = self.qmp.command("query-migrate")
            yield info

            if info["status"] == "setup":
                pass
            elif info["status"] == "completed":
                break
            elif info["status"] == "active":
                # This didn't work out of the box on our 2.5, so I'll leave
                # this out for now. I think it's due to the need for the
                # userfaultd that needs to be installed on the host.
                # if info['ram']['transferred'] > info['ram']['total']:
                #     self.log.info('migrate-start-postcopy')
                #     self.qmp.command('migrate-start-postcopy')
                pass
            else:
                raise InvalidMigrationStatus(info)
            timeout.cutoff += 30

    def process_exists(self):
        proc = self.proc()
        if proc is None:
            return False
        return proc.is_running()

    def is_running(self):
        # This method must be very reliable. It is perfectly OK to error
        # out in the case of inconsistencies. But a "true" must mean:
        # we have a working Qemu instance here. And a "false" must mean:
        # there is no reason to think that any remainder of a Qemu process is
        # still running

        timeout = TimeOut(10, raise_on_timeout=False)
        while timeout.tick():
            # Try to find a stable result within a few seconds - ignore
            # unstable results in between. Qemu might just be starting
            # and the process already there but QMP not, or vice versa.

            # a) is there a process?
            expected_process_exists = self.process_exists()

            # b) is the monitor port around reliably? Let's assume it does.
            qmp_available = self.qmp

            # c) is the monitor available and talks to us?
            monitor_says_running = False
            status = {}

            if qmp_available:
                try:
                    status = self.qmp.command("query-status")
                except (QMPConnectError, socket.error):
                    # Force a reconnect in the next iteration.
                    self.__qmp.close()
                    self.__qmp = None
                    qmp_available = False
                    monitor_says_running = False
                else:
                    monitor_says_running = status["running"]

            if (
                expected_process_exists
                and qmp_available
                and monitor_says_running
            ):
                return True

            if not expected_process_exists and not qmp_available:
                return False

        # The timeout passed and we were not able to determine a consistent
        # result. :/
        raise VMStateInconsistent(
            "Can not determine whether Qemu is running. "
            "Process exists: {}, QMP socket reliable: {}, "
            "Status is running: {}".format(
                expected_process_exists, qmp_available, monitor_says_running
            ),
            status,
        )

    def rescue(self):
        """Recover from potentially inconsistent state.

        If the VM is running and we own all locks, then everything is fine.

        If the VM is running and we do not own the locks, then try to acquire
        them or bail out.

        Returns True if we were able to rescue the VM.
        Returns False if the rescue attempt failed and the VM is stopped now.

        """
        status = self.qmp.command("query-status")
        assert status["running"]
        for image in set(self.locks.available) - set(self.locks.held):
            try:
                self.acquire_lock(image)
            except Exception:
                self.log.debug("acquire-lock-failed", exc_info=True)
        self.assert_locks()

    def graceful_shutdown(self):
        if not self.qmp:
            return
        self.qmp.command(
            "send-key",
            keys=[
                {"type": "qcode", "data": "ctrl"},
                {"type": "qcode", "data": "alt"},
                {"type": "qcode", "data": "delete"},
            ],
        )

    def destroy(self, kill_supervisor=False):
        # We use this destroy command in "fire-and-forget"-style because
        # sometimes the init script will complain even if we achieve what
        # we want: that the VM isn't running any longer. We check this
        # by contacting the monitor instead.
        p = self.proc()
        if not p:
            return

        # Check whether the parent is the supervising process.
        # Kill that one first so we avoid immediate restarts.
        if kill_supervisor:
            parent = p.parent()
            if "supervised-qemu-wrapped" in parent.cmdline()[1]:
                # Do not raise on timeout so we get a chance to actually kill
                # the VM even if killing the supervisor fails.
                timeout = TimeOut(100, interval=2, raise_on_timeout=False)
                attempt = 0
                while parent.is_running() and timeout.tick():
                    attempt += 1
                    self.log.debug(
                        "vm-destroy-kill-supervisor", attempt=attempt
                    )
                    try:
                        parent.terminate()
                    except psutil.NoSuchProcess:
                        break

        timeout = TimeOut(100, interval=2, raise_on_timeout=True)
        attempt = 0
        while p.is_running() and timeout.tick():
            attempt += 1
            self.log.debug("vm-destroy-kill-vm", attempt=attempt)
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                break

    def resize_root(self, size):
        self.qmp.command("block_resize", device="virtio0", size=size)

    def block_info(self):
        devices = {}
        for device in self.qmp.command("query-block"):
            devices[device["device"]] = device
        return devices

    def block_io_throttle(self, device, iops):
        self.qmp.command(
            "block_set_io_throttle",
            device=device,
            iops=iops,
            iops_rd=0,
            iops_wr=0,
            bps=0,
            bps_wr=0,
            bps_rd=0,
        )

    def watchdog_action(self, action):
        self.qmp.command(
            "human-monitor-command",
            **{"command-line": "watchdog_action action={}".format(action)},
        )

    def clean_run_files(self):
        runfiles = list(
            (self.prefix / "run").glob(f"qemu.{self.cfg['name']}.*")
        )
        if not runfiles:
            return
        self.log.debug("clean-run-files")
        for runfile in runfiles:
            if runfile.suffix == ".lock":
                # Never, ever, remove lock files. Those should be on
                # partitions that get cleaned out during reboot, but
                # never otherwise.
                continue
            runfile.unlink(missing_ok=True)

    def prepare_config(self):
        if not self.chroot.exists():
            self.chroot.mkdir(parents=True, exist_ok=True)

        def format(s):
            # These names must stay compatible between
            # fc.qemu versions so that VMs can migrate between
            # older and newer versions freely.
            return s.format(
                monitor_port=self.monitor_port,
                vnc=self.vnc.format(**self.cfg),
                pidfile=self.cfg["pid_file"],
                configfile=self.cfg["config_file"],
                **self.cfg,
            )

        self.local_args = [format(a) for a in self.args]
        self.local_config = format(self.config)

        with self.config_file_in.open("w") as f:
            f.write(self.config)

        with self.config_file.open("w") as f:
            f.write(self.local_config)

        with self.arg_file_in.open("w") as f:
            yaml.safe_dump(self.args, f)

        # Qemu tends to overwrite the pid file incompletely -> truncate
        self.pid_file.open("w").close()

    def get_running_config(self):
        """Return the host-independent version of the current running
        config."""
        with self.arg_file_in.open() as a:
            args = yaml.safe_load(a.read())
        with self.config_file_in.open() as c:
            config = c.read()
        return args, config

    def acquire_migration_lock(self):
        assert not self._migration_lock_fd
        open(self.migration_lock_file, "a+").close()
        self._migration_lock_fd = os.open(self.migration_lock_file, os.O_RDONLY)
        try:
            fcntl.flock(self._migration_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.log.debug("acquire-migration-lock", result="success")
            return True
        except Exception as e:
            if isinstance(e, IOError):
                self.log.debug(
                    "acquire-migration-lock",
                    result="failure",
                    reason="competing lock",
                )
            else:
                self.log.exception(
                    "acquire-migration-lock", result="failure", exc_info=True
                )
            os.close(self._migration_lock_fd)
            self._migration_lock_fd = None
            return False

    def release_migration_lock(self):
        assert self._migration_lock_fd
        fcntl.flock(self._migration_lock_fd, fcntl.LOCK_UN)
        os.close(self._migration_lock_fd)
        self._migration_lock_fd = None
