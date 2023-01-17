import datetime
import shlex
import subprocess
import sys

from fc.qemu.main import daemonize
from fc.qemu.util import FlushingStream, ensure_separate_cgroup


def run_supervised(cmd, name, logfile):
    log = FlushingStream(open(logfile, "a+"))
    daemonize(log)
    now = datetime.datetime.now().isoformat()
    log.write("{} - starting command {}\n".format(now, cmd))
    s = subprocess.Popen(
        shlex.split(cmd), close_fds=True, stdin=None, stdout=log, stderr=log
    )
    now = datetime.datetime.now().isoformat()
    log.write("{} - command has PID {}\n".format(now, s.pid))
    exit_code = s.wait()
    now = datetime.datetime.now().isoformat()
    log.write("{} - command exited with exit code {}\n".format(now, exit_code))

    # Restart immediately using ensure. This can happen if a VM powers down
    # with the intention to get started with new settings or if qemu crashes.
    # If the VM really should be shut down, then fc-qemu won't do anything.
    # This requires that `agent.ensure` is using a non-blocking lock to
    # avoid deadlocks.
    log.write("{} - ensuring VM state".format(now))
    s = subprocess.Popen(
        ["fc-qemu", "-v", "ensure", name],
        close_fds=True,
        stdin=None,
        stdout=log,
        stderr=log,
        encoding="ascii",
        errors="replace",
    )
    exit_code = s.wait()
    now = datetime.datetime.now().isoformat()
    log.write(
        "{} - ensure command exited with exit code {}\n".format(now, exit_code)
    )


def main():
    ensure_separate_cgroup()
    run_supervised(*sys.argv[1:])
