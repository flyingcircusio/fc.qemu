# noqa

# The context manager is suitable to express "I am going to
# interact with this VM: please prepare connections to
# the according backends and make sure to clean up after me.""
def __enter__(self):
    self.cookie = self.locks.auth_cookie()
    return self


def __exit__(self, _exc_type, _exc_value, _traceback):
    if _exc_type is None:
        self.migration_errorcode = 0
    else:
        self.migration_errorcode = 1
        try:
            _log.exception('A problem occured trying to migrate the VM. '
                           'Trying to rescue it.',
                           exc_info=(_exc_type, _exc_value, _traceback))
            self.rescue()
        except:
            # Purposeful bare except: try really hard to kill
            # our VM.
            _log.exception('A problem occured trying to rescue the VM '
                           'after a migration failure. Destroying it.')
            self.destroy()

    self.monitor = None



def outmigrate(self):
    if not self.is_running():
        return
    subprocess.check_call(
        ['/usr/bin/fc-livemig', self.name, 'outgoing',
         self.parameters['kvm_host']])


def inmigrate(self):
    if self.is_running():
        return
    subprocess.check_call(
        ['/usr/bin/fc-livemig', self.name, 'incoming',
         self.this_kvm_host])


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
