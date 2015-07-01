"""Low-level interface to Qemu commands."""

from ..exc import QemuNotRunning
from ..timeout import TimeOut
from .monitor import Monitor
import glob
import logging
import os.path
import psutil
import socket
import subprocess
import time
import yaml


log = logging.getLogger(__name__)


class Qemu(object):

    executable = 'qemu-system-x86_64'

    # These variables get overridden in agent init or load_system_config()
    cfg = None
    require_kvm = True
    migration_address = None

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

    def __init__(self, cfg):
        self.cfg = cfg
        # expand template keywords in configuration variables
        for f in ['pidfile', 'configfile', 'argfile', 'migration_address']:
            setattr(self, f, getattr(self, f).format(**cfg))
        # We are running qemu with chroot which causes us to not be able to
        # resolve names. :( See #13837.
        a = self.migration_address.split(':')
        if a[0] == 'tcp':
            a[1] = socket.gethostbyname(a[1])
        self.migration_address = ':'.join(a)
        self.name = self.cfg['name']
        self.monitor_port = self.cfg['id'] + self.MONITOR_OFFSET

    def __enter__(self):
        self.monitor = Monitor(self.monitor_port)

    def __exit__(self, exc_value, exc_type, exc_tb):
        self.monitor = None

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

    def _start(self, additional_args=()):
        if self.require_kvm and not os.path.exists('/dev/kvm'):
            raise RuntimeError('Refusing to start without /dev/kvm support.')
        self.prepare_config()
        with open('/proc/sys/vm/compact_memory', 'w') as f:
            f.write('1\n')
        try:
            cmd = '{} {} {}'.format(
                self.executable,
                ' '.join(self.local_args),
                ' '.join(additional_args))
            # We explicitly close all fds for the child to avoid
            # inheriting locks infinitely.
            log.info('[qemu] %s: %s', self.name, cmd)
            subprocess.check_call(cmd, shell=True, close_fds=True)
        except subprocess.CalledProcessError:
            # Did not start. Not running.
            log.error('[qemu] %s: Failed to start', self.name)
            raise QemuNotRunning()
        self.monitor.reset()

    def start(self):
        self._start()
        assert self.is_running()

    def inmigrate(self):
        self._start(['-incoming {}'.format(self.migration_address)])
        time.sleep(1)
        self.monitor.assert_status('VM status: paused (inmigrate)')
        return self.migration_address

    def migrate(self, address):
        """Initiate actual (out-)migration"""
        log.debug('[qemu] %s: migrate (mon:%s)', self.name, self.monitor_port)
        self.monitor.migrate(address)

    def is_running(self):
        try:
            self.monitor.assert_status('VM status: running')
        except Exception:
            return False
        return True

    def status(self):
        return self.monitor.status()

    def rescue(self):
        """Recover from potentially inconsistent state.

        If the VM is running and we own all locks, then everything is fine.

        If the VM is running and we do not own the locks, then try to acquire
        them or bail out.

        Returns True if we were able to rescue the VM.
        Returns False if the rescue attempt failed and the VM is stopped now.

        """
        # XXX hold a lock to avoid another process on the same machine to
        # interfere with the VM while we're on it. E.g. by locking the main
        # config file.
        self.monitor.assert_status('VM status: running')
        for image in set(self.locks.available) - set(self.locks.held):
            try:
                self.acquire_lock(image)
            except Exception:
                pass

        self.assert_locks()

    def graceful_shutdown(self):
        self.monitor.sendkey('ctrl-alt-delete')

    def destroy(self):
        # We use this destroy command in "fire-and-forget"-style because
        # sometimes the init script will complain even if we achieve what
        # we want: that the VM isn't running any longer. We check this
        # by contacting the monitor instead.
        p = self.proc()
        if p:
            p.terminate()
        timeout = TimeOut(5, interval=1, raise_on_timeout=True)
        while timeout.tick():
            status = self.monitor.status()
            if status == '':
                break

    def resize_root(self, size):
        size = size / 1024**2  # MiB
        self.monitor._cmd('block_resize virtio0 {}'.format(size))

    def clean_run_files(self):
        for runfile in glob.glob('/run/qemu.{}.*'.format(self.cfg['name'])):
            os.unlink(runfile)

    def prepare_config(self):
        if not os.path.exists('/var/log/vm'):
            os.makedirs('/var/log/vm')

        chroot = self.chroot.format(**self.cfg)
        if not os.path.exists(chroot):
            os.makedirs(chroot)

        format = lambda s: s.format(
            pidfile=self.pidfile,
            configfile=self.configfile,
            monitor_port=self.monitor.port,
            vnc=self.vnc.format(**self.cfg),
            chroot=chroot,
            **self.cfg)
        self.local_args = [format(a) for a in self.args]
        self.local_config = format(self.config)

        with open(self.configfile+'.in', 'w') as f:
            f.write(self.config)
        with open(self.configfile, 'w') as f:
            f.write(self.local_config)
        with open(self.argfile+'.in', 'w') as f:
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
