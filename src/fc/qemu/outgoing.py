from .timeout import TimeOut
from .util import log
import socket
import xmlrpclib


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
                log.debug('A problem occured trying to migrate the VM. '
                          'Trying to rescue it.',
                          exc_info=(_exc_type, _exc_value, _traceback))
                self.rescue()
                return True  # swallow exception
            except:
                # Purposeful bare except: try really hard to kill
                # our VM.
                log.exception('A problem occured trying to rescue the VM '
                              'after a migration failure. Destroying it.')
                self.destroy()

    def locate_inmigrate_service(self, timeout=330):
        service_name = 'vm-inmigrate-' + self.name
        timeout = TimeOut(timeout, interval=3, raise_on_timeout=True)
        log.info('locate-inmigration-service')
        while timeout.tick():
            log.debug('waiting', remaining=int(timeout.remaining),
                      machine=self.name)
            inmig = self.consul.catalog.service(service_name)
            if inmig:
                if len(inmig) > 1:
                    log.warning('multiple-services-found',
                                action='use newest',
                                service=service_name)
                inmig = inmig[-1]
                url = 'http://{}:{}'.format(
                    inmig['ServiceAddress'], inmig['ServicePort'])
                log.info('located-inmigration-service', url=url)
                return url
        raise RuntimeError('failed to locate inmigrate service', service_name)

    def connect(self, timeout=330):
        timeout = TimeOut(timeout, interval=3, raise_on_timeout=True)
        while timeout.tick():
            try:
                address = self.locate_inmigrate_service()
                log.debug('connecting to {}'.format(address))
                self.target = xmlrpclib.ServerProxy(address, allow_none=True)
                self.target.ping(self.cookie)
                break
            # XXX the default socket timeout is quite high. we might wanna
            # lower it on this side via socket.settimeout()
            except socket.error:
                log.debug(
                    'cannot establish XML-RPC connection, retrying for %ds',
                    timeout.remaining)

    def transfer_locks(self):
        self.agent.ceph.unlock()
        self.target.acquire_locks(self.cookie)

    def migrate(self):
        """Actually move VM between hosts."""
        args, config = self.agent.qemu.get_running_config()
        log.info('prepare-incoming-environment', machine=self.name)
        migration_address = self.target.prepare_incoming(
            self.cookie, args, config)
        log.info('start-migration', machine=self.name,
                 target=migration_address)
        self.agent.qemu.migrate(migration_address)
        for stat in self.agent.qemu.poll_migration_status():
            self.target.ping(self.cookie)

        status = self.agent.qemu.qmp.command('query-status')
        assert not status['running'], status
        assert status['status'] == 'postmigrate', status
        log.info('finish-migration', machine=self.name)
        self.target.finish_incoming(self.cookie)
        self.agent.qemu.destroy()

    def rescue(self):
        """Outgoing rescue: try to rescue the remote side first."""
        log.exception('rescue', machine=self.name)
        if self.target is not None:
            try:
                self.target.rescue(self.cookie)
                self.target.finish_incoming(self.cookie)
                log.info('rescue-remote-success', action='destroy local',
                         machine=self.name)
                self.destroy()
                # We managed to rescue on the remote side - hooray!
                self.migration_exitcode = 0
                return
            except Exception:
                log.exception('rescue-remote-failed',
                              action='destroy remote', machien=self.name)
                try:
                    self.target.destroy(self.cookie)
                except Exception:
                    log.exception('destroy-remote-failed', machine=self.name)
        try:
            log.info('acquire-local-locks', machine=self.name)
            self.agent.ceph.lock()
        except Exception:
            log.exception('acquire-local-locks-failed', action='destroy local')
            self.destroy()
        else:
            log.info('acquire-local-locks-succeeded',
                     result='continuing locally')

    def destroy(self):
        self.agent.qemu.destroy()
        self.agent.qemu.clean_run_files()
