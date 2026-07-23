import unittest.mock
from subprocess import CalledProcessError

from pydantic import BaseModel, ValidationError

from fc.qemu.util import model_from_json_cmd, parse_export_format
from tests.conftest import get_log
from tests.test_agent import pytest


def test_export_format():
    data = """DEVNAME="test"
    UUID='a5370804-c9b1-4610-8f99-02a7841b8393'
    BLOCK_SIZE=512
    TYPE=xfs
    """
    assert parse_export_format(data) == {
        "BLOCK_SIZE": "512",
        "TYPE": "xfs",
        "UUID": "a5370804-c9b1-4610-8f99-02a7841b8393",
        "DEVNAME": "test",
    }


class DummyModel(BaseModel):
    value: int


def test_json_cmd_call_ok():
    log = unittest.mock.Mock()
    m = model_from_json_cmd(DummyModel, "echo '{\"value\": 1}'", log)
    assert isinstance(m, DummyModel)
    assert m.value == 1


def test_json_cmd_call_validation_error():
    log = unittest.mock.Mock()
    with pytest.raises(ValidationError) as e:
        model_from_json_cmd(DummyModel, "echo '{\"wrong\": 1}'", log)


def test_json_cmd_call_cmd_error():
    log = unittest.mock.Mock()
    with pytest.raises(CalledProcessError) as e:
        model_from_json_cmd(DummyModel, "notfound", log)


def test_log_is_emptied():
    from fc.qemu import util

    util.log_data.append("asdf")
    util.log_data.append("bsdf")
    assert get_log() == "asdf\nbsdf"
    assert get_log() == ""
    assert get_log() == ""
