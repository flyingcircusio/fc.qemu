import json
import os.path

import pytest
from io import StringIO
from codecs import encode

import fc.qemu.agent
from fc.qemu import util
from fc.qemu.agent import Agent
from fc.qemu.hazmat.qemu import Qemu

from ..conftest import get_log
from ..ellipsis import Ellipsis


@pytest.fixture(autouse=True)
def consul_timeouts(monkeypatch):
    monkeypatch.setattr(Qemu, "guestagent_timeout", 0.01)
    monkeypatch.setattr(Qemu, "qmp_timeout", 0.01)
    monkeypatch.setattr(Qemu, "thaw_retry_timeout", 0.1)


def test_no_events():
    stdin = StringIO("[]")
    Agent.handle_consul_event(stdin)
    assert util.log_data == []


def test_empty_event():
    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "", "Key": "node/test22"}]'
    )
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        "start-consul-events count=1",
        "handle-key key=node/test22",
        "ignore-key key=node/test22 reason=empty value",
        "finish-consul-events",
    ]


def test_no_key_event():
    stdin = StringIO('[{"ModifyIndex": 123, "Value": ""}]')
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        "start-consul-events count=1",
        "handle-key-failed exc_info=True key=None",
        "finish-handle-key key=None",
        "finish-consul-events",
    ]


@pytest.yield_fixture
def clean_config_test22(tmpdir):
    targets = [
        str(tmpdir / "etc/qemu/vm/test22.cfg"),
        str(tmpdir / "/etc/qemu/vm/.test22.cfg.staging"),
    ]
    for target in targets:
        if os.path.exists(target):
            os.unlink(target)
    yield
    for target in targets:
        if os.path.exists(target):
            os.unlink(target)


def test_qemu_config_change(clean_config_test22):
    util.test_log_options["show_methods"] = ["info"]
    cfg = {
        "classes": [
            "role::appserver",
            "role::backupclient",
            "role::generic",
            "role::postgresql90",
            "role::pspdf",
            "role::webproxy",
        ],
        "consul-generation": 128,
        "name": "test22",
        "parameters": {
            "ceph_id": "admin",
            "cores": 1,
            "directory_password": "1jmGYd3dddyjpFlLqg63RHie",
            "directory_ring": 1,
            "disk": 15,
            "environment": "staging",
            "id": 4097,
            "interfaces": {},
            "kvm_host": "host1",
            "location": "whq",
            "machine": "virtual",
            "memory": 512,
            "name": "test22",
            "online": False,
            "production": False,
            "profile": "generic",
            "rbd_pool": "rbd.ssd",
            "resource_group": "test",
            "resource_group_parent": "",
            "reverses": {},
            "service_description": "asdf",
            "servicing": True,
            "swap_size": 1073741824,
            "timezone": "Europe/Berlin",
            "tmp_size": 5368709120,
        },
    }
    test22 = encode(json.dumps(cfg),"base64")
    test22 = test22.replace("\n", "")

    stdin = StringIO(
        '[{"ModifyIndex": 128, "Value": "%s", "Key": "node/test22"}]' % test22
    )
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        "start-consul-events count=1",
        "processing-consul-event consul_event=node machine=test22",
        "launch-ensure cmd=['true', '-D', 'ensure', u'test22'] machine=test22",
        "finish-consul-events",
    ]

    # The test doesn't really active the config, so we need to mock this.
    util.log_data = []
    agent = Agent("test22")
    assert agent.has_new_config()
    assert not os.path.exists(agent.configfile)
    agent.activate_new_config()
    agent._update_from_enc()
    assert not agent.has_new_config()
    assert os.path.exists(agent.configfile)
    assert util.log_data == []

    # Applying the same config again doesn't cause another ensure run.
    stdin.seek(0)
    util.log_data = []
    Agent.handle_consul_event(stdin)

    assert util.log_data == [
        "start-consul-events count=1",
        "processing-consul-event consul_event=node machine=test22",
        "ignore-consul-event machine=test22 reason=config is unchanged",
        "finish-consul-events",
    ]

    # Changing the config does cause ensure to be called.
    util.log_data = []
    cfg["disk"] = 20
    test22 = encode(json.dumps(cfg),"base64")
    test22 = test22.replace("\n", "")

    stdin = StringIO(
        '[{"ModifyIndex": 135, "Value": "%s", "Key": "node/test22"}]' % test22
    )
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        "start-consul-events count=1",
        "processing-consul-event consul_event=node machine=test22",
        "launch-ensure cmd=['true', '-D', 'ensure', u'test22'] machine=test22",
        "finish-consul-events",
    ]


def test_qemu_config_change_physical():
    util.test_log_options["show_events"] = ["consul"]

    test22 = encode(json.dumps(
        {
            "classes": [
                "role::appserver",
                "role::backupclient",
                "role::generic",
                "role::postgresql90",
                "role::pspdf",
                "role::webproxy",
            ],
            "name": "test22",
            "parameters": {
                "ceph_id": "admin",
                "cores": 1,
                "directory_password": "1jmGYd3dddyjpFlLqg63RHie",
                "directory_ring": 1,
                "disk": 15,
                "environment": "staging",
                "id": 4097,
                "interfaces": {},
                "kvm_host": "host1",
                "location": "whq",
                "machine": "physical",
                "memory": 512,
                "name": "test22",
                "online": False,
                "production": False,
                "profile": "generic",
                "rbd_pool": "rbd.ssd",
                "resource_group": "test",
                "resource_group_parent": "",
                "reverses": {},
                "service_description": "asdf",
                "servicing": True,
                "swap_size": 1073741824,
                "timezone": "Europe/Berlin",
                "tmp_size": 5368709120,
            },
        }
    ), "base64")
    test22 = test22.replace("\n", "")

    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", "Key": "node/test22"}]' % test22
    )
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        "start-consul-events count=1",
        "ignore-consul-event machine=test22 reason=is a physical machine",
        "finish-consul-events",
    ]


@pytest.mark.timeout(60)
@pytest.mark.live
def test_snapshot_online_vm(vm):
    util.test_log_options["show_events"] = [
        "consul",
        "snapshot",
        "thaw",
        "freeze",
        "retry",
        "fail",
    ]

    vm.ensure_online_local()
    vm.qemu.qmp.close()
    get_log()

    snapshot = json.dumps({"vm": "simplevm", "snapshot": "backy-1234"})
    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", '
        '"Key": "snapshot/7468743"}]' % encode(snapshot, "base64").strip()
    )
    Agent.handle_consul_event(stdin)
    assert (
        Ellipsis(
            """\
start-consul-events count=1
snapshot machine=simplevm snapshot=backy-1234
snapshot-create machine=simplevm name=backy-1234
freeze machine=simplevm volume=root
freeze-failed action=continue machine=simplevm reason=Unable to sync with guest agent after 10 tries.
snapshot-ignore machine=simplevm reason=not frozen
ensure-thawed machine=simplevm volume=root
guest-fsfreeze-thaw-failed exc_info=True machine=simplevm subsystem=qemu
ensure-thawed-failed machine=simplevm reason=Unable to sync with guest agent after 10 tries.
handle-key-failed exc_info=True key=snapshot/7468743
finish-consul-events"""
        )
        == get_log()
    )


def test_snapshot_nonexisting_vm():
    util.test_log_options["show_events"] = ["consul", "unknown", "snapshot"]

    get_log()

    snapshot = json.dumps({"vm": "test77", "snapshot": "backy-1234"})
    snapshot = encode(snapshot, "base64").replace("\n", "")

    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", '
        '"Key": "snapshot/7468743"}]' % snapshot
    )
    Agent.handle_consul_event(stdin)
    assert (
        get_log()
        == """\
start-consul-events count=1
unknown-vm machine=test77
snapshot-ignore machine=test77 reason=failed loading config snapshot=backy-1234
finish-consul-events"""
    )


@pytest.mark.live
@pytest.mark.timeout(60)
def test_snapshot_offline_vm(vm):
    util.test_log_options["show_events"] = ["consul", "snapshot"]

    vm.enc["parameters"]["kvm_host"] = "foobar"
    vm.stage_new_config()
    vm.activate_new_config()

    vm.ceph.ensure_root_volume()
    vm.ensure_offline()
    get_log()

    snapshot = json.dumps({"vm": "simplevm", "snapshot": "backy-1234"})
    snapshot = encode(snapshot, "base64").replace("\n", "")

    stdin = StringIO(
        '[{"ModifyIndex": 123, "Value": "%s", '
        '"Key": "snapshot/7468743"}]' % snapshot
    )
    Agent.handle_consul_event(stdin)
    assert (
        get_log()
        == """\
start-consul-events count=1
snapshot machine=simplevm snapshot=backy-1234
snapshot expected=VM running machine=simplevm
finish-consul-events"""
    )


def test_multiple_events():
    util.test_log_options["show_events"] = ["consul", "handle-key"]

    test22 = encode(json.dumps(
        {
            "classes": [
                "role::appserver",
                "role::backupclient",
                "role::generic",
                "role::postgresql90",
                "role::pspdf",
                "role::webproxy",
            ],
            "name": "test22",
            "parameters": {
                "ceph_id": "admin",
                "cores": 1,
                "directory_password": "1jmGYd3dddyjpFlLqg63RHie",
                "directory_ring": 1,
                "disk": 15,
                "environment": "staging",
                "id": 4097,
                "interfaces": {},
                "kvm_host": "host1",
                "location": "whq",
                "machine": "physical",
                "memory": 512,
                "name": "test22",
                "online": False,
                "production": False,
                "profile": "generic",
                "rbd_pool": "rbd.ssd",
                "resource_group": "test",
                "resource_group_parent": "",
                "reverses": {},
                "service_description": "asdf",
                "servicing": True,
                "swap_size": 1073741824,
                "timezone": "Europe/Berlin",
                "tmp_size": 5368709120,
            },
        }
    ), "base64")
    test22 = test22.replace("\n", "")

    stdin = StringIO(
        '[{{"ModifyIndex": 123, "Value": "{0}", "Key": "node/test22"}},'
        '{{"ModifyIndex": 123, "Value": "{0}", "Key": "node/test22"}},'
        '{{"ModifyIndex": 123, "Value": "{0}", "Key": "node/test22"}},'
        '{{"ModifyIndex": 123, "Value": "{0}", "Key": "node/test22"}}]'.format(
            test22
        )
    )
    Agent.handle_consul_event(stdin)
    assert util.log_data == [
        "start-consul-events count=4",
        "handle-key key=node/test22",
        "ignore-consul-event machine=test22 reason=is a physical machine",
        "finish-handle-key key=node/test22",
        "handle-key key=node/test22",
        "ignore-consul-event machine=test22 reason=is a physical machine",
        "finish-handle-key key=node/test22",
        "handle-key key=node/test22",
        "ignore-consul-event machine=test22 reason=is a physical machine",
        "finish-handle-key key=node/test22",
        "handle-key key=node/test22",
        "ignore-consul-event machine=test22 reason=is a physical machine",
        "finish-handle-key key=node/test22",
        "finish-consul-events",
    ]
