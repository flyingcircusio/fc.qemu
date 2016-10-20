import json
import random
import socket
from ..util import log


class ClientError(RuntimeError):
    pass


class GuestAgent(object):
    """Wraps qemu guest agent wire protocol."""

    def __init__(self, machine, timeout=1):
        self.machine = machine
        self.file = None
        self.client = None
        self.timeout = timeout

    def read(self):
        """Reads single response from the GA and returns the result.

        Blocks and runs into a timeout if no response is available.
        """
        result = json.loads(self.file.readline())
        if 'error' in result:
                raise ClientError(result)
        return result['return']

    def cmd(self, cmd, **args):
        """Issues GA command and returns the result."""
        self.client.send(json.dumps({"execute": cmd, "arguments": args}))
        return self.read()

    def sync(self):
        """Ensures that request and response are in order."""
        sync_id = random.randint(0, 0xffff)
        n = 0
        result = self.cmd('guest-sync', id=sync_id)
        while n < 3:
            if result == sync_id:
                return
            log.error('incorrect-sync-id', expected=sync_id, got=result)
            n += 1
            result = self.read()
        raise ClientError('Unable to sync with guest agent after {} tries.'.
                          format(n))

    def __enter__(self):
        self.client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.client.settimeout(self.timeout)
        self.client.connect('/run/qemu.{}.gqa.sock'.format(self.machine))
        self.file = self.client.makefile()
        self.sync()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.file.close()
        self.client.close()
