"""Global helper functions and utilites for fc.qemu."""

import contextlib
import datetime
import os
import os.path
import subprocess
import sys
import time
from typing import IO, Any, Callable, Dict, List

from structlog import get_logger

MiB = 2**20
GiB = 2**30

log = get_logger()

# Test harnesses
log_data: List[str]
test_log_start: float
test_log_options: Dict[str, List[str]]
test_log_print: Callable


# workaround for ValueError: can't have unbuffered text I/O
class FlushingStream(IO[Any]):
    def __init__(self, stream: IO[Any]) -> None:
        self.stream = stream

    def write(self, s: Any, /) -> int:
        written = self.stream.write(s)
        self.stream.flush()
        return written

    def __getattribute__(self, name: str) -> Any:
        if name in ["write", "stream"]:
            return super().__getattribute__(name)
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


def parse_export_format(data: str) -> Dict[str, str]:
    """Parses formats intended for shell exports into a dict.

    ASDF=foo
    BSDF=bar

    Introduced to support output from `blkid`.

    """
    result = {}
    for line in data.splitlines():
        try:
            k, v = line.strip().split("=")
        except ValueError:
            continue
        k = k.strip()
        if not k:
            continue
        v = v.strip("'\"")
        result[k] = v
    return result


def generate_cloudinit_ssh_keyfile(
    users: List[Dict], resource_group: str
) -> str:

    authorized_ssh_keys = [
        u["ssh_pubkey"]
        for u in users
        if set(u["permissions"][resource_group]) & set(["sudo-srv", "manager"])
    ]
    flattened_ssh_keys = sum(authorized_ssh_keys, [])
    return (
        "### managed by Flying Circus - do not edit! ###\n"
        + "\n".join(flattened_ssh_keys)
        + "\n"
    )
