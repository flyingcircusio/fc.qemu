from .exc import MigrationError, LockError
from .incomingapi import IncomingAPI
from .timeout import TimeOut
from .vm import VM
import logging
import os
import SimpleXMLRPCServer
import socket
import subprocess
import time

_log = logging.getLogger(__name__)


class IncomingVM(VM):

    MIGRATION_CTL = '/run/kvm.{}.migrate'
    CFG_IN = '/run/kvm.{}.cfg.in'
    OPT_IN = '/run/kvm.{}.opt.in'

    def __init__(self, name, listen_host):
        super(IncomingVM, self).__init__(name)
        self.keep_listening = True
        self.listen_host = listen_host
        self.timeout = TimeOut(900, raise_on_timeout=True)

    def run(self):
        # Check whether the VM is running at all (locks are assumed). If the
        # locks aren't held by anyone, then we just go ahead and start the VM
        # without much ado. This makes it easier for outside instrumentation to
        # just fire up the inmigrate script without considering global
        # interactions too much.
        if self.locks.held == self.locks.available:
            # Either nothing is locked or we own all locks.
            _log.info('Incoming VM is not locked by anyone. '
                      'Starting it directly.')
            self.initd('start')
            return

        _log.info('Incoming server started. Current cutoff at {}'.format(
            self.timeout.cutoff))
        s = SimpleXMLRPCServer.SimpleXMLRPCServer(
            (self.listen_host, self.port), logRequests=False,
            allow_none=True)
        _log.debug('listening on %s:%s', self.listen_host, self.port)
        s.timeout = 1
        s.register_instance(IncomingAPI(self))
        s.register_introspection_functions()

        while self.timeout.tick():
            _log.info('Waiting for request ({} until cut-off)'.format(
                self.timeout.remaining))
            s.handle_request()
            if not self.keep_listening:
                break

        _log.info('VM migration completed, exiting')

    def extend_cutoff_time(self, timeout=30):
        self.timeout.cutoff = time.time() + timeout

    def prepare_incoming(self, addr, (cfg, opts)):
        with open(self.MIGRATION_CTL.format(self.name), 'w') as f:
            f.write('INCOMING_ADDR={}:{}\n'.format(addr, self.port))
        with open(self.CFG_IN.format(self.name), 'w') as f:
            f.write(cfg)
        with open(self.OPT_IN.format(self.name), 'w') as f:
            f.write(opts)

        self.initd('start', '--debug')
        self.monitor.assert_status('VM status: paused (inmigrate)')

    def finish_incoming(self):
        self.monitor.assert_status('VM status: running')
        self.keep_listening = False