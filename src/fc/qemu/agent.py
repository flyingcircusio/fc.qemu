from .hazmat.ceph import Ceph
from .hazmat.qemu import Qemu
from .incoming import IncomingServer
from .outgoing import Outgoing
from .timeout import TimeOut
from .util import rewrite, locate_live_service
from logging import getLogger
import consulate
import copy
import fcntl
import json
import multiprocessing
import os
import os.path as p
import pkg_resources
import socket
import sys
import time
import yaml

log = getLogger(__name__)


def _handle_consul_event(event):
    """Actual handling of a single Consul event in a separate process."""
    try:
        config = json.loads(event['Value'].decode('base64'))
        config['consul-generation'] = event['ModifyIndex']
        vm = config['name']
        if config['parameters']['machine'] != 'virtual':
            return
        log.debug('[Consul] checking VM %s', vm)
        agent = Agent(vm, config)
        with agent:
            agent.save_enc()
            agent.ensure()
    except Exception as e:
        log.exception('error handling consul event: %s', e)


def running(expected=True):
    def wrap(f):
        def checked(self, *args, **kw):
            if self.qemu.is_running() != expected:
                raise RuntimeError(
                    'action not allowed - VM must {}be running here'.format(
                        '' if expected else 'not '))
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
    timeout_graceful = 80
    vm_config_template_path = [
        '/etc/qemu/qemu.vm.cfg.in',
        pkg_resources.resource_filename(__name__, 'qemu.vm.cfg.in')
    ]
    consul_token = None

    _configfile_fd = None

    def __init__(self, name, enc=None):
        if '.' in name:
            self.configfile = name
        else:
            self.configfile = '/etc/qemu/vm/{}.cfg'.format(name)
        if enc is not None:
            self.enc = enc
        else:
            self.enc = self._load_enc()

        self.cfg = copy.copy(self.enc['parameters'])
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
            if p.exists(cand):
                self.vm_config_template = cand
                break
        self.consul = consulate.Consul(token=self.consul_token)

    def _load_enc(self):
        try:
            with open(self.configfile) as f:
                return yaml.safe_load(f)
        except IOError:
            raise RuntimeError("Could not load {}".format(self.configfile))

    @classmethod
    def handle_consul_event(cls):
        events = json.load(sys.stdin)
        if not events:
            return
        log.info('[Consul] processing %d event(s)', len(events))
        for e in events:
            p = multiprocessing.Process(target=_handle_consul_event, args=(e,))
            p.start()
            time.sleep(0.1)
        for proc in multiprocessing.active_children():
            proc.join()

    def save_enc(self):
        if not p.isdir(p.dirname(self.configfile)):
            os.makedirs(p.dirname(self.configfile))
        with rewrite(self.configfile) as f:
            yaml.safe_dump(self.enc, f)
        os.chmod(self.configfile, 0o644)

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
        if not self.cfg['online'] or not self.cfg['kvm_host']:
            self.ensure_offline()
        elif self.cfg['kvm_host'] != self.this_host:
            if self.qemu.is_running():
                self.outmigrate()
            else:
                pass
        else:
            self.ensure_online()
            self.ensure_online_disk_size()
        if not self.state_is_consistent():
            log.warning('%s: state not consistent (monitor, pidfile, ceph), '
                        'destroying VM and cleaning up', self.name)
            self.qemu.destroy()

    def ensure_offline(self):
        if not self.qemu.is_running():
            return
        log.info('VM %s should not be running here', self.name)
        self.stop()

    def ensure_online(self):
        if self.qemu.is_running():
            # re-register in case services got lost during Consul restart
            self.consul_register()
            return
        log.info('VM %s should be running here', self.name)
        existing = locate_live_service(self.consul, 'qemu-' + self.name)
        if existing and existing['Address'] != self.this_host:
            log.info('Found VM %s to be running on %s already. '
                     'Trying an inmigration.', self.name, existing['Address'])
            self.inmigrate()
        else:
            self.start()

    def ensure_online_disk_size(self):
        """Trigger block resize action for the root disk via Qemu monitor."""
        target_size = self.cfg['disk'] * (1024**3)
        if self.ceph.root.image.size() >= target_size:
            return
        log.info('Online disk resize for VM %s to %s GiB', self.name,
                 self.cfg['disk'])
        self.qemu.resize_root(target_size)

    def consul_register(self):
        """Register running VM with Consul."""
        self.consul.agent.service.register(
            'qemu-{}'.format(self.name),
            address=self.this_host,
            interval='5s',
            check=('test -e /proc/$(< /run/qemu.{}.pid )/mem || exit 2'.
                   format(self.name)))

    def consul_deregister(self):
        """De-register non-running VM with Consul."""
        self.consul.agent.service.deregister('qemu-{}'.format(self.name))

    @locked
    @running(False)
    def start(self):
        self.generate_config()
        log.info('Using Qemu config template %s', self.vm_config_template)
        self.ceph.start()
        log.info('Starting VM %s', self.name)
        self.qemu.start()
        self.consul_register()
        # We exit here without releasing the ceph lock in error cases
        # because the start may have failed because of an already running
        # process. Removing the lock in that case is dangerous. OTOH leaving
        # the lock is never dangerous. Lets go with that.

    def status(self):
        """Determine status of the VM."""
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
                self.ceph.stop()
                self.qemu.clean_run_files()
                self.consul_deregister()
                log.info('Graceful shutdown of %s succeeded', self.name)
                break
        else:
            self.kill()

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
                log.info('VM killed, cleaning up.')
                self.ceph.stop()
                self.qemu.clean_run_files()
                self.consul_deregister()
                break
        else:
            log.warning('Did not see the VM disappear. '
                        'Please check lock consistency.')

    @locked
    @running(False)
    def inmigrate(self):
        log.info('Preparing to migrate-in VM %s', self.name)
        if self.ceph.is_unlocked():
            log.info("%s: VM isn't running at all, starting it directly",
                     self.name)
            self.start()
            return
        server = IncomingServer(self)
        exitcode = server.run()
        if not exitcode:
            self.consul_register()
        log.info('%s: inmigration finished with exitcode %s', self.name,
                 exitcode)
        return exitcode

    @locked
    @running(True)
    def outmigrate(self):
        log.info('Migrating VM %s out', self.name)
        # re-register in case services got lost during Consul restart
        self.consul_register()
        client = Outgoing(self)
        exitcode = client()
        if not exitcode:
            self.consul_deregister()
        log.info('Out-migration of VM %s finished with exitcode %s',
                 self.name, exitcode)
        return exitcode

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
    def state_is_consistent(self):
        """Returns True if all relevant components agree about VM state.

        If False, results from Qemu monitor, pidfile or Ceph differ.
        """
        substates = [
            self.qemu.is_running(),
            bool(self.qemu.proc()),
            self.ceph.locked_by_me()]
        log.info('Current state: qemu=={}, proc=={}, locked=={}'.
                 format(*substates))
        return any(substates) == all(substates)

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
            '-vga std',
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
