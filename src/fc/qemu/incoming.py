from .exc import MigrationError, QemuNotRunning, ConfigChanged
from .timeout import TimeOut
from .util import parse_address, log
import contextlib
import functools
import re
import SimpleXMLRPCServer
import time


def authenticated(f):
    """Decorator to express that authentication is required."""
    @functools.wraps(f)
    def wrapper(self, cookie, *args):
        if cookie != self.cookie:
            self.log.debug('authentication-cookie-mismatch',
                method=f.__name__, received_cookie=cookie)
            raise MigrationError('authentication cookie mismatch')
        return f(self, *args)
    return wrapper


def reset_timeout(f):
    """Reset the timeout when interacting with the wrapped method."""
    @functools.wraps(f)
    def wrapper(self, *args):
        result = f(self, *args)
        self.log.debug('reset-timeout')
        self.server.extend_cutoff_time(soft_timeout=60)
        return result
    return wrapper


class IncomingServer(object):
    """Run an XML-RPC server to orchestrate a single migration."""

    finished = False
    obsolete_config_items = ['iommu']

    # How long to wait until we get the first connection by an outgoing
    # migration?
    # Maybe keep this in sync with the identically named timeout in outgoing.py
    connect_timeout = 60 * 60  # 1 hour

    def __init__(self, agent):
        self.agent = agent
        self.log = agent.log
        self.name = agent.name
        self.qemu = agent.qemu
        self.ceph = agent.ceph
        self.bind_address = parse_address(self.agent.migration_ctl_address)
        self.timeout = TimeOut(
            self.connect_timeout, interval=0, raise_on_timeout=False,
            log=self.log)
        self.consul = agent.consul
        self.had_contact = False

    _now = time.time

    @contextlib.contextmanager
    def inmigrate_service_registered(self):
        """Context manager for in-migration.

        Registers an inmigration service which remains active as long as the
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
            self.log.debug('consul-deregister-inmigrate')
            self.consul.agent.service.deregister(svcname)

    def run(self):
        s = SimpleXMLRPCServer.SimpleXMLRPCServer(
            self.bind_address, logRequests=False, allow_none=True)
        # Support ephemeral ports (specifying 0 as the bind port)
        # so we avoid running into recently used ports if migrations need
        # to be retried.
        self.bind_address = self.bind_address[0], s.socket.getsockname()[1]
        url = 'http://{}:{}/'.format(*self.bind_address)
        self.log.info('start-server', type='incoming', url=url)
        # This timeout causes the `handle_request` call a few lines down to
        # not block infinitely so the timeout-based while loop actually does
        # something useful. This is combined with having our peer call any
        # method on the API which will cause a reset of the timeout before
        # it is checked the next time.
        s.timeout = 15
        s._send_traceback_header = True
        s.register_instance(IncomingAPI(self))
        s.register_introspection_functions()
        with self.inmigrate_service_registered():
            while self.timeout.tick():
                s.handle_request()
                if not self.had_contact and self.agent.has_new_config():
                    # We are sure that we have not been in contact with the
                    # outgoing server and thus we can simply abort here
                    # (and check the new config) without risking to jump into
                    # any intermediate state of a running migration.
                    s.server_close()
                    raise ConfigChanged()
                if self.finished:
                    break
        try:
            self.release_migration_lock()
        except Exception:
            pass
        s.server_close()
        self.log.info('stop-server', type='incoming', result=self.finished)
        if self.finished == 'success':
            return 0
        else:
            self.qemu.destroy()
            return 1

    def extend_cutoff_time(self, hard_timeout=None, soft_timeout=None):
        assert bool(hard_timeout) != bool(soft_timeout)  # XOR
        # We start with a relatively high timeout but once we get a first
        # request we switch to always giving a new cutoff time starting from
        # now. This may cause sudden drops in remaining timeout but is
        # intentional: once we made contact we don't want to wait for many
        # minutes but expect communication to move fast.
        if hard_timeout:
            self.timeout.cutoff = self._now() + hard_timeout
        if soft_timeout:
            if self.timeout.remaining < soft_timeout:
                self.timeout.cutoff = self._now() + soft_timeout

    def screen_config(self, config):
        """Remove obsolete items from transferred Qemu config."""
        exprs = [re.compile(r'^\s*{}\s*=.*$'.format(re.escape(c)), re.M)
                 for c in self.obsolete_config_items]
        for expr in exprs:
            config = expr.sub('', config)
        return config

    def prepare_incoming(self, args, config):
        self.qemu.args = args
        # Adapt actual VM memory size: we will start with the proper parameter
        # but the memory verification needs to find the real value.
        # XXX This is a nasty code path.
        for arg in args:
            if arg.startswith('-m '):
                memory = int(arg.split(' ')[1])
                self.qemu.cfg['memory'] = memory
        self.qemu.config = self.screen_config(config)
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

    def acquire_migration_lock(self):
        return self.qemu.acquire_migration_lock()

    def release_migration_lock(self):
        self.qemu.release_migration_lock()

    def acquire_ceph_locks(self):
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
        self.log.debug('setup-incoming-api', cookie=self.cookie)

    @authenticated
    def ping(self, timeout=60):
        """Check connectivity and extend timeout.

        Allow the remote peer to inform us about ongoing slow
        processes that have a higher expected timeout.

        This can set a very high explicit timeout that will not
        get reduced by regular interaction.

        """
        self.log.debug('received-ping', timeout=timeout)
        self.server.extend_cutoff_time(hard_timeout=timeout)
        self.server.had_contact = True

    @authenticated
    @reset_timeout
    def acquire_migration_lock(self):
        self.log.debug('received-acquire-migration-lock')
        return self.server.acquire_migration_lock()

    @authenticated
    @reset_timeout
    def release_migration_lock(self):
        self.log.debug('received-release-migration-lock')
        return self.server.release_migration_lock()

    @authenticated
    @reset_timeout
    def acquire_ceph_locks(self):
        self.log.debug('received-acquire-ceph-locks')
        return self.server.acquire_ceph_locks()

    @authenticated
    @reset_timeout
    def prepare_incoming(self, args, config):
        """Spawn KVM process ready to receive the VM.

        `args` and `config` should be the output of
        qemu.get_running_config() on the sending side.
        """
        self.log.debug('received-prepare-incoming')
        return self.server.prepare_incoming(args, config)

    @authenticated
    @reset_timeout
    def finish_incoming(self):
        self.log.debug('received-finish-incoming')
        self.server.finish_incoming()

    @authenticated
    @reset_timeout
    def rescue(self):
        """Incoming rescue."""
        self.log.debug('received-rescue')
        return self.server.rescue()

    @authenticated
    @reset_timeout
    def destroy(self):
        """Incoming destroy."""
        self.log.debug('received-destroy')
        return self.server.destroy()

    @authenticated
    @reset_timeout
    def cancel(self):
        self.log.debug('received-cancel')
        self.server.cancel()

