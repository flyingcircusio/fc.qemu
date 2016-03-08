def pytest_collectstart(collector):
    # sys.modules['rados'] = mock.Mock()
    # sys.modules['rbd'] = mock.Mock()
    from fc.qemu.sysconfig import sysconfig
    sysconfig.load_system_config()
