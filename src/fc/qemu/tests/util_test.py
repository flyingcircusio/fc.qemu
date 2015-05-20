from ..util import rewrite
import os
import pytest
import tempfile


@pytest.yield_fixture
def tmp():
    tf = tempfile.NamedTemporaryFile(mode='w', delete=False)
    yield tf
    try:
        os.unlink(tf.name)
    except OSError:  # pragma: nocover
        pass


def test_rewrite(tmp):
    tmp.write('old content\n')
    tmp.flush()
    old_ino = os.fstat(tmp.fileno()).st_ino
    with rewrite(tmp.name) as f:
        f.write('new content\n')
    with open(tmp.name) as f:
        assert 'new content\n' == f.read()
    assert old_ino != os.stat(tmp.name).st_ino


def test_rewrite_should_ignore_idempotent_rewrite(tmp):
    tmp.write('old content\n')
    tmp.flush()
    old_ino = os.fstat(tmp.fileno()).st_ino
    with rewrite(tmp.name) as f:
        f.write('old content\n')
    assert old_ino == os.stat(tmp.name).st_ino


def test_rewrite_ignore_deleted(tmp):
    tmp.write('old content\n')
    tmp.flush()
    old_ino = os.fstat(tmp.fileno()).st_ino
    with rewrite(tmp.name) as f:
        os.unlink(f.name)
    with open(tmp.name) as f:
        assert 'old content\n' == f.read()
    assert old_ino == os.stat(tmp.name).st_ino


def test_rewrite_should_create_nonexisting_file(tmp):
    os.unlink(tmp.name)
    with rewrite(tmp.name) as f:
        f.write('new file\n')
    with open(tmp.name) as f:
        assert 'new file\n' == f.read()
