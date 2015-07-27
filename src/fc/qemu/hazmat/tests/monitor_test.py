from ...exc import MigrationError
from ..monitor import Monitor
import pytest
import socket
import telnetlib
import time


class FakeTelnet(object):

    def __init__(self, host, port, timeout=0):
        pass

    def write(self, data):
        pass

    def close(self):
        pass


class MigrationStatusSequence(FakeTelnet):

    # This is a class-mutable by intention: we need several connections
    # and keep track over those.
    attempts = []

    def read_until(self, search, timeout=0):
        if len(self.attempts) < 2:
            self.attempts.append(search)
            return 'Migration status: active\r\n(qemu)\r\n'
        return 'Migration status: completed\r\n(qemu)\r\n'


class ConnectionRefusedTelnet(FakeTelnet):

    def __init__(self, host, port, timeout=0):
        raise socket.error(111, 'Connection refused')


class TestMonitor(object):

    def test_cmd_sanitizes_info_status(self, monkeypatch):
        """The telnet monitor interface returns screwed echo."""
        monkeypatch.setattr(telnetlib, 'Telnet', FakeTelnet)
        # this crazy stuff is what we actually get from KVM :-/
        setattr(FakeTelnet, 'read_until', lambda self, exp, timeout: (
            ' i\x1b[K\x1b[Din\x1b[K\x1b[D\x1b[Dinf\x1b[K\x1b[D\x1b[D\x1b[Dinfo'
            '\x1b[K\x1b[D\x1b[D\x1b[D\x1b[Dinfo \x1b[K\x1b[D\x1b[D\x1b[D\x1b[D'
            '\x1b[Dinfo s\x1b[K\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[Dinfo st'
            '\x1b[K\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[Dinfo sta\x1b[K'
            '\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[Dinfo stat\x1b[K'
            '\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[Dinfo statu'
            '\x1b[K\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D'
            '\x1b[Dinfo status\x1b[K\r\nVM status: running\r\n(qemu)'))
        m = Monitor(12345)
        assert 'VM status: running\n' == m._cmd('info status')

    def test_cmd_raises_unless_prompt_in_output(self, monkeypatch):
        monkeypatch.setattr(telnetlib, 'Telnet', FakeTelnet)
        setattr(FakeTelnet, 'read_until', lambda self, exp, timeout: '')
        m = Monitor(12345)
        with pytest.raises(RuntimeError):
            m._cmd('info status')

    def test_status_should_not_catch_connection_errors(self, monkeypatch):
        monkeypatch.setattr(telnetlib, 'Telnet', ConnectionRefusedTelnet)
        m = Monitor(12345)
        with pytest.raises(socket.error):
            m._cmd('info status')

    def test_migstatus_should_catch_connection_errors(self, monkeypatch):
        monkeypatch.setattr(telnetlib, 'Telnet', ConnectionRefusedTelnet)
        m = Monitor(12345)
        assert m.info_migrate() == ''

    def test_poll_status_should_terminate_on_reached_state(self, monkeypatch):
        MigrationStatusSequence.attempts = []
        monkeypatch.setattr(telnetlib, 'Telnet', MigrationStatusSequence)
        monkeypatch.setattr(time, 'sleep', lambda t: None)
        m = Monitor(12345)
        res = list(m.poll_migration_status('Migration status: completed', [
            'Migration status: active', 'Migration status: starting']))
        assert 'Migration status: completed' in res[-1]

    def test_poll_status_should_raise_on_unexpected_status(self, monkeypatch):
        MigrationStatusSequence.attempts = []
        monkeypatch.setattr(telnetlib, 'Telnet', MigrationStatusSequence)
        monkeypatch.setattr(time, 'sleep', lambda t: None)
        m = Monitor(12345)
        with pytest.raises(MigrationError):
            for s in m.poll_migration_status('Migration status: completed', [
                    'interim status 1', 'interim status 2']):
                pass
