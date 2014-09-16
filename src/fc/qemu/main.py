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
    accelerator = sysconfig.get('qemu', 'accelerator')
    if accelerator:
        Agent.accelerator = '   accel = "{}"'.format(accelerator)
    if sysconfig.getboolean('qemu', 'vhost'):
        Agent.vhost = '    vhost = "on"'
    Agent.vnc = sysconfig.get('qemu', 'vnc')
    Agent.timeout_graceful = sysconfig.getint('qemu', 'timeout-graceful')
    Agent.this_host = sysconfig.get('ceph', 'lock_host')

    # CEPH
    from .hazmat import ceph
    Agent.ceph_id = sysconfig.get('ceph', 'client-id')
    ceph.CEPH_CLUSTER = sysconfig.get('ceph', 'cluster', 'ceph')
    ceph.CEPH_LOCK_HOST = sysconfig.get('ceph', 'lock_host')
    # Not sure whether it makes sense to hardcode this. Weird that qemu
    # doesn't want to see client.<name>.
    ceph.CEPH_CLIENT = Agent.ceph_id
    ceph.CREATE_VM = sysconfig.get('ceph', 'create-vm')


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

    logger.info('$ ' + ' '.join(sys.argv))


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

    p = sub.add_parser('lock', help='Assume all locks of a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='lock')

    p = sub.add_parser('unlock', help='Release all locks of a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='unlock')

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
