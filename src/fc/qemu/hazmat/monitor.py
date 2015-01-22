from ..exc import MigrationError
from fc.qemu.timeout import TimeOut
import logging
import re
import socket
import telnetlib


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

    def reset(self):
        self._connect()

    def _cmd(self, command):
        """Issue a monitor command and return QEMU's response.

        The monitor connection will be established on the first
        invocation.
        """
        if not self.conn:
            self._connect()
        _log.debug('[mon] (qemu) {}'.format(command))
        self.conn.write(command + '\n')
        res = self.conn.read_until('(qemu)', self.timeout)
        if res == '':  # Connection went away
            _log.debug('[mon] \\EOF')
            self.conn = None
            return ''
        r_strip_echo = re.compile(r'^.*' + re.escape(command) + '\S*\r\n')
        output = r_strip_echo.sub('', res).replace('\r\n', '\n')
        output = output.replace('(qemu)', '')
        show_res = output.replace('\n', '\n[mon] ')
        _log.debug('[mon] {}'.format(show_res))
        return output

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

    def sendkey(self, keys):
        self._cmd('sendkey {}'.format(keys))

    def migrate(self, address):
        """Initiate migration (asynchronously)."""
        self._cmd('migrate_set_capability xbzrle on')
        self._cmd('migrate_set_capability auto-converge on')
        # We are running qemu with chroot at the moment which causes us to
        # not be able to resolve names. :( See #13837.
        address = address.split(':')
        if address[0] == 'tcp':
            address[1] = socket.gethostbyname(address[1])
        address = ':'.join(address)
        res = self._cmd('migrate -d {}'.format(address)).strip()
        if res:
            raise MigrationError('error while initiating migration', res)

    def info_migrate(self):
        """Migration status and statistics."""
        try:
            return self._cmd('info migrate')
        except Exception:
            return ''

    def poll_migration_status(self, target, acceptable_interim, timeout=30):
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
        timeout = TimeOut(timeout, 1, raise_on_timeout=True)
        startup_phase = True
        while timeout.tick():
            if timeout.interval < 10:
                timeout.interval *= 1.4142
            status = self.info_migrate()
            yield status
            if startup_phase and not status.strip():
                # The monitor didn't really respond with anything.
                # This tends to happen sometimes at the beginning of the
                # migration. I let this slip.
                continue
            if target in status:
                break
            if not any(i in status for i in acceptable_interim):
                raise MigrationError('invalid migration status', status)
            startup_phase = False
            timeout.cutoff += 30

    def quit(self):
        """Terminate KVM process."""
        try:
            self._cmd('quit')
        except (EOFError, MigrationError):
            self.conn.close()
            self.conn = None
        else:
            raise RuntimeError('Machine did not quit?')
