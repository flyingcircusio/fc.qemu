import datetime
import hashlib
import hmac
import json
import socket
import uuid
from pathlib import Path
import fc.qemu.util
import rfc8785
from websockets.sync.client import connect


class AramakiBeaconSender:
    def __init__(self, status: dict):
        # todo: url
        self.message = None
        self.status = status

    def construct_message(self):
        now = fc.qemu.util.now()
        self.message = {
            "@context": "https://flyingcircus.io/ns/aramaki",
            "@version": 1,
            "@signature": {"alg": "HS256"},
            "@principal": socket.gethostname(),
            "@type": "vm.status",
            "@issued": now.isoformat(),
            "@expiry": (now + datetime.timedelta(hours=1)).isoformat(),
            "@id": uuid.uuid4().hex,
        }
        assert not set(self.status).issubset(set(self.message))  # xxx prohibit @
        self.message.update(self.status)

    def sign_message(self):
        enc = json.loads(Path("/etc/nixos/enc.json").read_text())
        secret_salt = enc["parameters"]["secret_salt"].encode("ascii")

        signature = hmac.new(
            secret_salt, rfc8785.dumps(self.message), hashlib.sha256
        ).hexdigest()

        self.message["@signature"]["signature"] = signature

    def send(self):
        self.construct_message()
        self.sign_message()

        with connect(
                "wss://directory.af3767cd.largo01.fcdev.fcio.net/aramaki"
        ) as websocket:
            websocket.send(json.dumps(self.message))
