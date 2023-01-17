"""Global helper functions and utilites for fc.qemu."""


import contextlib
import datetime
import os
import os.path
import subprocess
import sys
import time

from structlog import get_logger

MiB = 2**20
GiB = 2**30


log = get_logger()


# workaround for ValueError: can't have unbuffered text I/O
class FlushingStream(object):
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        self.stream.write(data)
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class ControlledRuntimeException(RuntimeError):
    """An exception that is used for flow control but doesn't have to be logged
    as it is handled properly inside.
    """


def parse_address(addr):
    if addr.startswith("["):
        host, port = addr[1:].split("]:")
    else:
        host, port = addr.split(":")
    return host, int(port)


def locate_live_service(consul, service_id):
    """Locate Consul service with at least one passing health check.

    It is an error if multiple live services with the same service name
    are found.
    """

    def passing(checks):
        return any(
            check["Status"] == "passing" for check in checks
        ) and not any(check["Status"] == "critical" for check in checks)

    live = [
        svc
        for svc in consul.health.service(service_id)
        if passing(svc["Checks"])
    ]
    if len(live) > 1:
        raise RuntimeError(
            "multiple services with passing checks found", service_id
        )
    return live[0]["Service"] if len(live) else None


def remove_empty_dirs(d):
    """Remove all empty directories from d up.

    Stops on the first non-empty directory.
    """
    while d != "/":
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


def cmd(cmdline, log, encoding="ascii", errors="replace"):
    """Execute cmdline with stdin closed to avoid questions on terminal"""
    prefix = cmdline.split()[0]
    args = " ".join(cmdline.split()[1:])
    log.debug(prefix, args=args)
    with open("/dev/null") as null:
        proc = subprocess.Popen(
            cmdline,
            shell=True,
            stdin=null,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding=encoding,
            errors=errors,
        )
        # This allows for more interactive logging and capturing
        # stdout in unit tests even if we get stuck.
        stdout = ""
        while True:
            line = proc.stdout.readline()
            if line:
                # This ensures we get partial output in case of test failures
                log.debug(os.path.basename(prefix), output_line=line)
                stdout += line
            else:
                break
    returncode = proc.wait()
    # Keep this here for compatibility with tests
    output = stdout.strip()
    log.debug(prefix, returncode=returncode)
    if returncode:
        log.warning(prefix, output=output)
        raise subprocess.CalledProcessError(returncode=returncode, cmd=cmdline)
    return output


@contextlib.contextmanager
def timeit(label):
    start = time.time()
    yield
    print(
        "run time for {}: {}".format(label, time.time() - start),
        file=sys.stderr,
    )


def today():
    return datetime.date.today()


def ensure_separate_cgroup():
    "Move this process to a separate fc-qemu cgroup."
    CGROUP = "/sys/fs/cgroup/fc-qemu"
    if not os.path.exists(CGROUP):
        try:
            os.mkdir(CGROUP)
        except OSError:
            if not os.path.isdir(CGROUP):
                raise
            # The directory exists now. We've run into a race condition.
            # Keep going.
    with open("{}/cgroup.procs".format(CGROUP), "w") as f:
        f.write(str(os.getpid()))
