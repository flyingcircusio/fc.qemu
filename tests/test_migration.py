import mock
import pytest

from fc.qemu.exc import MigrationError
from fc.qemu.incoming import (
    IncomingAPI,
    IncomingServer,
    authenticated,
    parse_address,
)


def test_authentication_wrapper():
    @authenticated
    def test(cookie):
        return 1

    context = mock.Mock()
    context.cookie = "asdf"
    assert test(context, "asdf") == 1

    with pytest.raises(MigrationError):
        test(context, "foobar")


def test_parse_address_host():
    assert ("host", 1234) == parse_address("host:1234")


def test_parse_address_ipv4():
    assert ("10.1.2.3", 4567) == parse_address("10.1.2.3:4567")


def test_parse_address_ipv5():
    assert ("2001:db8:1::45", 1234) == parse_address("[2001:db8:1::45]:1234")


def test_incoming_api():
    server = mock.Mock()
    server.agent.ceph.auth_cookie.return_value = "asdf"
    api = IncomingAPI(server)
    assert api.cookie == "asdf"

    api.ping("asdf")
    assert server.extend_cutoff_time.call_args_list == [
        mock.call(hard_timeout=60)
    ]

    api.acquire_migration_lock("asdf")
    assert server.acquire_migration_lock.call_args_list == [mock.call()]

    api.release_migration_lock("asdf")
    assert server.release_migration_lock.call_args_list == [mock.call()]

    api.acquire_ceph_locks("asdf")
    assert server.acquire_ceph_locks.call_args_list == [mock.call()]

    api.prepare_incoming("asdf", [], {})
    assert server.prepare_incoming.call_args_list == [mock.call([], {})]

    api.finish_incoming("asdf")
    assert server.finish_incoming.call_args_list == [mock.call()]

    api.rescue("asdf")
    assert server.rescue.call_args_list == [mock.call()]

    api.destroy("asdf")
    assert server.destroy.call_args_list == [mock.call()]

    api.cancel("asdf")
    assert server.cancel.call_args_list == [mock.call()]


def test_incoming_server():
    agent = mock.Mock()
    agent.migration_ctl_address = "localhost:9000"
    server = IncomingServer(agent)
    assert server.bind_address == ("localhost", 9000)

    server._now = server.timeout._now = mock.Mock(return_value=30)
    server.timeout.cutoff = 29
    server.extend_cutoff_time(soft_timeout=60)
    assert server.timeout.cutoff == 90
    server.extend_cutoff_time(soft_timeout=30)
    assert server.timeout.cutoff == 90
    server.extend_cutoff_time(soft_timeout=40)
    assert server.timeout.cutoff == 90
    server.extend_cutoff_time(soft_timeout=120)
    assert server.timeout.cutoff == 150
    server.extend_cutoff_time(hard_timeout=30)
    assert server.timeout.cutoff == 60
    server.extend_cutoff_time(hard_timeout=340)
    assert server.timeout.cutoff == 370
    server.extend_cutoff_time(soft_timeout=30)
    assert server.timeout.cutoff == 370
