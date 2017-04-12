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
        'event=consul-handle-event exc_info=True',
        'event=finish-consul-events']


@pytest.yield_fixture
def clean_config_test22():
    target = '/etc/qemu/vm/test22.cfg'
    if os.path.exists(target):
        os.unlink(target)
    yield
    if os.path.exists(target):
        os.unlink(target)


def test_qemu_config_change(clean_config_test22):
    cfg = {u'classes': [u'role::appserver',
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
        '[{"ModifyIndex": 123, "Value": "%s", "Key": "node/test22"}]' % test22)
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        'count=1 event=start-consul-events',
        'event=handle-key key=node/test22',
        'consul_event=node event=processing-consul-event machine=test22',
        'event=connect-rados machine=test22 subsystem=ceph',
        'action=none event=ensure-state found=offline machine=test22 '
        'wanted=offline',
        'ceph_lock=False event=check-state-consistency is_consistent=True '
        'machine=test22 proc=False qemu=False',
        'event=finish-consul-events',
    ]

    # Applying the same config again doesn't
    stdin.seek(0)
    util.log_data = []
    Agent.handle_consul_event(stdin)

    assert util.log_data == [
        'count=1 event=start-consul-events',
        'event=handle-key key=node/test22',
        'consul_event=node event=processing-consul-event machine=test22',
        'event=ignore-consul-event machine=test22 reason=config is unchanged',
        'event=finish-consul-events']

    # Changing the config does cause ensure to be called.
    util.log_data = []
    cfg['disk'] = 20
    test22 = json.dumps(cfg).encode('base64')
    test22 = test22.replace('\n', '')

    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", "Key": "node/test22"}]' % test22)
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        'count=1 event=start-consul-events',
        'event=handle-key key=node/test22',
        'consul_event=node event=processing-consul-event machine=test22',
        'event=connect-rados machine=test22 subsystem=ceph',
        'action=none event=ensure-state found=offline machine=test22 '
        'wanted=offline',
        'ceph_lock=False event=check-state-consistency is_consistent=True '
        'machine=test22 proc=False qemu=False',
        'event=finish-consul-events',
    ]


def test_qemu_config_change_physical():
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
        'event=handle-key key=node/test22',
        'event=ignore-consul-event machine=test22 reason=is a physical '
        'machine',
        'event=finish-consul-events']


def test_snapshot_online_vm(vm):

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
event=handle-key key=snapshot/7468743
event=connect-rados machine=simplevm subsystem=ceph
event=snapshot machine=simplevm snapshot=backy-1234
event=snapshot-create machine=simplevm name=backy-1234
arguments={} event=qmp_capabilities id=None machine=simplevm subsystem=qemu/qmp
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
event=freeze machine=simplevm volume=root
event=incorrect-sync-id expected=... got=None machine=simplevm tries=0
event=incorrect-sync-id expected=... got=None machine=simplevm tries=1
event=incorrect-sync-id expected=... got=None machine=simplevm tries=2
event=incorrect-sync-id expected=... got=None machine=simplevm tries=3
event=incorrect-sync-id expected=... got=None machine=simplevm tries=4
event=incorrect-sync-id expected=... got=None machine=simplevm tries=5
event=incorrect-sync-id expected=... got=None machine=simplevm tries=6
event=incorrect-sync-id expected=... got=None machine=simplevm tries=7
event=incorrect-sync-id expected=... got=None machine=simplevm tries=8
event=incorrect-sync-id expected=... got=None machine=simplevm tries=9
event=incorrect-sync-id expected=... got=None machine=simplevm tries=10
event=incorrect-sync-id expected=... got=None machine=simplevm tries=11
event=incorrect-sync-id expected=... got=None machine=simplevm tries=12
event=incorrect-sync-id expected=... got=None machine=simplevm tries=13
event=incorrect-sync-id expected=... got=None machine=simplevm tries=14
event=incorrect-sync-id expected=... got=None machine=simplevm tries=15
event=incorrect-sync-id expected=... got=None machine=simplevm tries=16
event=incorrect-sync-id expected=... got=None machine=simplevm tries=17
event=incorrect-sync-id expected=... got=None machine=simplevm tries=18
event=incorrect-sync-id expected=... got=None machine=simplevm tries=19
action=continue event=freeze-failed machine=simplevm reason=Unable to sync \
with guest agent after 20 tries.
event=snapshot-ignore machine=simplevm reason=not frozen
arguments={} event=query-status id=None machine=simplevm subsystem=qemu/qmp
event=thaw machine=simplevm volume=root
event=incorrect-sync-id expected=... got=None machine=simplevm tries=0
event=incorrect-sync-id expected=... got=None machine=simplevm tries=1
event=incorrect-sync-id expected=... got=None machine=simplevm tries=2
event=incorrect-sync-id expected=... got=None machine=simplevm tries=3
event=incorrect-sync-id expected=... got=None machine=simplevm tries=4
event=incorrect-sync-id expected=... got=None machine=simplevm tries=5
event=incorrect-sync-id expected=... got=None machine=simplevm tries=6
event=incorrect-sync-id expected=... got=None machine=simplevm tries=7
event=incorrect-sync-id expected=... got=None machine=simplevm tries=8
event=incorrect-sync-id expected=... got=None machine=simplevm tries=9
event=incorrect-sync-id expected=... got=None machine=simplevm tries=10
event=incorrect-sync-id expected=... got=None machine=simplevm tries=11
event=incorrect-sync-id expected=... got=None machine=simplevm tries=12
event=incorrect-sync-id expected=... got=None machine=simplevm tries=13
event=incorrect-sync-id expected=... got=None machine=simplevm tries=14
event=incorrect-sync-id expected=... got=None machine=simplevm tries=15
event=incorrect-sync-id expected=... got=None machine=simplevm tries=16
event=incorrect-sync-id expected=... got=None machine=simplevm tries=17
event=incorrect-sync-id expected=... got=None machine=simplevm tries=18
event=incorrect-sync-id expected=... got=None machine=simplevm tries=19
action=retry event=thaw-failed machine=simplevm reason=Unable to sync with \
guest agent after 20 tries.
event=incorrect-sync-id expected=... got=None machine=simplevm tries=0
event=incorrect-sync-id expected=... got=None machine=simplevm tries=1
event=incorrect-sync-id expected=... got=None machine=simplevm tries=2
event=incorrect-sync-id expected=... got=None machine=simplevm tries=3
event=incorrect-sync-id expected=... got=None machine=simplevm tries=4
event=incorrect-sync-id expected=... got=None machine=simplevm tries=5
event=incorrect-sync-id expected=... got=None machine=simplevm tries=6
event=incorrect-sync-id expected=... got=None machine=simplevm tries=7
event=incorrect-sync-id expected=... got=None machine=simplevm tries=8
event=incorrect-sync-id expected=... got=None machine=simplevm tries=9
event=incorrect-sync-id expected=... got=None machine=simplevm tries=10
event=incorrect-sync-id expected=... got=None machine=simplevm tries=11
event=incorrect-sync-id expected=... got=None machine=simplevm tries=12
event=incorrect-sync-id expected=... got=None machine=simplevm tries=13
event=incorrect-sync-id expected=... got=None machine=simplevm tries=14
event=incorrect-sync-id expected=... got=None machine=simplevm tries=15
event=incorrect-sync-id expected=... got=None machine=simplevm tries=16
event=incorrect-sync-id expected=... got=None machine=simplevm tries=17
event=incorrect-sync-id expected=... got=None machine=simplevm tries=18
event=incorrect-sync-id expected=... got=None machine=simplevm tries=19
action=continue event=thaw-failed machine=simplevm reason=Unable to sync with \
guest agent after 20 tries.
event=consul-handle-event exc_info=True
event=finish-consul-events""") == get_log()


def test_snapshot_nonexisting_vm():
    get_log()

    snapshot = json.dumps({'vm': 'test77', 'snapshot': 'backy-1234'})
    snapshot = snapshot.encode('base64').replace('\n', '')

    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", '
        '"Key": "snapshot/7468743"}]' % snapshot)
    Agent.handle_consul_event(stdin)
    assert get_log() == """\
count=1 event=start-consul-events
event=handle-key key=snapshot/7468743
event=unknown-vm machine=test77
event=snapshot-ignore machine=test77 reason=failed loading config \
snapshot=backy-1234
event=finish-consul-events"""


def test_snapshot_offline_vm(vm):
    vm.enc['parameters']['kvm_host'] = 'foobar'
    vm.save_enc()
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
event=handle-key key=snapshot/7468743
event=connect-rados machine=simplevm subsystem=ceph
event=snapshot machine=simplevm snapshot=backy-1234
event=snapshot-create machine=simplevm name=backy-1234
event=snapshot-ignore machine=simplevm reason=not frozen
event=finish-consul-events"""


def test_multiple_events():
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
        'event=handle-key key=node/test22',
        'event=ignore-consul-event machine=test22 reason=is a physical machine',
        'event=handle-key key=node/test22',
        'event=ignore-consul-event machine=test22 reason=is a physical machine',
        'event=handle-key key=node/test22',
        'event=ignore-consul-event machine=test22 reason=is a physical machine',
        'event=finish-consul-events']
