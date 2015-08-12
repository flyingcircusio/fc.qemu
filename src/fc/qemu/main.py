from .agent import Agent
from .sysconfig import sysconfig
import argparse
import logging
import os.path
import sys


logger = logging.getLogger(__name__)


def daemonize():
    """
    Copyright/License abberation:

    Copied from
http://stackoverflow.com/questions/1417631/python-code-to-daemonize-a-process
    and
http://www.jejik.com/articles/2007/02/a_simple_unix_linux_daemon_in_python/

    do the UNIX double-fork magic, see Stevens' "Advanced
    Programming in the UNIX Environment" for details (ISBN 0201563177)
    http://www.erlenstar.demon.co.uk/unix/faq_2.html#SEC16
    """
    try:
        pid = os.fork()
        if pid > 0:
            # exit first parent
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(
            "fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
        sys.exit(1)

    # decouple from parent environment
    os.chdir("/")
    os.setsid()
    os.umask(0)

    # do second fork
    try:
        pid = os.fork()
        if pid > 0:
            # exit from second parent
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(
            "fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
        sys.exit(1)

    # redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()
    si = open('/dev/null', 'r')
    so = open('/dev/null', 'a+', 0)
    se = open('/dev/null', 'a+', 0)
    os.dup2(si.fileno(), sys.stdin.fileno())
    os.dup2(so.fileno(), sys.stdout.fileno())
    os.dup2(se.fileno(), sys.stderr.fileno())


def init_logging(verbose=True):
    if verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(
        filename='/var/log/fc-qemu.log',
        format='%(asctime)s [%(process)d] %(levelname)s %(message)s',
        level=level)

    # silence requests package -- we assume that it's just doing its job
    logging.getLogger('requests').setLevel(logging.CRITICAL)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    logging.getLogger('').addHandler(console)


def main():
    a = argparse.ArgumentParser(description="Qemu VM agent")
    a.add_argument('--verbose', '-v', action='store_true', default=False,
                   help='Increase logging level.')
    a.add_argument('--daemonize', '-D', action='store_true',
                   help="Run command in background.")

    sub = a.add_subparsers(title='subcommands')
    p = sub.add_parser('status', help='Get the status of a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='status')

    p = sub.add_parser('ensure', help='Ensure proper status of the VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='ensure')

    p = sub.add_parser('start', help='Start a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='start')

    p = sub.add_parser('stop', help='Stop a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='stop')

    p = sub.add_parser('restart', help='Restart a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='restart')

    p = sub.add_parser('lock', help='Assume all locks of a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='lock')

    p = sub.add_parser('unlock', help='Release all locks of a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='unlock')

    p = sub.add_parser('snapshot',
                       help='Take a clean snapshot of this VM\'s root volume.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.add_argument('snapshot', help='name of the snapshot')
    p.set_defaults(func='snapshot')

    p = sub.add_parser('force-unlock', help="Release all locks of a VM even "
                       "if we don't own them.")
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='force_unlock')

    p = sub.add_parser('inmigrate', help='Start incoming migration for a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='inmigrate')

    p = sub.add_parser('outmigrate', help='Start outgoing migration for a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='outmigrate')

    p = sub.add_parser(
        'handle-consul-event',
        help='Handle a change in VM config distributed via consul.')
    p.set_defaults(func='handle_consul_event')

    args = a.parse_args()
    func = args.func
    vm = getattr(args, 'vm', None)
    kwargs = dict(args._get_kwargs())
    del kwargs['func']
    if 'vm' in kwargs:
        del kwargs['vm']
    del kwargs['daemonize']
    del kwargs['verbose']

    if args.daemonize:
        daemonize()

    try:
        init_logging(args.verbose)
        sysconfig.load_system_config()
        if vm is None:
            # Expecting a class/static method
            agent = Agent
            sys.exit(getattr(agent, func)(**kwargs) or 0)
        else:
            agent = Agent(vm)
            with agent:
                sys.exit(getattr(agent, func)(**kwargs) or 0)
    except Exception as e:
        logger.exception(e)
        sys.exit(69)  # EX_UNAVAILABLE
