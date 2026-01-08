import time

import mock
import pytest
from mock import call
from structlog import get_logger

from fc.qemu.outgoing import Heartbeat, Outgoing
from tests.conftest import get_log


@pytest.fixture
def outgoing():
    o = Outgoing(mock.MagicMock())
    o.target = mock.MagicMock()
    return o


def test_prefer_remote_rescue(outgoing):
    outgoing.rescue()
    assert outgoing.target.rescue.called is True
    assert outgoing.agent._destroy.called is True


def test_request_remote_destroy_if_remote_rescue_fails(outgoing):
    outgoing.target.rescue.side_effect = RuntimeError("boom")
    outgoing.rescue()
    assert outgoing.target.destroy.called is True
    assert outgoing.agent.qemu.destroy.called is False
    assert outgoing.agent.ceph.lock.called is True


def test_heartbeat_retry():
    connection = mock.Mock()
    log = get_logger()
    heartbeat = Heartbeat(log, connect=lambda url: connection)
    heartbeat.url = "..."
    heartbeat.PING_FREQUENCY = 1
    heartbeat.PING_RETRY_FREQUENCY = 0.5
    heartbeat.PING_RETRY_ATTEMPTS = 2
    connection.ping.side_effect = Exception("unsuccessful ping")
    heartbeat.start()
    while not heartbeat.failed:
        time.sleep(0.1)

    with pytest.raises(RuntimeError) as e:
        heartbeat.propagate()

    assert str(e.value) == "Heartbeat failed."

    assert connection.ping.call_args_list == [call(None), call(None)]


def test_heartbeat_success():
    connection = mock.Mock()
    log = get_logger()
    heartbeat = Heartbeat(log, connect=lambda url: connection)
    heartbeat.url = "..."
    heartbeat.PING_FREQUENCY = 2
    heartbeat.PING_RETRY_FREQUENCY = 0.1
    heartbeat.PING_RETRY_ATTEMPTS = 5
    heartbeat.start()
    time.sleep(5)  # -> 3 pings
    heartbeat.stop()
    heartbeat.thread.join()
    heartbeat.propagate()
    assert connection.ping.call_args_list == [
        call(None),
        call(None),
        call(None),
    ]

    assert (
        get_log()
        == """\
heartbeat-initialized
started-heartbeat-ping
heartbeat-ping
heartbeat-ping
heartbeat-ping
stopped-heartbeat-ping\
"""
    )
