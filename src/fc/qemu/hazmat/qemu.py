from .monitor import Monitor
import socket
import subprocess
import yaml


HOSTNAME = socket.gethostname()
SUFFIX = 'rzob.gocept.net'  # XXX


class Qemu(object):

    executable = 'qemu-system-x86_64'

    # This cfg is the cfg from the agent.
    cfg = None

    # The non-hosts-specific config configuration of this Qemu instance.
    args = ()
    config = ''

    MONITOR_OFFSET = 20000

    pidfile = '/run/kvm.{name}.pid'
    configfile = '/run/qemu.{name}.cfg'
    argfile = '/run/qemu.{name}.args'

    def __init__(self, cfg):
        self.cfg = cfg
        self.monitor = Monitor(cfg['id'] + self.MONITOR_OFFSET)

        for f in ['pidfile', 'configfile', 'argfile']:
            setattr(self, f, getattr(self, f).format(**cfg))

    def start(self):
        self.prepare_config()
        with open('/proc/sys/vm/compact_memory', 'w') as f:
            f.write('1')
        subprocess.check_call(
            '{executable} {args}'.format(executable=self.executable,
                                         args=' '.join(self.local_args)),
            shell=True)
        # XXX Validate the PID

    def is_running(self):
        try:
            self.monitor.assert_status('VM status: running')
            return True
        except Exception:
            return False

    # def is_running(self):
    #     if not os.path.exists(self.pidfile):
    #         return False
    #     try:
    #         with open(self.pidfile) as f:
    #             pid = int(f.read())
    #     except ValueError:
    #         return False
    #     return pid > 1 and os.path.exists('/proc/%s' % pid)

    def status(self):
        return self.monitor.status()

    def rescue(self):
        """Recover from potentially inconsistent state.

        If the VM is running and we own all locks, then everything is fine.

        If the VM is running and we do not own the locks, then try to acquire
        them or bail out.

        Returns True if we were able to rescue the VM.
        Returns False if the rescue attempt failed and the VM is stopped now.

        """
        # XXX hold a lock to avoid another process on the same machine to
        # interfere with the VM while we're on it. E.g. by locking the main
        # config file.
        self.monitor.assert_status('VM status: running')
        for image in set(self.locks.available) - set(self.locks.held):
            try:
                self.acquire_lock(image)
            except LockError:
                pass

        self.assert_locks()

    def graceful_shutdown(self):
        self.monitor.sendkey('ctrl-alt-delete')

    def destroy(self):
        # We use this destroy command in "fire-and-forget"-style because
        # sometimes the init script will complain even if we achieve what
        # we want: that the VM isn't running any longer. We check this
        # by contacting the monitor instead.

        timeout = TimeOut(5, interval=1, raise_on_timeout=True)
        while timeout.tick():
            status = self.monitor.status()
            if status == '':
                break

        # We could not connect to the monitor, thus the VM is gone.
        self.query_locks()
        for image in list(self.locks.held):
            self.release_lock(image)


    def image_names(self):
        prefix = self.name + '.'
        r = rbd.RBD()
        for img in r.list(self.ioctx):
            if img.startswith(prefix) and not '@' in img:
                yield img

    def images(self):
        for name in self.image_names():
            with rbd.Image(self.ioctx, name) as i:
                yield name, i

    def query_locks(self):
        """Collect all locks for all images of this VM.

        list_lockers returns:
            [{'lockers': [(locker_id, host, address), ...],
              'exclusive': bool,
              'tag': str}, ...]
        """
        self.locks = Locks()
        for name, img in self.images():
            self.locks.add(name, img.list_lockers())

    def acquire_lock(self, image_name):
        with rbd.Image(self.ioctx, image_name) as img:
            try:
                img.lock_exclusive(CEPH_ID)
                self.locks.acquired(image_name)
            except rbd.ImageBusy:
                raise LockError('failed to acquire lock', image_name)
            except rbd.ImageExists:
                # we hold the lock already
                pass

    def release_lock(self, image_name):
        """Release lock.

        Make sure that vm.locks.available is up to date before calling
        this method.
        """
        lock = self.locks.available[image_name]
        if lock.host != CEPH_ID:
            raise LockError('refusing to release lock held by another host',
                            lock)
        with rbd.Image(self.ioctx, image_name) as img:
            try:
                img.break_lock(lock.locker_id, CEPH_ID)
                self.locks.released(image_name)
            except rbd.ImageNotFound:
                # lock has already been released
                pass

    def assert_locks(self):
        if not self.locks.held == self.locks.available:
            raise RuntimeError(
                "I don't own all locks for {}".format(self.name),
                self.locks.held.keys(), self.locks.available.keys())

    def prepare_config(self):
        format = lambda s: s.format(
            hostname=HOSTNAME,
            suffix=SUFFIX,
            configfile=self.configfile,
            monitor_port=self.monitor.port)
        self.local_args = [format(a) for a in self.args]
        self.local_config = format(self.config)

        with open(self.configfile+'.in', 'w') as f:
            f.write(self.config)
        with open(self.configfile, 'w') as f:
            f.write(self.local_config)

        with open(self.argfile+'.in', 'w') as f:
            yaml.dump(self.args, f)


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
