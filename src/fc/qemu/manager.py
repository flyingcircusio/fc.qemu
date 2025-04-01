import asyncio
import copy
import datetime
import hashlib
import hmac
import json
import socket
import time
from collections import deque
from pathlib import Path

import rfc8785
from websockets.asyncio.client import connect

from .aramaki import prepare_message
from .util import log


class MessageReplaySet:
    ids: set[str]
    ages: deque[
        tuple[float, str]
    ]  # monotonic increasing timestamp from left to right

    TIMEOUT = 60 * 60 + 5 * 60  # 1h 5m

    def __init__(self):
        self.ids = set()
        self.ages = deque()
        self.last_expire = time.time()

    def check(self, id: str):
        if self.last_expire < time.time() - 60:
            self.expire()
        if id in self.ids:
            raise KeyError(f"ID already seen: {id}")

    def mark(self, id):
        self.ids.add(id)
        self.ages.append((time.time(), id))

    def expire(self):
        cutoff = time.time() - self.TIMEOUT
        while self.ages:
            t, id = self.ages.popleft()
            if t > cutoff:
                self.ages.appendleft((t, id))
                break
            self.ids.remove(id)


class Manager:
    principal: str

    def __init__(self):
        self.principal = socket.gethostname()
        self.known_messages = MessageReplaySet()
        enc = json.loads(Path("/etc/nixos/enc.json").read_text())
        self.secret_salt = enc["parameters"]["secret_salt"]

    async def run(self):
        loop = asyncio.get_running_loop()
        log.info("start-manager")
        while True:
            try:
                log.info("directory-connection", status="connecting")
                async with connect(
                    "wss://directory.b82f1c635cc.largo01.fcdev.fcio.net/aramaki"
                ) as websocket:
                    log.info("directory-connection", status="connected")

                    log.info("Sending subscription.")
                    subscription = {
                        "@type": "aramaki.subscription",
                        "@application": "fc.qemu.manager",
                        "matches": [
                            {
                                "@type": "vm.restart",
                                "scope": {"host": self.principal},
                            },
                            {
                                "@type": "vm.update",
                                "scope": {
                                    "location": "vagrant",
                                    "managing_rg": "services",
                                },
                            },
                        ],
                    }
                    await websocket.send(prepare_message(subscription))

                    # XXX error handling, e.g. if authentication has failed
                    # -> wait for a response

                    log.info("Waiting for messages ...")
                    async for message in websocket:
                        loop.create_task(self.process(message))
            except Exception:
                log.exception("unexpected-exception")
            # XXX exponential backoff / csmacd
            log.info("connection lost, backing off")
            time.sleep(5)

    async def process(self, message):
        log.info("got message")
        log.info(repr(message))
        message = json.loads(message)
        self.authenticate(message)
        if message["@type"] == "vm.restart":
            await handle_restart(message)

    def authenticate(self, message):
        """Authenticate whether this message has originated from the advertised
        principal.

        """
        advertised_principal = message["@principal"]
        assert advertised_principal == "@directory"

        expiry = datetime.datetime.fromisoformat(message["@expiry"])
        # Only accept messages that are not expired
        if expiry < datetime.datetime.now(datetime.UTC):
            raise Exception(
                f"message too old (expired at {message['@expiry']})"
            )

        self.known_messages.check(message["@id"])

        check_message = copy.deepcopy(message)
        advertised_signature = check_message["@signature"].pop("signature")

        signature = hmac.new(
            self.secret_salt.encode("ascii"),
            rfc8785.dumps(check_message),
            hashlib.sha256,
        ).hexdigest()

        if signature != advertised_signature:
            raise Exception(
                f"signature mismatch {signature} != {advertised_signature}"
            )

        # Once the message is authenticated, store the ID.
        # This prevents a DOS attack enumerating IDs
        self.known_messages.mark(message["@id"])


async def handle_restart(message):
    vm = message["machine"]
    print(f"Restarting VM: {vm}")
    # XXX the vm will only restart if it is running. this is intentional as we must not
    # mix up the restart command e.g. for a VM which has been ensured to be offline.
    # The alternative here would be to explicitly stop and ensure. but generally we should
    # only offer "restart" as an option in the UI if the VM is known to be running.
    await asyncio.create_subprocess_exec(
        "fc-qemu", "restart", message["machine"]
    )
