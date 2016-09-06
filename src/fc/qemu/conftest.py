import pytest
import structlog


def pytest_collectstart(collector):
    # sys.modules['rados'] = mock.Mock()
    # sys.modules['rbd'] = mock.Mock()
    from fc.qemu.sysconfig import sysconfig
    sysconfig.load_system_config()


@pytest.fixture(scope='session')
def setup_structlog():
    from . import util
    util.log = []

    def test_logger(logger, method_name, event):
        util.log.append((method_name, event))

    structlog.configure(
        processors=[test_logger])


@pytest.fixture(autouse=True)
def reset_structlog(setup_structlog):
    from . import util
    util.log.log = []
