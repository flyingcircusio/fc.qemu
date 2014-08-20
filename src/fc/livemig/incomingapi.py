from .exc import MigrationError
import functools
import logging

_log = logging.getLogger(__name__)


def authenticated(f):
    """Decorator to express that authentication is required."""
    @functools.wraps(f)
    def wrapper(self, cookie, *args):
        if cookie != self.cookie:
            raise MigrationError('authentication cookie mismatch')
        return f(self, *args)
    return wrapper


class IncomingAPI(object):

    def __init__(self, vm):
        self.vm = vm
        self.cookie = vm.cookie

    @authenticated
    def ping(self):
        """Check connectivity and extend timeout."""
        _log.info('got pinged')
        self.vm.extend_cutoff_time()

    @authenticated
    def acquire_lock(self, image_name):
        _log.info('receiving lock for %s', image_name)
        return self.vm.acquire_lock(image_name)

    @authenticated
    def prepare_incoming(self, addr, options):
        """Spawn KVM process ready to receive the VM.

        `addr` is a host name of IP address which specifies where KVM
        should open its port.
        """
        _log.debug('prepare incoming, listen on %s:%s', addr, self.vm.port)
        self.vm.prepare_incoming(addr, options)

    @authenticated
    def finish_incoming(self):
        _log.debug('finish incoming')
        self.vm.finish_incoming()

    @authenticated
    def rescue(self):
        """Incoming rescue."""
        _log.debug('received rescue request')
        return self.vm.rescue()

    @authenticated
    def destroy(self):
        """Incoming destroy."""
        _log.debug('received destroy request')
        return self.vm.destroy()
