from .hazmat.ceph import Ceph
from .hazmat.qemu import Qemu, QemuNotRunning
from .timeout import TimeOut
from logging import getLogger
import fcntl
import socket
import os
import os.path
import yaml


log = getLogger(__name__)


def running(expected=True):
    def wrap(f):
        def checked(self, *args, **kw):
            if self.qemu.is_running() != expected:
                raise RuntimeError('action not allowed')
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
    accelerator = ''
    vhost = ''
    ceph_id = 'admin'
    vnc = 'localhost:1'
    timeout_graceful = 30

    _configfile_fd = None

    def __init__(self, name):
        if '.' in name:
            self.configfile = name
        else:
            self.configfile = '/etc/qemu/vm/{}.cfg'.format(name)
        if not os.path.isfile(self.configfile):
            raise RuntimeError("Could not find {}".format(self.configfile))

        self.enc = yaml.load(open(self.configfile))
        self.cfg = self.enc['parameters']
        self.name = self.enc['name']
        self.cfg['name'] = self.enc['name']
        self.cfg['swap_size'] = swap_size(self.cfg['memory'])
        self.cfg['tmp_size'] = tmp_size(self.cfg['disk'])
        self.qemu = Qemu(self.cfg)
        self.ceph = Ceph(self.cfg)
        self.contexts = [self.qemu, self.ceph]
        self.vnc = self.vnc.format(**self.cfg)

    def save(self):
        with open(self.configfile, 'w') as f:
            f.write(yaml.dump(self.enc))

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
            print "VM should not be running here."
            self.stop()

    def ensure_online(self):
        if not self.qemu.is_running():
            print "VM should be running here."
            self.start()

    def ensure_online_disk_size(self):
        """Trigger block resize action for the root disk via Qemu monitor."""
        target_size = self.cfg['disk'] * (1024**3)
        if self.ceph.root.image.size() >= target_size:
            return

        print 'resizing disk for VM to {} GiB'.format(self.cfg['disk'])
        self.qemu.resize_root(target_size)

    @locked
    @running(False)
    def start(self):
        self.generate_config()
        self.ceph.start()
        try:
            self.qemu.start()
        except QemuNotRunning:
            self.ceph.stop

    def status(self):
        """Determine status of the VM.
        """
        if self.qemu.is_running():
            status = 0
            print 'online'
        else:
            status = 1
            print 'offline'
        for lock in self.ceph.locks():
            print 'lock: {}@{}'.format(*lock)
        return status

    @locked
    def stop(self):
        timeout = TimeOut(self.timeout_graceful, interval=1)
        print "Trying graceful shutdown ..."
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
        print "Graceful shutdown succeeded."

    @locked
    def restart(self):
        print "Restarting VM..."
        self.stop()
        self.start()

    @locked
    @running(True)
    def kill(self):
        print "Killing VM"
        timeout = TimeOut(15, interval=1, raise_on_timeout=True)
        self.qemu.destroy()
        while timeout.tick():
            if not self.qemu.is_running():
                break

        self.qemu.clean_run_files()
        self.ceph.stop()
        print "Killing VM succeeded."

    @locked
    @running(False)
    def delete(self):
        # XXX require a safety belt: make an online check that this
        # VM really should be deleted.
        pass

    @locked
    @running(False)
    def inmigrate(self):
        pass

    @locked
    @running(True)
    def outmigrate(self):
        pass

    @locked
    def lock(self):
        print "Assuming all Ceph locks."
        for vol in self.ceph.volumes:
            vol.lock()

    @locked
    @running(False)
    def unlock(self):
        print "Releasing all Ceph locks."
        self.ceph.stop()

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
        if not os.path.exists('/var/log/vm'):
            os.makedirs('/var/log/vm')
        chroot = '/srv/vm/{name}'.format(name=self.cfg['name'])
        if not os.path.exists(chroot):
            os.makedirs(chroot)

        self.qemu.args = [
            '-daemonize',
            '-nodefaults',
            '-name {name},process=kvm.{name}',
            '-chroot {chroot}',
            '-runas nobody',
            '-serial file:/var/log/vm/{name}.log',
            '-display vnc={vnc}',
            '-pidfile {{pidfile}}',
            '-vga cirrus',
            '-m {memory}',
            '-watchdog i6300esb',
            '-watchdog-action reset',
            '-readconfig {{configfile}}']
        self.qemu.args = [a.format(vnc=self.vnc, chroot=chroot, **self.cfg)
                          for a in self.qemu.args]

        self.qemu.config = """\
# qemu config file
# generated by localconfig. do not edit.

[machine]
  type = "pc-q35-2.1"
{accelerator}

[smp-opts]
  cpus = "{cores}"

[name]
  guest = "{name}"
  process = "kvm.{name}"

[drive]
  index = "0"
  media = "disk"
  if = "virtio"
  format = "rbd"
  file = "rbd:{resource_group}/{name}.root:id={ceph_id}"
  aio = "native"
  cache = "writeback"

[drive]
  index = "1"
  media = "disk"
  if = "virtio"
  format = "rbd"
  file = "rbd:{resource_group}/{name}.swap:id={ceph_id}"
  aio = "native"
  cache = "writeback"

[drive]
  index = "2"
  media = "disk"
  if = "virtio"
  format = "rbd"
  file = "rbd:{resource_group}/{name}.tmp:id={ceph_id}"
  aio = "native"
  cache = "writeback"

[device]
  driver = "virtio-rng-pci"
  max-bytes = "1024"
  period = "100"

# Guest agent support
[device]
  driver = "virtio-serial"

[device]
  driver = "virtserialport"
  chardev = "qga0"
  name = "org.qemu.guest_agent.0"

[chardev "qga0"]
  backend = "socket"
  path = "/run/qemu.{name}.gqa.sock"
  server = "on"
  wait = "off"

# QMP monitor support via Unix socket

[mon "qmp_monitor"]
  mode = "control"
  chardev = "ch_qmp_monitor"
  default = "on"

[chardev "ch_qmp_monitor"]
  backend = "socket"
  path = "/run/qemu.{name}.qmp.sock"
  server = "on"
  wait = "off"

# Human monitor support via Unix socket

[chardev "ch_readline_socket_monitor"]
  backend = "socket"
  path = "/run/qemu.{name}.monitor.sock"
  server = "on"
  wait = "off"

[mon "readline_socket_monitor"]
  mode = "readline"
  chardev = "ch_readline_socket_monitor"

# Human monitor support via localhost IP

[chardev "ch_readline_telnet_monitor"]
  backend = "socket"
  host = "localhost"
  port = "{{monitor_port}}"
  server = "on"
  wait = "off"

[mon "readline_telnet_monitor"]
  mode = "readline"
  chardev = "ch_readline_telnet_monitor"

# Network interfaces

""".format(accelerator=self.accelerator, ceph_id=self.ceph_id, **self.cfg)

        for net, net_config in sorted(self.cfg['interfaces'].items()):
            ifname = 't{}{}'.format(net, self.cfg['id'])
            self.qemu.config += """
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
""".format(ifname=ifname, mac=net_config['mac'], vhost=self.vhost)
