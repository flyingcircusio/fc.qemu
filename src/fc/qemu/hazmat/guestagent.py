from ..util import log
import fcntl
import json
import random
import socket


class ClientError(RuntimeError):
    pass


class GuestAgent(object):
    """Wraps qemu guest agent wire protocol."""

    def __init__(self, machine, timeout):
        self.machine = machine
        self.timeout = timeout
        self.log = log.bind(machine=machine)
        self.file = None
        self.client = None

    def read(self):
        """Reads single response from the GA and returns the result.

        Blocks and runs into a timeout if no response is available.
        """
        result = json.loads(self.file.readline())
        if 'error' in result:
                raise ClientError(result)
        return result['return']

    def cmd(self, cmd, flush_ga_parser=False, timeout=None, **args):
        """Issues GA command and returns the result."""
        message = json.dumps({"execute": cmd, "arguments": args})
        if flush_ga_parser:
            # \xff is an invalid utf-8 character and recommended to safely
            # ensure that the parser of the guest agent at the other end
            # is reset to a known state. This is recommended for sync.
            # http://wiki.qemu-project.org/index.php/Features/GuestAgent#guest-sync
            message = b'\xff' + message
        timeout = timeout or self.timeout
        # Allow setting temporary timeouts for operations that are known to be
        # slow.
        self.client.settimeout(timeout)
        self.client.send(message)
        return self.read()

    def sync(self):
        """Ensures that request and response are in order."""
        sync_id = random.randint(0, 0xffff)
        n = 0
        try:
            result = self.cmd('guest-sync', id=sync_id, flush_ga_parser=True)
        except ClientError:
            # we tripped a client error as we caused the guest agent to notice
            # invalid json, which in turn triggers an error response
            result = self.read()
        except socket.error:
            # Maybe a timeout. Keep trying a little bit harder.
            result = None
        while n < 20:
            if result == sync_id:
                return
            self.log.error('incorrect-sync-id', expected=sync_id, got=result,
                           tries=n)
            n += 1
            try:
                result = self.read()
            except (ClientError, socket.error):
                # we tripped a client error later than right now. There may
                # have been a response still in the queue.
                pass

        raise ClientError('Unable to sync with guest agent after {} tries.'.
                          format(n))

    def __enter__(self):
        self.client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.client.settimeout(self.timeout)
        self.client.connect('/run/qemu.{}.gqa.sock'.format(self.machine))
        self.file = self.client.makefile()
        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
        self.sync()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.file.close()
        self.client.close()
