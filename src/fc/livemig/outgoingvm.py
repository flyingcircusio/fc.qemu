from .exc import MigrationError
from .vm import VM
from .timeout import TimeOut
import logging
import socket
import time
import xmlrpclib

_log = logging.getLogger(__name__)


class OutgoingVM(VM):

    def __init__(self, name, ctladdr, migaddr):
        super(OutgoingVM, self).__init__(name)
        self.ctladdr = ctladdr
        self.migaddr = migaddr
        self.target = None

    def wait_for_incoming_agent(self, timeout=900):
        _log.debug('connecting to %s:%s', self.ctladdr, self.port)

        timeout = TimeOut(timeout, interval=3, raise_on_timeout=True)
        while timeout.tick():
            try:
                self.target = xmlrpclib.ServerProxy('http://{}:{}/'.format(
                    self.ctladdr, self.port), allow_none=True)
                self.target.ping(self.cookie)
                break
            # XXX the default socket timeout is quite high. we might wanna
            # lower it on this side via socket.settimeout()
            except socket.error:
                _log.debug('failed connecting, retrying later')

    def transfer_locks(self):
        self.assert_locks()

        for image in list(self.locks.held):
            _log.info('transferring lock for %s', image)
            self.release_lock(image)
            try:
                self.target.acquire_lock(self.cookie, image)
            except Exception:
                _log.warning('failed to transfer lock for %s', image)
                self.acquire_lock(image)
                raise

    def get_running_options(self):
        config = open('/run/kvm.{}.cfg.in'.format(self.name)).read()
        opts = open('/run/kvm.{}.opt.in'.format(self.name)).read()
        return config, opts

    def migrate(self):
        """Actually move VM between hosts."""
        _log.info('starting to transfer VM to %s:%s', self.migaddr, self.port)

        self.target.prepare_incoming(self.cookie, self.migaddr,
                                     self.get_running_options())
        self.monitor.migrate(self.migaddr, self.port)
        for mig_status in self.monitor.poll_migration_status(
                'Migration status: completed', ['Migration status: active']):
            self.target.ping(self.cookie)

        # XXX not knowing what is going on here is dangerous, I guess.
        # this could even mean we already started to corrupt the disk, if the
        # target started running already and we might be running, too.
        self.monitor.assert_status('VM status: paused (postmigrate)')
        self.target.finish_incoming(self.cookie)
        _log.info('VM migration completed, stopping VM')
        self.monitor.quit()
        self.initd('zap')

    def rescue(self):
        """Outgoing rescue: try to rescue the remote side first."""
        _log.warning('something went wrong, trying to clean up')
        try:
            self.target.rescue(self.cookie)
        except:
            try:
                self.target.destroy(self.cookie)
            except Exception:
                pass
            super(OutgoingVM, self).rescue()
        else:
            # The remote VM was rescued successfully so we destroy ourselves.
            self.destroy()
