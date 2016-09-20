from fc.qemu.agent import Agent
from fc.qemu import util
from ..conftest import get_log
from StringIO import StringIO
import json


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


def test_qemu_config_change():
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
                         u'tmp_size': 5368709120}}).encode('base64')
    test22 = test22.replace('\n', '')

    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", "Key": "node/test22"}]' % test22)
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        'count=1 event=start-consul-events',
        'event=handle-key key=node/test22',
        'consul_event=node event=processing-consul-event machine=test22',
        'event=connect-rados machine=test22 subsystem=ceph',
        'event=open-pool machine=test22 pool=rbd.ssd subsystem=ceph',
        'action=none event=ensure-state found=offline machine=test22 '
        'wanted=offline',
        'ceph_lock=False event=check-state-consistency is_consistent=True '
        'machine=test22 proc=False qemu=False',
        'event=purge-run-files machine=test22 subsystem=qemu',
        'event=close-pool machine=test22 pool=rbd.ssd subsystem=ceph',
        'event=finish-consul-events']


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


def test_snapshot(vm):
    vm.ceph.ensure_root_volume()
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
event=open-pool machine=simplevm pool=rbd.ssd subsystem=ceph
event=snapshot machine=simplevm snapshot=backy-1234
event=create-snapshot machine=simplevm snapshot=backy-1234 subsystem=ceph \
volume=rbd.ssd/simplevm.root
event=close-pool machine=simplevm pool=rbd.ssd subsystem=ceph
event=finish-consul-events"""

    # A second time the snapshot is ignored but the request doesn't fail.
    stdin.seek(0)
    Agent.handle_consul_event(stdin)
    assert get_log() == """\
count=1 event=start-consul-events
event=handle-key key=snapshot/7468743
event=connect-rados machine=simplevm subsystem=ceph
event=open-pool machine=simplevm pool=rbd.ssd subsystem=ceph
event=snapshot machine=simplevm snapshot=backy-1234
event=snapshot-exists machine=simplevm snapshot=backy-1234
event=close-pool machine=simplevm pool=rbd.ssd subsystem=ceph
event=finish-consul-events"""


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


def test_snapshot_foreign_vm(vm):
    vm.enc['parameters']['kvm_host'] = 'foobar'
    vm.save_enc()
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
event=open-pool machine=simplevm pool=rbd.ssd subsystem=ceph
event=snapshot-ignore machine=simplevm reason=foreign host snapshot=backy-1234
event=close-pool machine=simplevm pool=rbd.ssd subsystem=ceph
event=finish-consul-events"""
