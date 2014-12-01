from fc.qemu.exc import MigrationError
from fc.qemu.incoming import authenticated, parse_address
from fc.qemu.incoming import IncomingAPI, IncomingServer
import mock
import pytest


def test_authentication_wrapper():
    @authenticated
    def test(cookie):
        return 1

    context = mock.Mock()
    context.cookie = 'asdf'
    assert test(context, 'asdf') == 1

    with pytest.raises(MigrationError):
        test(context, 'foobar')


def test_parse_address():
    assert ('asdf', 1234) == parse_address('asdf:1234')


def test_incoming_api():
    server = mock.Mock()
    server.agent.ceph.auth_cookie.return_value = 'asdf'
    api = IncomingAPI(server)
    assert api.cookie == 'asdf'

    api.ping('asdf')
    assert server.extend_cutoff_time.call_args_list == [mock.call()]

    api.acquire_locks('asdf')
    assert server.acquire_locks.call_args_list == [mock.call()]

    api.prepare_incoming('asdf', [], {})
    assert server.prepare_incoming.call_args_list == [mock.call([], {})]

    api.finish_incoming('asdf')
    assert server.finish_incoming.call_args_list == [mock.call()]

    api.rescue('asdf')
    assert server.rescue.call_args_list == [mock.call()]

    api.destroy('asdf')
    assert server.destroy.call_args_list == [mock.call()]

    api.cancel('asdf')
    assert server.cancel.call_args_list == [mock.call()]


def test_incoming_server():
    agent = mock.Mock()
    agent.migration_ctl_address = 'localhost:9000'
    server = IncomingServer(agent)
    assert server.bind_address == ('localhost', 9000)

    server._now = mock.Mock(return_value=30)
    server.extend_cutoff_time()
    assert server.timeout.cutoff == 60
