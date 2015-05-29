from .agent import Agent
from .hazmat.qemu import Qemu
import argparse
import ConfigParser
import logging
import os.path
import sys


logger = logging.getLogger(__name__)


def load_system_config():
    # System-wide config - pretty hacky
    sysconfig = ConfigParser.SafeConfigParser()
    sysconfig.read(os.path.dirname(__file__) + '/default.conf')
    sysconfig.read('/etc/qemu/fc-qemu.conf')

    # QEMU
    accelerator = sysconfig.get('qemu', 'accelerator')
    if accelerator:
        Agent.accelerator = '  accel = "{}"'.format(accelerator)
    else:
        Qemu.require_kvm = False
    if sysconfig.getboolean('qemu', 'vhost'):
        Agent.vhost = '  vhost = "on"'
    Agent.timeout_graceful = sysconfig.getint('qemu', 'timeout-graceful')
    Agent.this_host = sysconfig.get('ceph', 'lock_host')
    Agent.migration_ctl_address = sysconfig.get(
        'qemu', 'migration-ctl-address')
    Agent.consul_token = sysconfig.get('consul', 'access-token')
    Qemu.migration_address = sysconfig.get('qemu', 'migration-address')
    Qemu.vnc = sysconfig.get('qemu', 'vnc')

    # CEPH
    from .hazmat import ceph
    Agent.ceph_id = sysconfig.get('ceph', 'client-id')
    ceph.CEPH_CLUSTER = sysconfig.get('ceph', 'cluster', 'ceph')
    ceph.CEPH_LOCK_HOST = sysconfig.get('ceph', 'lock_host')
    # Not sure whether it makes sense to hardcode this. Weird that qemu
    # doesn't want to see client.<name>.
    ceph.CEPH_CLIENT = Agent.ceph_id
    ceph.CREATE_VM = sysconfig.get('ceph', 'create-vm')
    ceph.SHRINK_VM = sysconfig.get('ceph', 'shrink-vm')


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
        load_system_config()
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
