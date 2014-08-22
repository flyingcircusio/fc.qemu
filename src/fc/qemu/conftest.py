import sys
import mock


def pytest_collectstart(collector):
    sys.modules['rados'] = mock.Mock()
    sys.modules['rbd'] = mock.Mock()
