import fcntl
import json
import random
import socket

from ..util import log


class ClientError(RuntimeError):
    pass


SYNC_TIMEOUT = 30


class GuestAgent(object):
    """Wraps qemu guest agent wire protocol."""

    def __init__(self, machine, timeout, client_factory=socket.socket):
        self.machine = machine
        self.timeout = timeout
        self.log = log.bind(machine=machine, subsystem="qemu/guestagent")
        self.file = None

        self.client_factory = client_factory
        self.client = None

    def read(self, unwrap=True):
        """Reads single response from the GA and returns the result.

        Blocks and runs into a timeout if no response is available.
        """
        try:
            result = self.file.readline()
        except socket.timeout:
            self.log.debug("read-timeout")
            self.disconnect()
            raise
        result = json.loads(result)
        self.log.debug("read", result=result)
        if "error" in result:
            raise ClientError(result)
        if unwrap:
            # Some commands, like guest-ping do not return a result. But we do
            # not want to silently swallow errors if a return value is missing
            # but expected.
            return result["return"]
        # However, to ensure that things like the guest-ping did receive a
        # proper result structure (e.g. {}) we do return it, so the command
        # can detect whether everything is as expected. We explicitly do not
        # just silently return `None` here.
        return result

    def cmd(self, cmd, timeout=None, fire_and_forget=False, **args):
        """Issues GA command and returns the result.

        All **args need to be serialisable to JSON, that implies that `bytes`
        are *not* valid.

        """
        self.connect()
        message = json.dumps({"execute": cmd, "arguments": args})
        message = message.encode("utf-8")
        self.log.debug("send", message=message)
        self.client.send(message)
        if not fire_and_forget:
            self.client.settimeout(timeout or self.timeout)
            return self.read(unwrap=(cmd != "guest-ping"))

    def sync(self):
        """Ensures that request and response are in order."""

        # Phase 1: ensure a low-level thaw command. This is an emergency safety
        # belt. We really do not want the VM to be accidentally stuck in a
        # frozen state.
        self.log.debug("sync-gratuitous-thaw")
        self.client.send(
            json.dumps({"execute": "guest-fsfreeze-thaw"}).encode("utf-8")
        )

        # Phase 2: clear the connection buffer from previous connections. We
        # set a very short timeout because the guest agent might still be
        # stuck trying to send a response to a client that went away. However,
        # we want to keep it as short as possible because this timeout will
        # always happen in the happy case which is most of the time.
        self.client.settimeout(1)
        self.log.debug("clear-buffer")
        try:
            while buffer := self.client.recv(4096):
                self.log.debug("found-buffer-garbage", buffer=buffer)
        except socket.timeout:
            self.log.debug("cleared-buffer")

        # Phase 3: ensure we see proper agent interactions. To be sure we
        # test this with two diagnostic calls. The timeout can be higher now
        # as we expect the agent to actually have to respond to us.
        sync_id = random.randint(0, 0xFFFF)
        result = self.cmd("guest-sync", timeout=SYNC_TIMEOUT, id=sync_id)

        self.log.debug("sync-response", expected=sync_id, got=result)
        if result == sync_id:
            return

        raise ClientError(
            f"Unable to sync with guest agent. Got invalid sync_id {sync_id}"
        )

    def connect(self):
        if self.client and self.file:
            return
        self.disconnect()
        self.client = self.client_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        self.client.connect("/run/qemu.{}.gqa.sock".format(self.machine))
        self.file = self.client.makefile()
        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
        self.sync()

    def disconnect(self):
        if self.file or self.client:
            self.log.debug("disconnect")
        try:
            if self.file:
                self.file.close()
        except Exception:
            pass
        finally:
            self.file = None
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        finally:
            self.client = None
