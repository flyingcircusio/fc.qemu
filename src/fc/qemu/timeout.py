import time

from structlog import BoundLogger

from fc.qemu.logging import NULL_LOGGER


class TimeoutError(RuntimeError):
    pass


class TimeOut(object):
    _now = staticmethod(time.time)

    log: BoundLogger

    def __init__(
        self,
        timeout: float,
        interval: float = 1,
        raise_on_timeout: bool = False,
        log: BoundLogger = NULL_LOGGER,
    ):
        self.cutoff = self._now() + timeout
        self.interval = interval
        self.timed_out = False
        self.first = True
        self.raise_on_timeout = raise_on_timeout
        self.log = log

    @property
    def remaining(self) -> int:
        return int(self.cutoff - self._now())

    def tick(self) -> bool:
        """Perform a `tick` for this timeout.

        Returns True if we should keep going or False if not.

        Instead of returning False this can raise an exception
        if raise_on_timeout is set.

        """
        remaining = self.remaining  # make atomic within this function
        self.timed_out = remaining <= 0

        if self.timed_out:
            self.log.error(
                "timeout", interval=int(self.interval), remaining=remaining
            )
            if self.raise_on_timeout:
                raise TimeoutError()
            else:
                return False

        if self.first:
            self.first = False
        else:
            self.log.debug(
                "waiting", interval=int(self.interval), remaining=remaining
            )
            time.sleep(self.interval)

        return True
