from ..agent import Agent
import os
import pkg_resources
import pytest
import shutil


@pytest.yield_fixture
def simplevm_cfg():
    fixtures = pkg_resources.resource_filename(__name__, 'fixtures')
    shutil.copy(fixtures + '/simplevm.yaml', '/etc/qemu/vm/simplevm.cfg')
    yield 'simplevm'
    os.unlink('/etc/qemu/vm/simplevm.cfg')


def test_builtin_config_template(simplevm_cfg):
    a = Agent(simplevm_cfg)
    a.generate_config()
    assert 'type = "pc-q35-2.1"' in a.qemu.config


def test_userdefined_config_template(simplevm_cfg):
    with open('/etc/qemu/qemu.vm.cfg.in', 'w') as f:
        f.write('# user defined config template\n')
    try:
        a = Agent(simplevm_cfg)
        a.generate_config()
        assert 'user defined config template' in a.qemu.config
    finally:
        os.unlink('/etc/qemu/qemu.vm.cfg.in')
