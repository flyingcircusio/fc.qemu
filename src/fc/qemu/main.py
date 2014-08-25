from .agent import Agent
import argparse


class Commands(object):

    def status(self, vm):
        a = Agent(vm)
        print a.status()

    def start(self, vm):
        a = Agent(vm)
        a.start()


def main():
    commands = Commands()
    a = argparse.ArgumentParser(description="Qemu VM agent")
    sub = a.add_subparsers(title='subcommands')

    p = sub.add_parser('status', help='Get the status of a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func=commands.status)

    p = sub.add_parser('start', help='Start a VM.')
    p.add_argument('vm', metavar='VM', help='name of the VM')
    p.set_defaults(func=commands.start)

    args = a.parse_args()
    func = args.func
    args = dict(args._get_kwargs())
    del args['func']
    func(**args)
