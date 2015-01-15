from .hazmat.ceph import Ceph
from .hazmat.qemu import Qemu, QemuNotRunning
from .incoming import IncomingServer
from .outgoing import Outgoing
from .timeout import TimeOut
from fc.qemu.util import rewrite
from logging import getLogger
import fcntl
import os
import os.path
import pkg_resources
import socket
import yaml


log = getLogger(__name__)


def running(expected=True):
    def wrap(f):
        def checked(self, *args, **kw):
            if self.qemu.is_running() != expected:
                raise RuntimeError('action not allowed - VM is running')
            return f(self, *args, **kw)
        return checked
    return wrap


def locked(f):
    def locked(self, *args, **kw):
        if not self._configfile_fd:
            self._configfile_fd = os.open(self.configfile, os.O_RDONLY)
        fcntl.flock(self._configfile_fd, fcntl.LOCK_EX)
        try:
            return f(self, *args, **kw)
        finally:
            fcntl.flock(self._configfile_fd, fcntl.LOCK_UN)
    return locked


def swap_size(memory):
    if memory > 2048:
        swap = memory / 2
    else:
        swap = 1024
    return swap * 1024**2


def tmp_size(disk):
    # disk in GiB, return Bytes
    return max(5*1024, disk*1024/10) * 1024**2


class Agent(object):
    """The agent to control a single VM."""

    # Those values can be overriden using the /etc/qemu/fc-agent.conf
    # config file. The defaults are intended for testing purposes.
    this_host = ''
    migration_ctl_address = None
    accelerator = ''
    vhost = ''
    ceph_id = 'admin'
    timeout_graceful = 30
    vm_config_template_path = [
        '/etc/qemu/qemu.vm.cfg.in',
        pkg_resources.resource_filename(__name__, 'qemu.vm.cfg.in')
    ]

    _configfile_fd = None

    def __init__(self, name):
        if '.' in name:
            self.configfile = name
        else:
            self.configfile = '/etc/qemu/vm/{}.cfg'.format(name)
        try:
            with open(self.configfile) as f:
                self.enc = yaml.load(f)
        except IOError:
            raise RuntimeError("Could not load {}".format(self.configfile))

        self.cfg = self.enc['parameters']
        self.name = self.enc['name']
        self.cfg['name'] = self.enc['name']
        self.cfg['swap_size'] = swap_size(self.cfg['memory'])
        self.cfg['tmp_size'] = tmp_size(self.cfg['disk'])
        self.cfg['ceph_id'] = self.ceph_id
        self.qemu = Qemu(self.cfg)
        self.ceph = Ceph(self.cfg)
        self.contexts = [self.qemu, self.ceph]
        for attr in ['migration_ctl_address']:
            setattr(self, attr, getattr(self, attr).format(**self.cfg))
        for cand in self.vm_config_template_path:
            if os.path.exists(cand):
                self.vm_config_template = cand
                break

    def save(self):
        with open(self.configfile, 'w') as f:
            yaml.dump(self.enc, f)

    def __enter__(self):
        for c in self.contexts:
            c.__enter__()

    def __exit__(self, exc_value, exc_type, exc_tb):
        for c in self.contexts:
            try:
                c.__exit__(exc_value, exc_type, exc_tb)
            except Exception:
                log.exception('Error while leaving agent contexts.')

    @locked
    def ensure(self):
        if not self.cfg['online']:
            self.ensure_offline()
            return
        if self.cfg['kvm_host'] != self.this_host:
            # Initiate outmigrate.
            self.ensure_offline()
            return
        self.ensure_online()
        self.ensure_online_disk_size()

    def ensure_offline(self):
        if self.qemu.is_running():
            log.info('VM %s should not be running here', self.name)
            self.stop()

    def ensure_online(self):
        if not self.qemu.is_running():
            log.info('VM %s should be running here', self.name)
            self.start()

    def ensure_online_disk_size(self):
        """Trigger block resize action for the root disk via Qemu monitor."""
        target_size = self.cfg['disk'] * (1024**3)
        if self.ceph.root.image.size() >= target_size:
            return
        log.info('Online disk resize for VM %s to %s GiB', self.name,
                 self.cfg['disk'])
        self.qemu.resize_root(target_size)

    @locked
    @running(False)
    def start(self):
        self.generate_config()
        log.info('Using Qemu config template %s', self.vm_config_template)
        self.ceph.start()
        try:
            log.info('Starting VM %s', self.name)
            self.qemu.start()
        except QemuNotRunning:
            self.ceph.stop

    def status(self):
        """Determine status of the VM.
        """
        if self.qemu.is_running():
            status = 0
            print('online')
        else:
            status = 1
            print('offline')
        for lock in self.ceph.locks():
            print('lock: {}@{}'.format(*lock))
        return status

    @locked
    def stop(self):
        timeout = TimeOut(self.timeout_graceful, interval=3)
        log.info('Trying graceful shutdown of VM %s...', self.name)
        try:
            self.qemu.graceful_shutdown()
        except socket.error:
            pass
        while timeout.tick():
            if not self.qemu.is_running():
                break
        else:
            self.kill()
            return

        self.ceph.stop()
        self.qemu.clean_run_files()
        log.info('Graceful shutdown of %s succeeded', self.name)

    @locked
    def restart(self):
        log.info('Restarting VM %s', self.name)
        self.stop()
        self.start()

    @locked
    @running(True)
    def kill(self):
        log.info('Killing VM %s', self.name)
        timeout = TimeOut(15, interval=1, raise_on_timeout=True)
        self.qemu.destroy()
        while timeout.tick():
            if not self.qemu.is_running():
                break

        self.qemu.clean_run_files()
        self.ceph.stop()
        log.info('Killing VM %s succeeded', self.name)

    @locked
    @running(False)
    def delete(self):
        # XXX require a safety belt: make an online check that this
        # VM really should be deleted.
        pass

    @locked
    @running(False)
    def inmigrate(self, statefile):
        self.qemu.statefile = statefile.format(**self.qemu.cfg)
        if self.ceph.is_unlocked():
            # The VM isn't running at all. Just start it directly.
            self.start()
            with rewrite(statefile) as f:
                f.write('{}')
            return
        server = IncomingServer(self)
        server.run()

    @locked
    @running(True)
    def outmigrate(self, target):
        client = Outgoing(self, target)
        return client()

    @locked
    def lock(self):
        log.info('Assuming all Ceph locks for VM %s', self.name)
        for vol in self.ceph.volumes:
            vol.lock()

    @locked
    @running(False)
    def unlock(self):
        log.info('Releasing all Ceph locks for VM %s', self.name)
        self.ceph.stop()

    @running(False)
    def force_unlock(self):
        log.info('Breaking all Ceph locks for VM %s', self.name)
        self.ceph.force_unlock()

    # Helper methods

    # CAREFUL: changing anything in this config files will cause maintenance w/
    # reboot of all VMs in the infrastructure.
    def generate_config(self):
        """Generate a new Qemu config (and options) for a freshly
        starting VM.

        The configs are intended to be non-host-specific.

        This two-step behaviour is needed to support migrating VMs
        and keeping arguments consistent while allowing to localize arguments
        to the host running or receiving the VM.

        """
        self.qemu.args = [
            '-daemonize',
            '-nodefaults',
            '-name {name},process=kvm.{name}',
            '-chroot {{chroot}}',
            '-runas nobody',
            '-serial file:/var/log/vm/{name}.log',
            '-display vnc={{vnc}}',
            '-pidfile {{pidfile}}',
            '-vga cirrus',
            '-m {memory}',
            '-watchdog i6300esb',
            '-watchdog-action reset',
            '-readconfig {{configfile}}']
        self.qemu.args = [a.format(**self.cfg)
                          for a in self.qemu.args]

        netconfig = []
        for net, net_config in sorted(self.cfg['interfaces'].items()):
            ifname = 't{}{}'.format(net, self.cfg['id'])
            netconfig.append("""
[device]
  driver = "virtio-net-pci"
  netdev = "{ifname}"
  mac = "{mac}"

[netdev "{ifname}"]
  type = "tap"
  ifname = "{ifname}"
  script = "/etc/kvm/kvm-ifup"
  downscript = "/etc/kvm/kvm-ifdown"
{vhost}
""".format(ifname=ifname, mac=net_config['mac'], vhost=self.vhost))

        with open(self.vm_config_template) as f:
            tpl = f.read()
        self.qemu.config = tpl.format(
            accelerator=self.accelerator,
            network=''.join(netconfig), **self.cfg)
