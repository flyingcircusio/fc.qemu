from ..guestagent import GuestAgent, ClientError
import mock
import pytest
import socket
import StringIO


@pytest.fixture
def ga(monkeypatch):
    ga = GuestAgent('testvm', .1)
    ga.file = StringIO.StringIO()
    ga.client = mock.MagicMock()
    randint = mock.Mock(return_value=87643)
    monkeypatch.setattr('random.randint', randint)
    return ga


def test_ga_read(ga):
    ga.file = StringIO.StringIO('{"return": 17035}\n')
    assert 17035 == ga.read()


def test_ga_read_error(ga):
    ga.file = StringIO.StringIO('{"return": 0, "error": "test failure"}\n')
    with pytest.raises(ClientError):
        ga.read()


def test_ga_sync_immediate(ga):
    ga.file = StringIO.StringIO('{"return": 87643}\n')
    ga.sync()
    assert True


def test_ga_sync_retry(ga):
    ga.file = StringIO.StringIO('{"return": 2}\n{"return": 87643}\n')
    ga.sync()
    assert True


def test_ga_sync_too_often(ga):
    ga.file = StringIO.StringIO("""\
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
""")
    with pytest.raises(ClientError):
        ga.sync()


def test_ga_contextmgr(ga, monkeypatch, tmpdir):
    monkeypatch.setattr(socket, 'socket', mock.MagicMock(socket.socket))
    with open(str(tmpdir / 'socket'), 'w') as f:
        f.write('{"return": 87643}\n')
    f = open(str(tmpdir / 'socket'), 'r')
    socket.socket().makefile.return_value = f
    with ga as g:
        assert g.machine == 'testvm'
