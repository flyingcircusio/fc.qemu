from .timeout import TimeOut
import pprint
import socket
import xmlrpclib


class Outgoing(object):

    migration_exitcode = None
    target = None
    cookie = None

    def __init__(self, agent):
        self.agent = agent
        self.log = agent.log
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
                self.log.debug('migration-failed',
                               action='rescue', exc_info=True)
                self.rescue()
                return True  # swallow exception
            except:
                # Purposeful bare except: try really hard to kill
                # our VM.
                self.log.exception(
                    'rescue-failed', exc_info=True, action='destroy')
                self.destroy()

    def locate_inmigrate_service(self, timeout=330):
        service_name = 'vm-inmigrate-' + self.name
        timeout = TimeOut(timeout, interval=3, raise_on_timeout=True)
        self.log.info('locate-inmigration-service')
        while timeout.tick():
            self.log.debug('waiting', remaining=int(timeout.remaining))
            inmig = self.consul.catalog.service(service_name)
            if inmig:
                if len(inmig) > 1:
                    self.log.warning('multiple-services-found',
                                     action='use newest', service=service_name)
                inmig = inmig[-1]
                url = 'http://{}:{}'.format(
                    inmig['ServiceAddress'], inmig['ServicePort'])
                self.log.info('located-inmigration-service', url=url)
                return url
        raise RuntimeError('failed to locate inmigrate service', service_name)

    def connect(self, timeout=330):
        timeout = TimeOut(timeout, interval=3, raise_on_timeout=True)
        while timeout.tick():
            try:
                address = self.locate_inmigrate_service()
                self.log.debug('connect', address=address)
                self.target = xmlrpclib.ServerProxy(address, allow_none=True)
                self.target.ping(self.cookie)
                break
            # XXX the default socket timeout is quite high. we might wanna
            # lower it on this side via socket.settimeout()
            except socket.error:
                self.log.debug('failed-connect', retrying=timeout.remaining,
                               exce_info=True)

    def transfer_locks(self):
        self.agent.ceph.unlock()
        self.target.acquire_locks(self.cookie)

    def migrate(self):
        """Actually move VM between hosts."""
        args, config = self.agent.qemu.get_running_config()
        self.log.info('prepare-remote-environment')
        migration_address = self.target.prepare_incoming(
            self.cookie, args, config)
        self.log.info('start-migration', target=migration_address)
        self.agent.qemu.migrate(migration_address)
        for stat in self.agent.qemu.poll_migration_status():
            remaining = stat['ram']['remaining'] if 'ram' in stat else '-'
            mbps = stat['ram']['mbps'] if 'ram' in stat else '-'
            self.log.info('migration-status',
                          status=stat['status'],
                          remaining=remaining,
                          mbps=mbps,
                          output=pprint.pformat(stat))
            self.target.ping(self.cookie)

        status = self.agent.qemu.qmp.command('query-status')
        assert not status['running'], status
        assert status['status'] == 'postmigrate', status
        self.log.info('finish-migration')
        try:
            self.target.finish_incoming(self.cookie)
        except Exception:
            self.log.exception('error-finish-remote', exc_info=True)
        self.agent.qemu.destroy()

    def rescue(self):
        """Outgoing rescue: try to rescue the remote side first."""
        self.log.exception('rescue', exc_info=True)
        if self.target is not None:
            try:
                self.target.rescue(self.cookie)
                self.target.finish_incoming(self.cookie)
                self.log.info('rescue-remote-success', action='destroy local')
                self.destroy()
                # We managed to rescue on the remote side - hooray!
                self.migration_exitcode = 0
                return
            except Exception:
                self.log.exception('rescue-remote-failed', exc_info=True,
                                   action='destroy remote')
                try:
                    self.target.destroy(self.cookie)
                except Exception:
                    self.log.exception('destroy-remote-failed', exc_info=True)
        try:
            self.log.info('acquire-local-locks')
            self.agent.ceph.lock()
        except Exception:
            self.log.exception('acquire-local-locks-failed', exc_info=True,
                               action='destroy local')
            self.destroy()
        else:
            self.log.info('acquire-local-locks-succeeded',
                          result='continuing locally')

    def destroy(self):
        self.agent.qemu.destroy()
        self.agent.qemu.clean_run_files()
