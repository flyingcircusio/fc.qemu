from .timeout import TimeOut
import logging
import socket
import xmlrpclib

_log = logging.getLogger(__name__)


class Outgoing(object):

    migration_exitcode = None
    target = None
    cookie = None

    def __init__(self, agent, address):
        self.agent = agent
        self.address = address

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
                _log.exception('A problem occured trying to migrate the VM. '
                               'Trying to rescue it.',
                               exc_info=(_exc_type, _exc_value, _traceback))
                self.rescue()
            except:
                # Purposeful bare except: try really hard to kill
                # our VM.
                _log.exception('A problem occured trying to rescue the VM '
                               'after a migration failure. Destroying it.')
                self.destroy()

    def connect(self, timeout=900):
        _log.debug('connecting to {}'.format(self.address))

        timeout = TimeOut(timeout, interval=3, raise_on_timeout=True)
        while timeout.tick():
            try:
                self.target = xmlrpclib.ServerProxy(
                    self.address, allow_none=True)
                self.target.ping(self.cookie)
                break
            # XXX the default socket timeout is quite high. we might wanna
            # lower it on this side via socket.settimeout()
            except socket.error:
                _log.debug('failed connecting, retrying later')

    def transfer_locks(self):
        self.agent.ceph.unlock()
        self.target.acquire_locks(self.cookie)

    def migrate(self):
        """Actually move VM between hosts."""
        args, config = self.agent.qemu.get_running_config()
        migration_address = self.target.prepare_incoming(
            self.cookie, args, config)
        self.agent.qemu.migrate(migration_address)
        for _ in self.agent.qemu.monitor.poll_migration_status(
                'Migration status: completed', ['Migration status: active']):
            self.target.ping(self.cookie)

        self.agent.qemu.monitor.assert_status(
            'VM status: paused (postmigrate)')
        self.target.finish_incoming(self.cookie)
        self.agent.qemu.destroy()
        self.agent.qemu.clean_run_files()

    def rescue(self):
        """Outgoing rescue: try to rescue the remote side first."""
        _log.warning('something went wrong, trying to rescue an instance')
        try:
            self.target.rescue(self.cookie)
        except:
            # The remote VM was not rescued successfully and we can't trust
            # that it self-destructed succesfully.
            try:
                self.target.destroy()
            except Exception:
                pass
            self.agent.ceph.lock()
        else:
            # The remote VM was rescued successfully so we destroy ourselves.
            self.destroy()

    def destroy(self):
        self.agent.qemu.destroy()
