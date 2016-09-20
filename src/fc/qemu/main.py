import sys
import os.path


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
    import os
    import os.path
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


def main():
    import argparse

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

    p = sub.add_parser(
        'telnet',
        help='Open a telnet connection to the VM\'s monitor port')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='telnet')

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

    from .agent import Agent, InvalidCommand, VMConfigNotFound
    from .logging import init_logging
    from .util import log
    try:
        init_logging(args.verbose)
        log.debug('load-system-config')

        from .sysconfig import sysconfig
        sysconfig.load_system_config()

        if vm is None:
            # Expecting a class/static method
            agent = Agent
            sys.exit(getattr(agent, func)(**kwargs) or 0)
        else:
            if '.' in vm:
                # Our fc.agent calls us with path names. This is kinda stupid
                # but this was hidden in the Agent class before and now is
                # a bit more explicit.
                vm = os.path.basename(vm).split('.')[0]
            agent = Agent(vm)
            with agent:
                sys.exit(getattr(agent, func)(**kwargs) or 0)
    except (VMConfigNotFound, InvalidCommand):
        # Those exceptions are properly logged and don't have to be shown
        # with their traceback.
        log.debug('unexpected-exception', exc_info=True)
        sys.exit(69)  # EX_UNAVAILABLE
    except Exception:
        log.exception("unexpected-exception", exc_info=True)
        sys.exit(69)  # EX_UNAVAILABLE
