from ..hazmat.qemu import Qemu
import os
import psutil
import pytest
import tempfile


@pytest.yield_fixture
def qemu_with_pidfile():
    q = Qemu(dict(name='testvm', id=1234))
    tf = tempfile.NamedTemporaryFile(prefix='test.', delete=False)
    q.pidfile = tf.name
    tf.write('{}\n'.format(os.getpid()))
    tf.close()
    try:
        yield q
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass


def test_proc_running(qemu_with_pidfile):
    assert isinstance(qemu_with_pidfile.proc(), psutil.Process)


def test_proc_not_running(qemu_with_pidfile):
    with open(qemu_with_pidfile.pidfile, 'w') as p:
        p.write('0\n')
    assert qemu_with_pidfile.proc() is None


def test_proc_no_pidilfe(qemu_with_pidfile):
    os.unlink(qemu_with_pidfile.pidfile)
    assert qemu_with_pidfile.proc() is None
