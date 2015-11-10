from .timeout import TimeOut
import logging
import socket
import xmlrpclib

_log = logging.getLogger(__name__)


class Outgoing(object):

    migration_exitcode = None
    target = None
    cookie = None

    def __init__(self, agent):
        self.agent = agent
        self.name = agent.name
        self.consul = agent.consul

    def __call__(self):
        self.cookie = self.agent.ceph.auth_cookie()
        with self:
            self.connect()
            self.transfer_locks()
            self.migrate()

        return self.migration_exitcode

    # The context manager is suitable to express "I am going to
    # interact with this VM: please prepare connections to
    # the according backends and make sure to clean up after me.""
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        if _exc_type is None:
            self.migration_exitcode = 0
        else:
            self.migration_exitcode = 1
            try:
                _log.debug('A problem occured trying to migrate the VM. '
                           'Trying to rescue it.',
                           exc_info=(_exc_type, _exc_value, _traceback))
                self.rescue()
                return True  # swallow exception
            except:
                # Purposeful bare except: try really hard to kill
                # our VM.
                _log.exception('A problem occured trying to rescue the VM '
                               'after a migration failure. Destroying it.')
                self.destroy()

    def locate_inmigrate_service(self, timeout=330):
        service_name = 'vm-inmigrate-' + self.name
        timeout = TimeOut(timeout, interval=3, raise_on_timeout=True)
        while timeout.tick():
            _log.debug('%s: waiting (%ds remaining)', self.name,
                       int(timeout.remaining))
            inmig = self.consul.catalog.service(service_name)
            if inmig:
                if len(inmig) > 1:
                    _log.warning('found more than one inmig services for %s, '
                                 'trying the most recent one', service_name)
                inmig = inmig[-1]
                _log.debug('found incoming service %r', inmig)
                return 'http://{}:{}'.format(
                    inmig['ServiceAddress'], inmig['ServicePort'])
        raise RuntimeError('failed to locate inmigrate service', service_name)

    def connect(self, timeout=330):
        timeout = TimeOut(timeout, interval=3, raise_on_timeout=True)
        while timeout.tick():
            try:
                address = self.locate_inmigrate_service()
                _log.debug('connecting to {}'.format(address))
                self.target = xmlrpclib.ServerProxy(address, allow_none=True)
                self.target.ping(self.cookie)
                break
            # XXX the default socket timeout is quite high. we might wanna
            # lower it on this side via socket.settimeout()
            except socket.error:
                _log.debug(
                    'cannot establish XML-RPC connection, retrying for %ds',
                    timeout.remaining)

    def transfer_locks(self):
        self.agent.ceph.unlock()
        self.target.acquire_locks(self.cookie)

    def migrate(self):
        """Actually move VM between hosts."""
        args, config = self.agent.qemu.get_running_config()
        _log.info('%s: preparing remote environment', self.name)
        migration_address = self.target.prepare_incoming(
            self.cookie, args, config)
        _log.info('%s: starting transfer to %s', self.name, migration_address)
        self.agent.qemu.migrate(migration_address)
        # XXX The status polling is a bit fuzzy: we noticed an additional
        # intermediate status "setup" while running this in production.
        # Over the long term this should get more reliable towards unknown
        # states and tolerate them (until running in a timeout)
        for stat in self.agent.qemu.monitor.poll_migration_status(
                'Migration status: completed',
                ['Migration status: active',
                 'Migration status: setup']):
            self.target.ping(self.cookie)

        self.agent.qemu.monitor.assert_status(
            'VM status: paused (postmigrate)')
        _log.info('%s: finishing and cleaning up', self.name)
        self.target.finish_incoming(self.cookie)
        self.agent.qemu.destroy()

    def rescue(self):
        """Outgoing rescue: try to rescue the remote side first."""
        _log.exception('{}: something went wrong, trying to rescue'.format(self.name))
        if self.target is not None:
            try:
                self.target.rescue(self.cookie)
                _log.info('%s: remote rescue successful, destroying our '
                          'instance', self.name)
                self.destroy()
                return
            except Exception as e:
                _log.debug(e)
                try:
                    _log.info('%s: remote rescue failed, trying remote '
                              'destroy', self.name)
                    self.target.destroy(self.cookie)
                except Exception as e:
                    _log.debug(e)
                    _log.info('%s: failed to destroy remote VM', self.name)
        try:
            self.agent.ceph.lock()
            _log.info('%s: re-acquired Ceph locks successfully, continuing '
                      'VM locally', self.name)
        except Exception:
            _log.warning('%s: failed to (re-)aquire Ceph locks, bailing out',
                         self.name)
            self.destroy()

    def destroy(self):
        self.agent.qemu.destroy()
        self.agent.qemu.clean_run_files()
