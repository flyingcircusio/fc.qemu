import time


class TimeoutError(RuntimeError):
    pass


class TimeOut(object):

    def __init__(self, timeout, interval=1, raise_on_timeout=False):
        self.remaining = timeout
        self.cutoff = time.time() + timeout
        self.interval = interval
        self.timed_out = False
        self.first = True
        self.raise_on_timeout = raise_on_timeout

    def tick(self):
        """Perform a `tick` for this timeout.

        Returns True if we should keep going or False if not.

        Instead of returning False this can raise an exception
        if raise_on_timeout is set.

        """
        self.remaining = int(self.cutoff - time.time())
        self.timed_out = self.remaining <= 0

        if self.timed_out:
            if self.raise_on_timeout:
                raise TimeoutError()
            else:
                return False

        if self.first:
            self.first = False
        else:
            time.sleep(self.interval)

        return True
