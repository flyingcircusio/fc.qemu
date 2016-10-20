"""Low-level interface to Qemu commands."""

from ..exc import QemuNotRunning
from ..sysconfig import sysconfig
from ..timeout import TimeOut
from ..util import log
from .guestagent import GuestAgent, ClientError
from .qmp import QEMUMonitorProtocol as Qmp, QMPConnectError
import glob
import os.path
import psutil
import socket
import subprocess
import time
import yaml
import datetime


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


class Qemu(object):

    executable = 'qemu-system-x86_64'

    # Attributes on this class can be overriden (in a controlled fashion
    # from the sysconfig module. See this class' __init__. The defaults
    # are here to support testing.

    cfg = None
    require_kvm = True
    migration_address = None
    max_downtime = 1.0

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

    def __init__(self, vm_cfg):
        # Update configuration values from system or test config.
        self.__dict__.update(sysconfig.qemu)

        self.cfg = vm_cfg
        # expand template keywords in configuration variables
        for f in ['pidfile', 'configfile', 'argfile', 'migration_address',
                  'qmp_socket']:
            setattr(self, f, getattr(self, f).format(**vm_cfg))
        # We are running qemu with chroot which causes us to not be able to
        # resolve names. :( See #13837.
        a = self.migration_address.split(':')
        if a[0] == 'tcp':
            a[1] = socket.gethostbyname(a[1])
        self.migration_address = ':'.join(a)
        self.name = self.cfg['name']
        self.monitor_port = self.cfg['id'] + self.MONITOR_OFFSET
        self.guestagent = GuestAgent(self.name, timeout=1)

        self.log = log.bind(machine=self.name, subsystem='qemu')

    __qmp = None

    @property
    def qmp(self):
        if self.__qmp is None:
            qmp = Qmp(self.qmp_socket, self.log)
            qmp.settimeout(5)
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
        if self.qmp:
            self.qmp.close()

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

    def _start(self, additional_args=()):
        if self.require_kvm and not os.path.exists('/dev/kvm'):
            raise RuntimeError('Refusing to start without /dev/kvm support.')
        self.prepare_config()
        self.prepare_log()
        with open('/proc/sys/vm/compact_memory', 'w') as f:
            f.write('1\n')
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
            subprocess.check_call(cmd, shell=True, close_fds=True)
        except subprocess.CalledProcessError:
            # Did not start. Not running.
            self.log.exception('qemu-failed', exc_info=True)
            raise QemuNotRunning()

    def start(self):
        self._start()
        assert self.is_running()

    def freeze(self):
        with self.guestagent as guest:
            try:
                guest.cmd('guest-fsfreeze-freeze')
            except ClientError:
                self.log.debug('guset-fsfreeze-freeze-failed', exc_info=True)
            assert guest.cmd('guest-fsfreeze-status') == 'frozen'

    def thaw(self):
        with self.guestagent as guest:
            try:
                guest.cmd('guest-fsfreeze-thaw')
            except ClientError:
                self.log.debug('guest-fsfreeze-freeze-thaw', exc_info=True)
            assert guest.cmd('guest-fsfreeze-status') == 'thawed'

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
            {'capability': 'xbzrle', 'state': True},
            {'capability': 'auto-converge', 'state': True}])
        self.qmp.command('migrate_set_downtime', value=self.max_downtime)
        self.qmp.command('migrate', uri=address)

    def poll_migration_status(self, timeout=30):
        """Monitor ongoing migration.

        Every few seconds, the migration status is queried from the Qemu
        monitor. It is yielded to the calling context to provide a hook
        for communicating status updates.

        """
        timeout = TimeOut(timeout, 0.02, raise_on_timeout=True)
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

    def is_running(self):
        # This method must be very reliable. It is perfectly OK to error
        # out in the case of inconsistencies. But a "true" must mean:
        # we have a working Qemu instance here. And a "false" must mean:
        # there is no reason to think that any remainder of a Qemu process is
        # still running

        timeout = TimeOut(10, 0.2, raise_on_timeout=False)
        while timeout.tick():
            # Try to find a stable result within a few seconds - ignore
            # unstable results in between. Qemu might just be starting
            # and the process already there but QMP not, or vice versa.

            # a) is there a process?
            proc = self.proc()
            if proc is None:
                expected_process_exists = False
            else:
                expected_process_exists = proc.is_running()

            # b) is the monitor port around reliably? Let's assume it does.
            qmp_available = self.qmp

            # c) is the monitor available and talks to us?
            monitor_says_running = False
            status = ''

            if qmp_available:
                try:
                    status = self.qmp.command('query-status')
                except QMPConnectError:
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
        raise RuntimeError(
            'Can not determine whether Qemu is running. '
            'Process exists: {} QMP socket reliable: {} '
            'Status is running: {} Status detail: {}'.format(
                expected_process_exists, qmp_available, monitor_says_running,
                status))

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

    def clean_run_files(self):
        self.log.debug('purge-run-files')
        for runfile in glob.glob('/run/qemu.{}.*'.format(self.cfg['name'])):
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
