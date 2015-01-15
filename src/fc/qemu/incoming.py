from .exc import MigrationError
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

    keep_listening = True

    def __init__(self, agent):
        self.agent = agent
        self.bind_address = parse_address(self.agent.migration_ctl_address)
        self.timeout = TimeOut(900)

    _now = time.time

    def run(self):
        _log.info('[server] waiting for incoming {}.'.format(self.agent.name))
        s = SimpleXMLRPCServer.SimpleXMLRPCServer(
            self.bind_address, logRequests=False, allow_none=True)
        url = 'http://{}:{}/'.format(*self.bind_address)
        _log.info('[server] listening on {}'.format(url))
        with rewrite(self.agent.qemu.statefile) as f:
            json.dump({'migration-ctl-url': url}, f)
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
        self.timeout.cutoff = self._now() + timeout

    def prepare_incoming(self, args, config):
        self.agent.qemu.args = args
        self.agent.qemu.config = config
        try:
            return self.agent.qemu.inmigrate()
        except Exception:
            self.agent.ceph.stop()
            raise

    def rescue(self):
        # Assume that we're running and try to get the locks if we didn't
        # have them before.
        try:
            self.agent.ceph.lock()
        except Exception:
            # We did not manage to get the locks and we're in an unknown state.
            # Try to self-destruct ASAP.
            self.destroy()
            self.keep_listening = True
            raise

    def acquire_locks(self):
        self.agent.ceph.lock()

    def finish_incoming(self):
        assert self.agent.qemu.is_running()
        self.keep_listening = False

    def cancel(self):
        self.agent.ceph.unlock()
        self.keep_listening = False

    def destroy(self):
        self.agent.qemu.destroy()
        try:
            self.agent.ceph.unlock()
        except Exception:
            pass
        self.keep_listening = False


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
