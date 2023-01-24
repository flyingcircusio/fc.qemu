import io

import pytest

from ..guestagent import ClientError


def test_ga_read(guest_agent):
    guest_agent.file = io.StringIO('{"return": 17035}\n')
    assert 17035 == guest_agent.read()


def test_ga_read_error(guest_agent):
    guest_agent.file = io.StringIO('{"return": 0, "error": "test failure"}\n')
    with pytest.raises(ClientError):
        guest_agent.read()


def test_ga_sync_immediate(guest_agent):
    guest_agent.file = io.StringIO('{"return": 87643}\n')
    guest_agent.sync()
    assert True


def test_ga_sync_retry(guest_agent):
    guest_agent.file = io.StringIO('{"return": 2}\n{"return": 87643}\n')
    guest_agent.sync()
    assert True


def test_ga_sync_too_often(guest_agent):
    guest_agent.file = io.StringIO(
        """\
{"return": 2}
{"return": 3}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 4}
{"return": 87643}
"""
    )
    with pytest.raises(ClientError):
        guest_agent.sync()


def test_ga_contextmgr(guest_agent, monkeypatch, tmpdir):
    with guest_agent as g:
        assert g.machine == "testvm"
