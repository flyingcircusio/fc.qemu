from .incomingvm import IncomingVM
from .outgoingvm import OutgoingVM
import argparse
import logging
import sys
import socket


def migrate_incoming(args):
    with IncomingVM(args.vm, args.listen_host) as vm:
        vm.run()
    sys.exit(vm.migration_errorcode)


def migrate_outgoing(args):
    if args.migaddr:
        migaddr = args.migaddr
    else:
        migaddr = args.ctladdr + '.sto'

    # We hit some DNS resolver issue with Qemu here. Not sure why.
    migaddr = socket.gethostbyname(migaddr)
    with OutgoingVM(args.vm, args.ctladdr, migaddr) as vm:
        vm.wait_for_incoming_agent()
        vm.transfer_locks()
        vm.migrate()

    sys.exit(vm.migration_errorcode)


def main():
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument('vm', metavar='VM', help='name of the VM to be migrated')
    a.add_argument('-v', '--verbose', help='increase output level',
                   action='store_true', default=False)

    sub = a.add_subparsers(title='subcommands')

    incoming = sub.add_parser('incoming', help='initiate incoming migration')
    incoming.add_argument('listen_host', help='host name/IP address to open '
                          'listening XML-RPC port on (local interface)')
    incoming.set_defaults(func=migrate_incoming)

    outgoing = sub.add_parser('outgoing', help='initiate outgoing migration')
    outgoing.add_argument('-m', '--migaddr', metavar='HOST', default=None,
                          help='IP address/host name to send actual VM '
                          'contents to (default: {ctladdr}.sto)')
    outgoing.add_argument('ctladdr', help='IP address/host name for the '
                          'XML-RPC control connection')
    outgoing.set_defaults(func=migrate_outgoing)

    args = a.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='{} {}: %(levelname)s: %(message)s'.format(
                            a.prog, args.vm))
    args.func(args)


if __name__ == '__main__':
    main()
