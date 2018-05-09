"""Low-level interface to Qemu commands."""

from ..exc import QemuNotRunning, VMStateInconsistent
from ..sysconfig import sysconfig
from ..timeout import TimeOut
from ..util import log, ControlledRuntimeException
from .guestagent import GuestAgent, ClientError
from .qmp import QEMUMonitorProtocol as Qmp, QMPConnectError
import datetime
import fcntl
import glob
import os.path
import psutil
import socket
import subprocess
import time
import yaml


class InvalidMigrationStatus(Exception):
    pass


def detect_current_machine_type(prefix):
    """Given a machine type prefix, e.g. 'pc-i440fx-' return the newest
    current machine on the available Qemu system.

    Newest in this case means the first item in the list as given by Qemu.
    """
    result = subprocess.check_output(
        ['qemu-system-x86_64', '-machine', 'help'])
    for line in result.splitlines():
        if line.startswith(prefix):
            return line.split()[0]
    raise KeyError("No machine type found for prefix `{}`".format(prefix))


def locked_global(f):
    LOCK = '/run/fc-qemu.lock'
    # This is thread-safe *AS LONG* as every thread uses a separate instance
    # of the agent. Using multiple file descriptors will guarantee that the
    # lock can only be held once even within a single process.
    def locked(self, *args, **kw):
        self.log.debug('acquire-global-lock', target=LOCK)
        if not self._global_lock_fd:
            if not os.path.exists(LOCK):
                open(LOCK, 'a+').close()
            self._global_lock_fd = os.open(LOCK, os.O_RDONLY)
        self.log.debug('global-lock-acquire', target=LOCK, result='locked')

        fcntl.flock(self._global_lock_fd, fcntl.LOCK_EX)
        self._global_lock_count += 1
        self.log.debug('global-lock-status', target=LOCK,
                       count=self._global_lock_count)
        try:
            return f(self, *args, **kw)
        finally:
            self._global_lock_count -= 1
            self.log.debug('global-lock-status', target=LOCK,
                           count=self._global_lock_count)
            if self._global_lock_count == 0:
                self.log.debug('global-lock-release', target=LOCK)
                fcntl.flock(self._global_lock_fd, fcntl.LOCK_UN)
                self.log.debug('global-lock-release', result='unlocked')
    return locked


class Qemu(object):

    executable = 'qemu-system-x86_64'

    # Attributes on this class can be overriden (in a controlled fashion
    # from the sysconfig module. See this class' __init__. The defaults
    # are here to support testing.

    cfg = None
    require_kvm = True
    migration_address = None
    max_downtime = 1.0
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
    config = ''

    # Host-specific qemu configuration
    chroot = '/srv/vm/{name}'
    vnc = 'localhost:1'

    MONITOR_OFFSET = 20000

    pidfile = '/run/qemu.{name}.pid'
    configfile = '/run/qemu.{name}.cfg'
    argfile = '/run/qemu.{name}.args'
    qmp_socket = '/run/qemu.{name}.qmp.sock'

    _global_lock_fd = None
    _global_lock_count = 0

    migration_lockfile = '/run/qemu.migration.lock'
    _migration_lock_fd = None

    def __init__(self, vm_cfg):
        # Update configuration values from system or test config.
        self.__dict__.update(sysconfig.qemu)

        self.cfg = vm_cfg
        # expand template keywords in configuration variables
        for f in ['pidfile', 'configfile', 'argfile', 'migration_address',
                  'qmp_socket']:
            setattr(self, f, getattr(self, f).format(**vm_cfg))

        # prepare qemu-specific config settings
        self.qemu_cfg = self.cfg.get('qemu', {})

        # The default if nothing is set is to enable "writeback" for backwards
        # compatability: before introducing this option everything used
        # writeback.
        if self.qemu_cfg.get('write_back_cache', True):
            self.disk_cache_mode = "writeback"
        else:
            self.disk_cache_mode = "none"

        # We are running qemu with chroot which causes us to not be able to
        # resolve names. :( See #13837.
        a = self.migration_address.split(':')
        if a[0] == 'tcp':
            a[1] = socket.gethostbyname(a[1])
        self.migration_address = ':'.join(a)
        self.name = self.cfg['name']
        self.monitor_port = self.cfg['id'] + self.MONITOR_OFFSET
        self.guestagent = GuestAgent(
            self.name, timeout=self.guestagent_timeout)

        self.log = log.bind(machine=self.name, subsystem='qemu')

    __qmp = None

    @property
    def qmp(self):
        if self.__qmp is None:
            qmp = Qmp(self.qmp_socket, self.log)
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
        if self.__qmp:
            self.__qmp.close()

    def proc(self):
        """Qemu processes as psutil.Process object.

        Returns None if the PID file does not exist or the process is
        not running.
        """
        try:
            with open(self.pidfile) as p:
                # pid file may contain trailing lines with garbage
                for line in p:
                    return psutil.Process(int(line))
        except (IOError, OSError, ValueError, psutil.NoSuchProcess):
            pass

    def prepare_log(self):
        if not os.path.exists('/var/log/vm'):
            os.makedirs('/var/log/vm')
        logfile = '/var/log/vm/{}.log'.format(self.name)
        alternate = '/var/log/vm/{}-{}.log'.format(
            self.name, datetime.datetime.now().isoformat())
        if os.path.exists(logfile):
            os.rename(logfile, alternate)

    def _current_vms_booked_memory(self):
        """Determine the amount of booked memory (MiB) from the currently running VM processes.

        """
        total = 0
        for proc in psutil.process_iter():
            try:
                pinfo = proc.as_dict(attrs=['pid', 'name', 'cmdline'])
            except psutil.NoSuchProcess:
                continue

            if not pinfo['name'].startswith('kvm.'):
                continue
            if not (pinfo['cmdline'] and
                    pinfo['cmdline'][0] == 'qemu-system-x86_64'):
                continue
            try:
                m_flag = pinfo['cmdline'].index('-m')
                memory = int(pinfo['cmdline'][m_flag + 1])
            except (ValueError, KeyError):
                self.log.debug('unexpected-cmdline',
                               cmdline=format(' '.join(pinfo['cmdline'])))
                raise ControlledRuntimeException(
                    "Can not determine used memory for {}".
                    format(' '.join(pinfo['cmdline'])))
            total += memory + self.vm_expected_overhead
        return total

    def _verify_memory(self):
        """Verify that we do not accidentally run more VMs than we can
        physically bear.

        This is a protection to avoid starting new VMs while some old VMs
        that the directory assumes have been migrated or stopped already
        are still running. This can cause severe performance penalties and may
        also kill VMs under some circumstances.

        Also, if VMs should exhibit extreme overhead, we protect against starting additional VMs even if our inventory says we should be
        able to run them.

        If no limit is configured then we start VMs based on actual availability only.

        """
        current_booked = self._current_vms_booked_memory()  # MiB
        required = self.cfg['memory'] + self.vm_expected_overhead # MiB

        available_real = psutil.virtual_memory().available / (1024 * 1024)
        limit_booked = self.vm_max_total_memory
        available_bookable =  limit_booked - current_booked

        if ((limit_booked and available_bookable < required) or
            (available_real < required)):
            self.log.error('insufficient-host-memory',
                           bookable=available_bookable,
                           available=available_real,
                           required=required)
            raise ControlledRuntimeException(
                'Insufficient bookable memory to start VM.')

        self.log.debug('sufficient-host-memory',
                       bookable=available_bookable,
                       available_real=available_real,
                       required=required)

    # This lock protects checking the amount of available memory and actually
    # starting the VM. This ensure that no other process checks at the same
    # time and we end up using the free memory twice.
    @locked_global
    def _start(self, additional_args=()):
        if self.require_kvm and not os.path.exists('/dev/kvm'):
            self.log.error('missing-kvm-support')
            raise ControlledRuntimeException(
                'Refusing to start without /dev/kvm support.')

        self._verify_memory()

        self.prepare_config()
        self.prepare_log()
        try:
            cmd = '{} {} {}'.format(
                self.executable,
                ' '.join(self.local_args),
                ' '.join(additional_args))
            # We explicitly close all fds for the child to avoid
            # inheriting locks infinitely.
            self.log.info('start-qemu')
            self.log.debug(self.executable,
                           local_args=self.local_args,
                           additional_args=additional_args)
            p = subprocess.Popen(cmd, shell=True, close_fds=True,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            stdout, stderr = p.communicate()
            if p.returncode != 0:
                raise QemuNotRunning(p.returncode, stdout, stderr)
        except QemuNotRunning:
            # Did not start. Not running.
            self.log.exception('qemu-failed')
            raise

    def start(self):
        self._start()
        assert self.is_running()

    def freeze(self):
        with self.guestagent as guest:
            try:
                # This request may take a _long_ _long_ time and the default
                # timeout of 3 seconds will cause everything to explode when
                # the guest takes too long. We've seen 16 seconds as a regular
                # period in some busy and large machines. I'm being _very_
                # generous using a 120s timeout here.
                guest.cmd('guest-fsfreeze-freeze', timeout=120)
            except ClientError:
                self.log.debug('guest-fsfreeze-freeze-failed', exc_info=True)
            assert guest.cmd('guest-fsfreeze-status') == 'frozen'

    def thaw(self):
        tries = 10
        while tries:
            # Try _really_ _really_ hard to get the VM to thaw. Otherwise
            # it will just sit there and do nothing, causing the application
            # to crash and us to have to get up.
            with self.guestagent as guest:
                try:
                    guest.cmd('guest-fsfreeze-thaw')
                except ClientError:
                    self.log.debug('guest-fsfreeze-freeze-thaw', exc_info=True)
                if guest.cmd('guest-fsfreeze-status') == 'thawed':
                    break
                time.sleep(self.thaw_retry_timeout)
        else:
            self.log.error('guest-fsfreeze-thaw', result='failed')

    def write_file(self, path, content):
        with self.guestagent as guest:
            try:
                handle = guest.cmd('guest-file-open', path=path, mode='w')
                guest.cmd('guest-file-write',
                          handle=handle,
                          **{'buf-b64': content.encode('base64')})
                guest.cmd('guest-file-close', handle=handle)
            except ClientError:
                self.log.error('guest-write-file', exc_info=True)

    def inmigrate(self):
        self._start(['-incoming {}'.format(self.migration_address)])
        time.sleep(1)
        status = self.qmp.command('query-status')
        assert not status['running'], status
        assert status['status'] == 'inmigrate', status
        return self.migration_address

    def migrate(self, address):
        """Initiate actual (out-)migration"""
        self.log.debug('migrate')
        self.qmp.command('migrate-set-capabilities', capabilities=[
            {'capability': 'xbzrle', 'state': False},
            {'capability': 'auto-converge', 'state': True}])
        self.qmp.command('migrate-set-parameters',
                         **{'compress-level': 0})

        self.qmp.command('migrate_set_downtime', value=self.max_downtime)
        self.qmp.command('migrate_set_speed', value=0)
        self.qmp.command('migrate', uri=address)
        self.log.debug('migrate-parameters',
                       **self.qmp.command('query-migrate-parameters'))

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
            info = self.qmp.command('query-migrate')
            yield info

            if info['status'] == 'setup':
                pass
            elif info['status'] == 'completed':
                break
            elif info['status'] == 'active':
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
                    status = self.qmp.command('query-status')
                except (QMPConnectError, socket.error):
                    # Force a reconnect in the next iteration.
                    self.__qmp.close()
                    self.__qmp = None
                    qmp_available = False
                    monitor_says_running = False
                else:
                    monitor_says_running = status['running']

            if (expected_process_exists and
                    qmp_available and monitor_says_running):
                return True

            if (not expected_process_exists and not qmp_available):
                return False

        # The timeout passed and we were not able to determine a consistent
        # result. :/
        raise VMStateInconsistent(
            'Can not determine whether Qemu is running. '
            'Process exists: {}, QMP socket reliable: {}, '
            'Status is running: {}'.format(
                expected_process_exists, qmp_available, monitor_says_running),
                status)

    def rescue(self):
        """Recover from potentially inconsistent state.

        If the VM is running and we own all locks, then everything is fine.

        If the VM is running and we do not own the locks, then try to acquire
        them or bail out.

        Returns True if we were able to rescue the VM.
        Returns False if the rescue attempt failed and the VM is stopped now.

        """
        status = self.qmp.command('query-status')
        assert status['running']
        for image in set(self.locks.available) - set(self.locks.held):
            try:
                self.acquire_lock(image)
            except Exception:
                self.log.debug('acquire-lock-failed', exc_info=True)
        self.assert_locks()

    def graceful_shutdown(self):
        if not self.qmp:
            return
        self.qmp.command('send-key', keys=[
            {'type': 'qcode', 'data': 'ctrl'},
            {'type': 'qcode', 'data': 'alt'},
            {'type': 'qcode', 'data': 'delete'}])

    def destroy(self):
        # We use this destroy command in "fire-and-forget"-style because
        # sometimes the init script will complain even if we achieve what
        # we want: that the VM isn't running any longer. We check this
        # by contacting the monitor instead.
        timeout = TimeOut(100, interval=1, raise_on_timeout=True)
        p = self.proc()
        if not p:
            return
        while p.is_running() and timeout.tick():
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                pass

    def resize_root(self, size):
        self.qmp.command('block_resize', device='virtio0', size=size)

    def block_info(self):
        devices = {}
        for device in self.qmp.command('query-block'):
            devices[device['device']] = device
        return devices

    def block_io_throttle(self, device, iops):
        self.qmp.command('block_set_io_throttle',
                         device=device,
                         iops=iops, iops_rd=0, iops_wr=0,
                         bps=0, bps_wr=0, bps_rd=0)

    def watchdog_action(self, action):
        self.qmp.command(
            'human-monitor-command',
            **{'command-line': 'watchdog_action action={}'.format(action)})

    def clean_run_files(self):
        runfiles = glob.glob('/run/qemu.{}.*'.format(self.cfg['name']))
        if not runfiles:
            return
        self.log.debug('clean-run-files')
        for runfile in runfiles:
            if runfile.endswith('.lock'):
                # Never, ever, remove lock files. Those should be on
                # partitions that get cleaned out during reboot, but
                # never otherwise.
                continue
            os.unlink(runfile)

    def prepare_config(self):
        chroot = self.chroot.format(**self.cfg)
        if not os.path.exists(chroot):
            os.makedirs(chroot)

        def format(s):
            return s.format(
                pidfile=self.pidfile,
                configfile=self.configfile,
                monitor_port=self.monitor_port,
                vnc=self.vnc.format(**self.cfg),
                chroot=chroot,
                **self.cfg)
        self.local_args = [format(a) for a in self.args]
        self.local_config = format(self.config)

        with open(self.configfile + '.in', 'w') as f:
            f.write(self.config)
        with open(self.configfile, 'w') as f:
            f.write(self.local_config)
        with open(self.argfile + '.in', 'w') as f:
            yaml.safe_dump(self.args, f)

        # Qemu tends to overwrite the pid file incompletely -> truncate
        open(self.pidfile, 'w').close()

    def get_running_config(self):
        """Return the host-independent version of the current running
        config."""
        with open(self.argfile + '.in') as a:
            args = yaml.safe_load(a.read())
        with open(self.configfile + '.in') as c:
            config = c.read()
        return args, config

    def acquire_migration_lock(self):
        assert not self._migration_lock_fd
        open(self.migration_lockfile, 'a+').close()
        self._migration_lock_fd = os.open(self.migration_lockfile, os.O_RDONLY)
        try:
            fcntl.flock(self._migration_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.log.debug('acquire-migration-lock', result='success')
            return True
        except Exception as e:
            if isinstance(e, IOError):
                self.log.debug(
                    'acquire-migration-lock',result='failure',
                    reason='competing lock')
            else:
                self.log.exception(
                    'acquire-migration-lock',result='failure', exc_info=True)
            os.close(self._migration_lock_fd)
            self._migration_lock_fd = None
            return False

    def release_migration_lock(self):
        assert self._migration_lock_fd
        fcntl.flock(self._migration_lock_fd, fcntl.LOCK_UN)
        os.close(self._migration_lock_fd)
        self._migration_lock_fd = None
