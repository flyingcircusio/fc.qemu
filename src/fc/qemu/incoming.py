from .exc import MigrationError
from .timeout import TimeOut
import functools
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
    addr = addr.split(':')
    addr[1] = int(addr[1])
    return tuple(addr)


class IncomingServer(object):
    """Run an XML-RPC server to orchestrate a single migration."""

    keep_listening = True

    def __init__(self, agent):
        self.agent = agent
        self.bind_address = parse_address(self.agent.migration_ctl_address)
        self.timeout = TimeOut(900)

    def run(self):
        _log.info('[server] waiting for incoming {}.'.format(self.agent.name))
        s = SimpleXMLRPCServer.SimpleXMLRPCServer(
            self.bind_address, logRequests=False, allow_none=True)
        _log.info('[server] listening on http://{}:{}/'.format(
            *self.bind_address))
        s.timeout = 1
        s.register_instance(IncomingAPI(self))
        s.register_introspection_functions()
        while self.timeout.tick():
            _log.debug('[server] idle ({:d}s remaining)'.format(
                int(self.timeout.remaining)))
            s.handle_request()
            if not self.keep_listening:
                break
        else:
            _log.info('[server] timed out.')
            return 1

        _log.info('[server] migration completed')
        return 0

    def extend_cutoff_time(self, timeout=30):
        self.timeout.cutoff = time.time() + timeout

    def prepare_incoming(self, args, config):
        self.agent.qemu.args = args
        self.agent.qemu.config = config
        try:
            return self.agent.qemu.inmigrate()
        except Exception:
            self.agent.ceph.stop()
            raise

    def acquire_locks(self):
        self.agent.ceph.lock()

    def finish_incoming(self):
        assert self.agent.qemu.is_running()
        self.keep_listening = False

    def cancel(self):
        self.keep_listening = False
        self.agent.ceph.unlock()


class IncomingAPI(object):

    def __init__(self, server):
        self.server = server
        self.cookie = server.agent.ceph.auth_cookie()

    @authenticated
    def ping(self):
        """Check connectivity and extend timeout."""
        _log.info('[server] ping()')
        self.server.extend_cutoff_time()

    @authenticated
    def acquire_locks(self):
        _log.info('[server] acquire_locks()')
        return self.server.acquire_locks()

    @authenticated
    def prepare_incoming(self, args, config):
        """Spawn KVM process ready to receive the VM.

        `addr` is a host name of IP address which specifies where KVM
        should open its port.
        """
        _log.info('[server] prepare_incoming()')
        return self.server.prepare_incoming(args, config)

    @authenticated
    def finish_incoming(self):
        _log.info('[server] finish_incoming()')
        self.server.finish_incoming()

    @authenticated
    def rescue(self):
        """Incoming rescue."""
        _log.info('[server] rescue()')
        return self.server.rescue()

    @authenticated
    def destroy(self):
        """Incoming destroy."""
        _log.info('[server] destroy()')
        return self.server.destroy()

    @authenticated
    def cancel(self):
        _log.info('[server] cancel()')
        self.server.cancel()
