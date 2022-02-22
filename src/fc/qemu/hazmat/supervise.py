import datetime
import shlex
import subprocess
import sys

from fc.qemu.main import daemonize
from fc.qemu.util import ensure_separate_cgroup


def run_supervised(cmd, name, logfile):
    daemonize()
    log = open(logfile, "a+", buffering=0)
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


def main():
    ensure_separate_cgroup()
    run_supervised(*sys.argv[1:])
