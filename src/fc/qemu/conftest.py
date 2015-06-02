import pytest
import logging


def pytest_collectstart(collector):
    # sys.modules['rados'] = mock.Mock()
    # sys.modules['rbd'] = mock.Mock()
    import fc.qemu.main
    fc.qemu.main.load_system_config()
