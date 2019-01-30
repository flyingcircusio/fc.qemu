from fc.qemu.main import daemonize
import datetime
import sys
import subprocess
import time
import shlex


def run_supervised(cmd, name, logfile):
    daemonize()
    log = open(logfile, 'a+', buffering=0)
    now = datetime.datetime.now().isoformat()
    log.write('{} - starting command {}\n'.format(now, cmd))
    s = subprocess.Popen(
        shlex.split(cmd), close_fds=True, stdin=None, stdout=log, stderr=log)
    now = datetime.datetime.now().isoformat()
    log.write('{} - command has PID {}\n'.format(now, s.pid))
    exit_code = s.wait()
    now = datetime.datetime.now().isoformat()
    log.write('{} - command exited with exit code {}\n'.format(now, exit_code))


if __name__ == '__main__':
    run_supervised(*sys.argv[1:])
