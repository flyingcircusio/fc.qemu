import datetime
import os
import shlex
import subprocess
import sys
import time

from fc.qemu.main import daemonize
from fc.qemu.util import FlushingStream, ensure_separate_cgroup


def run_supervised(cmd, name, logfile):
    _log = FlushingStream(open(logfile, "a+"))

    def log(msg):
        now = datetime.datetime.now().isoformat()
        _log.write(f"{now} - {msg}\n")

    daemonize(_log)
    log(f"starting command {cmd}")
    s = subprocess.Popen(
        shlex.split(cmd),
        close_fds=True,
        stdin=None,
        stdout=_log,
        stderr=_log,
    )
    log(f"command has PID {s.pid}")
    exit_code = s.wait()
    log(f"command exited with exit code {exit_code}")

    # Restart immediately using ensure. This can happen if a VM powers down
    # with the intention to get started with new settings or if qemu crashes.
    # If the VM really should be shut down, then fc-qemu won't do anything.
    # This requires that `agent.ensure` is using a non-blocking lock to
    # avoid deadlocks.

    DELAY = 5
    for try_ in range(int(60 / DELAY)):
        log(f"ensuring VM state (try {try_})")
        s = subprocess.Popen(
            ["fc-qemu", "-v", "ensure", name],
            close_fds=True,
            stdin=None,
            stdout=_log,
            stderr=_log,
            encoding="ascii",
            errors="replace",
        )
        log(f"ensure command exited with exit code {exit_code}")

        exit_code = s.wait()
        if exit_code != os.EX_TEMPFAIL:
            break

        time.sleep(DELAY)


def main():
    ensure_separate_cgroup()
    run_supervised(*sys.argv[1:])
