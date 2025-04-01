import copy
import datetime
import hashlib
import hmac
import json
import socket
import uuid
from pathlib import Path

import rfc8785
from websockets.sync.client import connect

import fc.qemu.util


def sign_message(message):
    # XXX keep state somewhere else
    enc = json.loads(Path("/etc/nixos/enc.json").read_text())
    secret_salt = enc["parameters"]["secret_salt"].encode("ascii")

    signature = hmac.new(
        secret_salt, rfc8785.dumps(message), hashlib.sha256
    ).hexdigest()
    message["@signature"]["signature"] = signature


def prepare_message(message):
    now = fc.qemu.util.now().astimezone(datetime.UTC)
    message_template = {
        "@context": "https://flyingcircus.io/ns/aramaki",
        "@version": 1,
        "@signature": {"alg": "HS256"},
        "@principal": socket.gethostname(),
        "@issued": now.isoformat(),
        "@expiry": (now + datetime.timedelta(hours=1)).isoformat(),
        "@id": uuid.uuid4().hex,
    }
    message = copy.deepcopy(message)
    message.update(message_template)
    sign_message(message)
    return json.dumps(message)


class AramakiBeaconSender:
    def __init__(self, status: dict):
        self.message = None
        self.status = status
        self.status["@type"] = "vm.status"

    def construct_message(self):
        self.message = prepare_message(self.status)

    def send(self):
        # todo: make url configurable
        with connect(
            "wss://directory.b82f1c635cc.largo01.fcdev.fcio.net/aramaki"
        ) as websocket:
            websocket.send(self.message)
