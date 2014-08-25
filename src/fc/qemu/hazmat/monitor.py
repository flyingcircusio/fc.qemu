from ..exc import MigrationError
import re
import telnetlib
import time
import logging
from fc.qemu.timeout import TimeOut


_log = logging.getLogger(__name__)


class Monitor(object):
    """KVM monitor connection.

    Provides easy to use abstraction for monitor commands.
    """

    def __init__(self, port, timeout=10):
        self.port = port
        self.timeout = timeout
        self.conn = None

    def _connect(self):
        self.conn = telnetlib.Telnet('localhost', self.port, self.timeout)
        res = self.conn.read_until('(qemu)', self.timeout)
        if '(qemu)' not in res:
            raise RuntimeError('failed to establish monitor connection', res)

    def _cmd(self, command):
        """Issue a monitor command and return QEMU's response.

        The monitor connection will be established on the first
        invocation.
        """
        if not self.conn:
            self._connect()
        _log.debug('mon <<< %s', command)
        self.conn.write(command + '\n')
        res = self.conn.read_until('(qemu)', self.timeout)
        _log.debug('mon >>> %s', res)
        if '(qemu)' not in res:
            raise MigrationError('communication problem with QEMU monitor',
                                 command, res)
        r_strip_echo = re.compile(r'^.*' + re.escape(command) + '\S*\r\n')
        output = r_strip_echo.sub('', res).replace('\r\n', '\n')
        return output.replace('(qemu)', '')

    def status(self):
        """VM status summary.

        Returns one-line status string or an empty string if we cannot
        connect to the monitor.
        """
        try:
            return self._cmd('info status').strip()
        except Exception:
            return ''

    def assert_status(self, expected):
        status = self.status()
        if status != expected:
            raise RuntimeError(
                'VM status mismatch: expected "{}" got "{}"'.format(
                    expected, status))

    def migrate(self, host, port):
        """Initiate migration (asynchronously)."""
        self._cmd('migrate_set_capability xbzrle on')
        self._cmd('migrate_set_capability auto-converge on')
        res = self._cmd('migrate -d tcp:{}:{}'.format(host, port)).strip()
        if res:
            raise MigrationError('error while initiating migration', res)

    def info_migrate(self):
        """Migration status and statistics."""
        try:
            return self._cmd('info migrate')
        except Exception:
            return ''

    def poll_migration_status(self, target, acceptable_interim, timeout=1200):
        """Monitor ongoing migration.

        Every few seconds, the migration status is queried from the KVM
        monitor. It is yielded to the calling context to provide a hook
        for communicating status updates.

        The migration status is allowed to progress through any value of
        `acceptable_interim`. This function terminates when the status
        reaches `target`. If any status that is not in
        `acceptable_interim` nor `target` is reached, this function
        raises an exception.
        """
        give_up = time.time() + timeout
        timeout = TimeOut(timeout, .5)
        while timeout.tick():
            if timeout.interval < 5:
                timeout.interval *= 1.4
            status = self.info_migrate()
            yield status
            if not status.strip():
                # The monitor didn't really respond with anything.
                # This tends to happen sometimes at the beginning of the
                # migration. I let this slip.
                continue
            if target in status:
                break
            if not any(i in status for i in acceptable_interim):
                raise MigrationError('invalid migration status', status)
        else:
            raise MigrationError(
                'failed to reach target status in {}s'.format(timeout),
                target, status)

    def quit(self):
        """Terminate KVM process."""
        try:
            self._cmd('quit')
        except (EOFError, MigrationError):
            self.conn.close()
            self.conn = None
        else:
            raise RuntimeError('Machine did not quit?')
