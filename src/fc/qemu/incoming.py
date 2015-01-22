from .exc import MigrationError, QemuNotRunning
from .timeout import TimeOut
from .util import rewrite
import functools
import json
import logging
import SimpleXMLRPCServer
import time

_log = logging.getLogger(__name__)


def authenticated(f):
    """Decorator to express that authentication is required."""
    @functools.wraps(f)
    def wrapper(self, cookie, *args):
        if cookie != self.cookie:
            raise MigrationError('authentication cookie mismatch')
        return f(self, *args)
    return wrapper


def parse_address(addr):
    if addr.startswith('['):
        host, port = addr[1:].split(']:')
    else:
        host, port = addr.split(':')
    return host, int(port)


class IncomingServer(object):
    """Run an XML-RPC server to orchestrate a single migration."""

    finished = False

    def __init__(self, agent):
        self.agent = agent
        self.name = agent.name
        self.qemu = agent.qemu
        self.ceph = agent.ceph
        self.bind_address = parse_address(self.agent.migration_ctl_address)
        self.timeout = TimeOut(600)

    _now = time.time

    def run(self):
        s = SimpleXMLRPCServer.SimpleXMLRPCServer(
            self.bind_address, logRequests=False, allow_none=True)
        url = 'http://{}:{}/'.format(*self.bind_address)
        _log.info('%s: listening on %s', self.name, url)
        with rewrite(self.qemu.statefile) as f:
            json.dump({'migration-ctl-url': url}, f)
            f.write('\n')
        _log.info('%s: created migration state file %s', self.name,
                  self.qemu.statefile)
        s.timeout = 1
        s.register_instance(IncomingAPI(self))
        s.register_introspection_functions()
        while self.timeout.tick():
            _log.debug('[server] %s: waiting (%ds remaining)', self.name,
                       int(self.timeout.remaining))
            s.handle_request()
            if self.finished:
                break
        else:
            _log.info('[server] time out while migrating %s', self.name)
            return 1

        _log.info('%s: incoming migration completed: %s', self.name,
                  self.finished)
        return 0 if self.finished == 'success' else 1

    def extend_cutoff_time(self, timeout=30):
        self.timeout.cutoff = self._now() + timeout

    def prepare_incoming(self, args, config):
        self.qemu.args = args
        self.qemu.config = config
        try:
            return self.qemu.inmigrate()
        except Exception:
            self.ceph.stop()
            raise

    def rescue(self):
        if not self.qemu.is_running():
            _log.warning('%s: trying to rescue, but VM is not online',
                         self.name)
            self.qemu.clean_run_files()
            self.ceph.stop()
            raise RuntimeError('rescue not possible - destroyed VM', self.name)
        try:
            _log.info('%s: rescue - assuming locks', self.name)
            self.ceph.lock()
        except Exception:
            _log.warning('%s: failed to acquire all locks', self.name)
            self.destroy()
            raise
        _log.info('%s: rescue succeeded, VM is running', self.name)

    def acquire_locks(self):
        self.ceph.lock()

    def finish_incoming(self):
        assert self.qemu.is_running()
        self.finished = 'success'

    def cancel(self):
        self.ceph.unlock()
        self.finished = 'canceled'

    def destroy(self):
        """Gets reliably rid of the VM."""
        _log.info('%s: self-destructing', self.name)
        self.finished = 'destroyed'
        try:
            self.qemu.destroy()
        except QemuNotRunning:
            pass
        self.qemu.clean_run_files()
        try:
            self.ceph.unlock()
        except Exception:
            pass


class IncomingAPI(object):

    def __init__(self, server):
        self.server = server
        self.cookie = server.agent.ceph.auth_cookie()

    @authenticated
    def ping(self):
        """Check connectivity and extend timeout."""
        _log.debug('[server] ping()')
        self.server.extend_cutoff_time()

    @authenticated
    def acquire_locks(self):
        _log.debug('[server] acquire_locks()')
        return self.server.acquire_locks()

    @authenticated
    def prepare_incoming(self, args, config):
        """Spawn KVM process ready to receive the VM.

        `addr` is a host name of IP address which specifies where KVM
        should open its port.
        """
        _log.debug('[server] prepare_incoming()')
        return self.server.prepare_incoming(args, config)

    @authenticated
    def finish_incoming(self):
        _log.debug('[server] finish_incoming()')
        self.server.finish_incoming()

    @authenticated
    def rescue(self):
        """Incoming rescue."""
        _log.debug('[server] rescue()')
        return self.server.rescue()

    @authenticated
    def destroy(self):
        """Incoming destroy."""
        _log.debug('[server] destroy()')
        return self.server.destroy()

    @authenticated
    def cancel(self):
        _log.debug('[server] cancel()')
        self.server.cancel()
