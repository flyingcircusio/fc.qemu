import pytest

from ..timeout import TimeOut, TimeoutError


def test_timeout_raises():
    timeout = TimeOut(0.1, raise_on_timeout=True)
    with pytest.raises(TimeoutError):
        while timeout.tick():
            pass


def test_timeout_stops():
    timeout = TimeOut(0.1)
    while timeout.tick():
        pass
