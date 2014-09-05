from .agent import Agent
import argparse
import logging
import sys

logger = logging.getLogger(__name__)


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

    p = sub.add_parser('start', help='Start a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='start')

    p = sub.add_parser('stop', help='Stop a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func='stop')

    args = a.parse_args()
    func = args.func
    vm = args.vm
    args = dict(args._get_kwargs())
    del args['func']
    del args['vm']

    init_logging()

    agent = Agent(vm)
    with agent:
        getattr(agent, func)(**args)
