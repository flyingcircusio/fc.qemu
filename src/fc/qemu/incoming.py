from .exc import MigrationError, QemuNotRunning
from .timeout import TimeOut
from .util import parse_address, log
import contextlib
import functools
import SimpleXMLRPCServer
import time


def authenticated(f):
    """Decorator to express that authentication is required."""
    @functools.wraps(f)
    def wrapper(self, cookie, *args):
        if cookie != self.cookie:
            raise MigrationError('authentication cookie mismatch')
        return f(self, *args)
    return wrapper


class IncomingServer(object):
    """Run an XML-RPC server to orchestrate a single migration."""

    finished = False

    def __init__(self, agent, timeout=330):
        self.agent = agent
        self.log = agent.log
        self.name = agent.name
        self.qemu = agent.qemu
        self.ceph = agent.ceph
        self.bind_address = parse_address(self.agent.migration_ctl_address)
        self.timeout = TimeOut(timeout, raise_on_timeout=False)
        self.consul = agent.consul

    _now = time.time

    @contextlib.contextmanager
    def inmigrate_service_registered(self):
        """Context manager for in-migration.

        Registers an inmigration service which keeps active as long as the
        with-block is executing.
        """
        svcname = 'vm-inmigrate-' + self.name
        self.log.debug('consul-register-inmigrate')
        self.consul.agent.service.register(
            svcname, address=self.bind_address[0], port=self.bind_address[1],
            ttl=self.timeout.remaining)
        try:
            yield
        finally:
            self.consul.agent.service.deregister(svcname)

    def run(self):
        s = SimpleXMLRPCServer.SimpleXMLRPCServer(
            self.bind_address, logRequests=False, allow_none=True)
        url = 'http://{}:{}/'.format(*self.bind_address)
        self.log.info('start-server', type='incoming', url=url)
        s.timeout = 1
        s.register_instance(IncomingAPI(self))
        s.register_introspection_functions()
        with self.inmigrate_service_registered():
            while self.timeout.tick():
                self.log.debug('waiting',
                               remaining=int(self.timeout.remaining))
                s.handle_request()
                if self.finished:
                    break
        self.log.info('stop-server', type='incoming', result=self.finished)
        if self.finished == 'success':
            return 0
        else:
            self.qemu.destroy()
            return 1

    def extend_cutoff_time(self, timeout=60):
        self.timeout.cutoff = self._now() + timeout

    def prepare_incoming(self, args, config):
        self.qemu.args = args
        self.qemu.config = config
        try:
            return self.qemu.inmigrate()
        except Exception:
            log.exception('incoming-migration-failed',
                          note='releasing locks', machine=self.name,
                          exc_info=True)
            self.ceph.stop()
            raise

    def rescue(self):
        if not self.qemu.is_running():
            log.warning('rescue-failed', reason='VM is offline',
                        machine=self.name)
            self.qemu.clean_run_files()
            self.ceph.stop()
            raise RuntimeError('rescue not possible - destroyed VM', self.name)
        try:
            log.info('rescue-locks', machine=self.name)
            self.ceph.lock()
        except Exception:
            log.warning('rescue-locks-failed', machine=self.name,
                        exc_info=True)
            self.destroy()
            raise
        log.info('rescue-succeeded', machine=self.name)

    def acquire_locks(self):
        self.ceph.lock()

    def finish_incoming(self):
        assert self.qemu.is_running()
        self.finished = 'success'

    def cancel(self):
        self.ceph.unlock()
        self.finished = 'cancelled'

    def destroy(self):
        """Reliably get rid of the VM."""
        log.info('destroying', machine=self.name)
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
        self.log = self.server.log
        self.cookie = server.agent.ceph.auth_cookie()

    @authenticated
    def ping(self):
        """Check connectivity and extend timeout."""
        self.log.debug('received-ping')
        self.server.extend_cutoff_time()

    @authenticated
    def acquire_locks(self):
        self.log.debug('received-acquire-locks')
        return self.server.acquire_locks()

    @authenticated
    def prepare_incoming(self, args, config):
        """Spawn KVM process ready to receive the VM.

        `args` and `config` should be the output of
        qemu.get_running_config() on the sending side.
        """
        self.log.debug('received-prepare-incoming')
        return self.server.prepare_incoming(args, config)

    @authenticated
    def finish_incoming(self):
        self.log.debug('received-finish-incoming')
        self.server.finish_incoming()

    @authenticated
    def rescue(self):
        """Incoming rescue."""
        self.log.debug('received-rescue')
        return self.server.rescue()

    @authenticated
    def destroy(self):
        """Incoming destroy."""
        self.log.debug('received-destroy')
        return self.server.destroy()

    @authenticated
    def cancel(self):
        self.log.debug('received-cancel')
        self.server.cancel()
