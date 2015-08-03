import socket
import random
import json


class ClientError(RuntimeError):
    pass


class GuestAgent(object):

    def __init__(self, machine):
        self.machine = machine
        self.file = None
        self.client = None

    def __enter__(self):
        self.client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.client.settimeout(5)
        self.client.connect('/run/qemu.{}.gqa.sock'.format(self.machine))
        self.file = self.client.makefile()

        sync_id = random.randint(0, 2 ** 32 - 1)
        result = self.cmd('guest-sync', id=sync_id)
        if not result == sync_id:
            raise ClientError(
                "Did not sync successfully. "
                "Received incorrect sync id. Expected: {} Got: {}".
                format(sync_id, result))
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.file.close()
        self.client.close()

    def cmd(self, cmd, **args):
        self.client.send(json.dumps({"execute": cmd, "arguments": args}))
        result = json.loads(self.file.readline())
        if 'error' in result:
                raise ClientError(result)
        return result['return']
