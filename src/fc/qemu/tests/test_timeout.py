import pytest

from fc.qemu.util import log

from ..timeout import TimeOut, TimeoutError


def test_timeout_raises():
    log_ = log.bind(machine="test")
    timeout = TimeOut(0.1, raise_on_timeout=True, log=log_)
    with pytest.raises(TimeoutError):
        while timeout.tick():
            pass


def test_timeout_stops():
    log_ = log.bind(machine="test")
    timeout = TimeOut(0.1, log=log_)
    while timeout.tick():
        pass
