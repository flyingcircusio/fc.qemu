from .agent import Agent
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
    from .hazmat.qemu import Qemu
    accelerator = sysconfig.get('qemu', 'accelerator')
    if accelerator:
        Agent.accelerator = '   accel = "{}"'.format(accelerator)
    else:
        Qemu.require_kvm = False
    if sysconfig.getboolean('qemu', 'vhost'):
        Agent.vhost = '  vhost = "on"'
    Agent.timeout_graceful = sysconfig.getint('qemu', 'timeout-graceful')
    Agent.this_host = sysconfig.get('ceph', 'lock_host')
    Agent.migration_ctl_address = sysconfig.get(
        'qemu', 'migration-ctl-address')
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


def init_logging(verbose=True):
    if verbose:
        level = logging.DEBUG
    else:
        level = logging.WARNING
    logging.basicConfig(
        filename='/var/log/fc-qemu.log',
        format='%(asctime)s [%(process)d] %(message)s',
        level=logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    logging.getLogger('').addHandler(console)


def main():
    a = argparse.ArgumentParser(description="Qemu VM agent")
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
    p.add_argument(
        'target', help='hostname:port of the target expecting inmigration')
    p.set_defaults(func='outmigrate')

    args = a.parse_args()
    func = args.func
    vm = args.vm
    args = dict(args._get_kwargs())
    del args['func']
    del args['vm']

    init_logging()

    load_system_config()

    agent = Agent(vm)
    with agent:
        sys.exit(getattr(agent, func)(**args) or 0)
