import hashlib
import hmac
import json
import socket
from pathlib import Path

import rfc8785
from websockets.sync.client import connect

enc = json.loads(Path("/etc/nixos/enc.json").read_text())
secret_salt = enc["parameters"]["secret_salt"].encode("ascii")
principal = enc["name"]


def send_status_beacon(status):
    message = {
        "@context": "https://flyingcircus.io/ns/aramaki",
        "@version": 1,
        "@signature": {"alg": "HS256"},
        "@principal": socket.gethostname(),
        "@type": "vm.status",
    }

    assert not set(message).issubset(set(status))  # xxx prohibit @
    message.update(status)

    signature = hmac.new(
        secret_salt, rfc8785.dumps(message), hashlib.sha256
    ).hexdigest()

    message["@signature"]["signature"] = signature

    with connect(
        "wss://directory.b82f1c635cc.largo01.fcdev.fcio.net/aramaki"
    ) as websocket:
        websocket.send(json.dumps(message))

    # 🌐 https://api.b82f1c635cc.largo01.fcdev.fcio.net/
    # 🌐 https://auth.b82f1c635cc.largo01.fcdev.fcio.net/
    # 🌐 https://directory.b82f1c635cc.largo01.fcdev.fcio.net/
    # 🌐 https://my.b82f1c635cc.largo01.fcdev.fcio.net/
