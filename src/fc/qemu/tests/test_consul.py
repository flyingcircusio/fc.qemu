from ..conftest import get_log
from ..ellipsis import Ellipsis
from fc.qemu import util
from fc.qemu.agent import Agent
from fc.qemu.hazmat.qemu import Qemu
from StringIO import StringIO
import json
import os.path
import pytest

# globally overriding timeouts since _handle_consul_event creates new
# Agent/Qemu/... instances itself.
Qemu.guestagent_timeout = .01
Qemu.qmp_timeout = .01
Qemu.thaw_retry_timeout = .1


def test_no_events():
    stdin = StringIO("[]")
    Agent.handle_consul_event(stdin)
    assert util.log_data == []


def test_empty_event():
    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "", "Key": "node/test22"}]')
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        'count=1 event=start-consul-events',
        'event=handle-key key=node/test22',
        'event=ignore-key key=node/test22 reason=empty value',
        'event=finish-consul-events']


def test_no_key_event():
    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": ""}]')
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        'count=1 event=start-consul-events',
        'event=handle-key-failed exc_info=True key=None',
        'event=finish-handle-key key=None',
        'event=finish-consul-events']


@pytest.yield_fixture
def clean_config_test22():
    targets = ['/etc/qemu/vm/test22.cfg', '/etc/qemu/vm/.test22.cfg.staging']
    for target in targets:
        if os.path.exists(target):
            os.unlink(target)
    yield
    for target in targets:
        if os.path.exists(target):
            os.unlink(target)


def test_qemu_config_change(clean_config_test22):
    util.test_log_options['show_methods'] = ['info']
    cfg = {u'classes': [u'role::appserver',
                        u'role::backupclient',
                        u'role::generic',
                        u'role::postgresql90',
                        u'role::pspdf',
                        u'role::webproxy'],
           u'consul-generation': 128,
           u'name': u'test22',
           u'parameters': {u'ceph_id': u'admin',
                           u'cores': 1,
                           u'directory_password': u'1jmGYd3dddyjpFlLqg63RHie',
                           u'directory_ring': 1,
                           u'disk': 15,
                           u'environment': u'staging',
                           u'id': 4097,
                           u'interfaces': {},
                           u'kvm_host': u'host1',
                           u'location': u'whq',
                           u'machine': u'virtual',
                           u'memory': 512,
                           u'name': u'test22',
                           u'online': False,
                           u'production': False,
                           u'profile': u'generic',
                           u'rbd_pool': u'rbd.ssd',
                           u'resource_group': u'test',
                           u'resource_group_parent': u'',
                           u'reverses': {},
                           u'service_description': u'asdf',
                           u'servicing': True,
                           u'swap_size': 1073741824,
                           u'timezone': u'Europe/Berlin',
                           u'tmp_size': 5368709120}}
    test22 = json.dumps(cfg).encode('base64')
    test22 = test22.replace('\n', '')

    stdin = StringIO(
        '[{"ModifyIndex": 128, "Value": "%s", "Key": "node/test22"}]' % test22)
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        'count=1 event=start-consul-events',
        'consul_event=node event=processing-consul-event machine=test22',
        'event=launch-ensure machine=test22',
        'event=finish-consul-events']

    # Applying the same config again doesn't
    stdin.seek(0)
    util.log_data = []
    Agent.handle_consul_event(stdin)

    assert util.log_data == ['count=1 event=start-consul-events',
        'consul_event=node event=processing-consul-event machine=test22',
        'event=ignore-consul-event machine=test22 reason=config is unchanged',
        'event=finish-consul-events']

    # Changing the config does cause ensure to be called.
    util.log_data = []
    cfg['disk'] = 20
    test22 = json.dumps(cfg).encode('base64')
    test22 = test22.replace('\n', '')

    stdin = StringIO(
        '[{"ModifyIndex": 135, "Value": "%s", "Key": "node/test22"}]' % test22)
    Agent.handle_consul_event(stdin)
    assert util.log_data == ['count=1 event=start-consul-events',
        'consul_event=node event=processing-consul-event machine=test22',
        'event=launch-ensure machine=test22',
        'event=finish-consul-events']


def test_qemu_config_change_physical():
    util.test_log_options['show_events'] = ['consul']

    test22 = json.dumps(
        {u'classes': [u'role::appserver',
                      u'role::backupclient',
                      u'role::generic',
                      u'role::postgresql90',
                      u'role::pspdf',
                      u'role::webproxy'],
         u'name': u'test22',
         u'parameters': {u'ceph_id': u'admin',
                         u'cores': 1,
                         u'directory_password': u'1jmGYd3dddyjpFlLqg63RHie',
                         u'directory_ring': 1,
                         u'disk': 15,
                         u'environment': u'staging',
                         u'id': 4097,
                         u'interfaces': {},
                         u'kvm_host': u'host1',
                         u'location': u'whq',
                         u'machine': u'physical',
                         u'memory': 512,
                         u'name': u'test22',
                         u'online': False,
                         u'production': False,
                         u'profile': u'generic',
                         u'rbd_pool': u'rbd.ssd',
                         u'resource_group': u'test',
                         u'resource_group_parent': u'',
                         u'reverses': {},
                         u'service_description': u'asdf',
                         u'servicing': True,
                         u'swap_size': 1073741824,
                         u'timezone': u'Europe/Berlin',
                         u'tmp_size': 5368709120}}).encode('base64')
    test22 = test22.replace('\n', '')

    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", "Key": "node/test22"}]' % test22)
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        'count=1 event=start-consul-events',
        'event=ignore-consul-event machine=test22 reason=is a physical '
        'machine',
        'event=finish-consul-events']


def test_snapshot_online_vm(vm):
    util.test_log_options['show_events'] = [
        'consul', 'snapshot', 'thaw', 'freeze', 'retry', 'fail']

    vm.ensure_online_local()
    vm.qemu.qmp.close()
    get_log()

    snapshot = json.dumps({'vm': 'simplevm', 'snapshot': 'backy-1234'})
    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", '
        '"Key": "snapshot/7468743"}]' % snapshot.encode('base64').strip())
    Agent.handle_consul_event(stdin)
    assert Ellipsis("""\
count=1 event=start-consul-events
event=snapshot machine=simplevm snapshot=backy-1234
event=snapshot-create machine=simplevm name=backy-1234
event=freeze machine=simplevm volume=root
action=continue event=freeze-failed machine=simplevm reason=Unable to sync \
with guest agent after 20 tries.
event=snapshot-ignore machine=simplevm reason=not frozen
event=thaw machine=simplevm volume=root
action=retry event=thaw-failed machine=simplevm reason=Unable to sync with \
guest agent after 20 tries.
action=continue event=thaw-failed machine=simplevm reason=Unable to sync with \
guest agent after 20 tries.
event=handle-key-failed exc_info=True key=snapshot/7468743
event=finish-consul-events""") == get_log()


def test_snapshot_nonexisting_vm():
    util.test_log_options['show_events'] = [
        'consul', 'unknown', 'snapshot']

    get_log()

    snapshot = json.dumps({'vm': 'test77', 'snapshot': 'backy-1234'})
    snapshot = snapshot.encode('base64').replace('\n', '')

    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", '
        '"Key": "snapshot/7468743"}]' % snapshot)
    Agent.handle_consul_event(stdin)
    assert get_log() == """\
count=1 event=start-consul-events
event=unknown-vm machine=test77
event=snapshot-ignore machine=test77 reason=failed loading config \
snapshot=backy-1234
event=finish-consul-events"""


def test_snapshot_offline_vm(vm):
    util.test_log_options['show_events'] = [
        'consul', 'snapshot']

    vm.enc['parameters']['kvm_host'] = 'foobar'
    vm.stage_new_config()
    vm.activate_new_config()

    vm.ceph.ensure_root_volume()
    vm.ensure_offline()
    get_log()

    snapshot = json.dumps({'vm': 'simplevm', 'snapshot': 'backy-1234'})
    snapshot = snapshot.encode('base64').replace('\n', '')

    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", '
        '"Key": "snapshot/7468743"}]' % snapshot)
    Agent.handle_consul_event(stdin)
    assert get_log() == """\
count=1 event=start-consul-events
event=snapshot machine=simplevm snapshot=backy-1234
event=snapshot-create machine=simplevm name=backy-1234
event=snapshot-ignore machine=simplevm reason=not frozen
event=finish-consul-events"""


def test_multiple_events():
    util.test_log_options['show_events'] = [
        'consul', 'handle-key']

    test22 = json.dumps(
        {u'classes': [u'role::appserver',
                      u'role::backupclient',
                      u'role::generic',
                      u'role::postgresql90',
                      u'role::pspdf',
                      u'role::webproxy'],
         u'name': u'test22',
         u'parameters': {u'ceph_id': u'admin',
                         u'cores': 1,
                         u'directory_password': u'1jmGYd3dddyjpFlLqg63RHie',
                         u'directory_ring': 1,
                         u'disk': 15,
                         u'environment': u'staging',
                         u'id': 4097,
                         u'interfaces': {},
                         u'kvm_host': u'host1',
                         u'location': u'whq',
                         u'machine': u'physical',
                         u'memory': 512,
                         u'name': u'test22',
                         u'online': False,
                         u'production': False,
                         u'profile': u'generic',
                         u'rbd_pool': u'rbd.ssd',
                         u'resource_group': u'test',
                         u'resource_group_parent': u'',
                         u'reverses': {},
                         u'service_description': u'asdf',
                         u'servicing': True,
                         u'swap_size': 1073741824,
                         u'timezone': u'Europe/Berlin',
                         u'tmp_size': 5368709120}}).encode('base64')
    test22 = test22.replace('\n', '')

    stdin = StringIO(
        '[{{"ModifyIndex": 123, "Value": "{0}", "Key": "node/test22"}},'
        '{{"ModifyIndex": 123, "Value": "{0}", "Key": "node/test22"}},'
        '{{"ModifyIndex": 123, "Value": "{0}", "Key": "node/test22"}},'
        '{{"ModifyIndex": 123, "Value": "{0}", "Key": "node/test22"}}]'
        .format(test22))
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        'count=4 event=start-consul-events',
        'event=handle-key key=node/test22',
        'event=ignore-consul-event machine=test22 reason=is a physical machine',
        'event=finish-handle-key key=node/test22',
        'event=handle-key key=node/test22',
        'event=ignore-consul-event machine=test22 reason=is a physical machine',
        'event=finish-handle-key key=node/test22',
        'event=handle-key key=node/test22',
        'event=ignore-consul-event machine=test22 reason=is a physical machine',
        'event=finish-handle-key key=node/test22',
        'event=handle-key key=node/test22',
        'event=ignore-consul-event machine=test22 reason=is a physical machine',
        'event=finish-handle-key key=node/test22',
        'event=finish-consul-events']
