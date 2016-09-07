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
    util.log_data = []

    def test_logger(logger, method_name, event):
        result = []
        for key in sorted(event):
            result.append('{}={}'.format(key, event[key]))
        util.log_data.append(' '.join(result))
        raise structlog.DropEvent

    structlog.configure(
        processors=[test_logger])


@pytest.fixture(autouse=True)
def reset_structlog(setup_structlog):
    from . import util
    util.log_data = []


def pytest_assertrepr_compare(op, left, right):
    if left.__class__.__name__ == 'Ellipsis':
        return left.compare(right).diff
    elif right.__class__.__name__ == 'Ellipsis':
        return right.compare(left).diff
