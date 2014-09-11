from ..timeout import TimeOut
from .monitor import Monitor
import glob
import logging
import os.path
import psutil
import socket
import subprocess
import yaml


log = logging.getLogger(__name__)

HOSTNAME = socket.gethostname()  # XXX
SUFFIX = 'rzob.gocept.net'  # XXX


class QemuNotRunning(Exception):
    """Something happened and we're sure Qemu isn't running."""


class Qemu(object):

    executable = 'qemu-system-x86_64'

    # This cfg is the cfg from the agent.
    cfg = None

    # The non-hosts-specific config configuration of this Qemu instance.
    args = ()
    config = ''

    MONITOR_OFFSET = 20000

    # XXX support old pid
    pidfile = '/run/qemu.{name}.pid'
    configfile = '/run/qemu.{name}.cfg'
    argfile = '/run/qemu.{name}.args'

    def __init__(self, cfg):
        self.cfg = cfg
        for f in ['pidfile', 'configfile', 'argfile']:
            setattr(self, f, getattr(self, f).format(**cfg))

    def __enter__(self):
        self.monitor = Monitor(self.cfg['id'] + self.MONITOR_OFFSET)

    def __exit__(self, exc_value, exc_type, exc_tb):
        self.monitor = None

    def proc(self):
        pid = int(open(self.pidfile).read().strip())
        proc = psutil.Process(pid)
        assert proc.is_running()
        return proc

    def start(self):
        if not os.path.exists('/dev/kvm'):
            raise RuntimeError('Refusing to start without /dev/kvm support.')
        self.prepare_config()
        with open('/proc/sys/vm/compact_memory', 'w') as f:
            f.write('1')
        try:
            cmd = '{} {}'.format(self.executable, ' '.join(self.local_args))
            subprocess.check_call(cmd, shell=True)
        except subprocess.CalledProcessError:
            # Did not start. Not running.
            log.error('Failed to start qemu.')
            raise QemuNotRunning()
        assert self.is_running()

    def is_running(self):
        try:
            self.monitor.assert_status('VM status: running')
        except Exception:
            return False

        proc = self.proc()
        try:
            assert proc.name() == 'kvm.{name}'.format(**self.cfg)
        except Exception:
            log.error('Process name does not match. '
                      'Expected kvm.{} got {}'.format(
                          self.cfg['name'], proc.name()))
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
        self.proc().kill()
        timeout = TimeOut(5, interval=1, raise_on_timeout=True)
        while timeout.tick():
            status = self.monitor.status()
            if status == '':
                break

    def clean_run_files(self):
        for runfile in glob.glob('/run/qemu.*'):
            os.unlink(runfile)

    def prepare_config(self):
        format = lambda s: s.format(
            hostname=HOSTNAME,
            suffix=SUFFIX,
            pidfile=self.pidfile,
            configfile=self.configfile,
            monitor_port=self.monitor.port)
        self.local_args = [format(a) for a in self.args]
        self.local_config = format(self.config)

        with open(self.configfile+'.in', 'w') as f:
            f.write(self.config)
        with open(self.configfile, 'w') as f:
            f.write(self.local_config)

        with open(self.argfile+'.in', 'w') as f:
            yaml.dump(self.args, f)
