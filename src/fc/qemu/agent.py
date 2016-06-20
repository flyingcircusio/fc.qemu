from .hazmat.ceph import Ceph
from .hazmat.qemu import Qemu
from .incoming import IncomingServer
from .outgoing import Outgoing
from .timeout import TimeOut
from .util import rewrite, locate_live_service, MiB, GiB
from .sysconfig import sysconfig
from logging import getLogger
import consulate
import contextlib
import copy
import fcntl
import json
import math
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
    handler = ConsulEventHandler()
    handler.handle(event)


class ConsulEventHandler(object):

    def handle(self, event):
        """Actual handling of a single Consul event in a
        separate process."""
        try:
            prefix = event['Key'].split('/')[0]
            if not event['Value']:
                return
            getattr(self, prefix)(event)
        except Exception as e:
            log.exception('error handling consul event: %s', e)

    def node(self, event):
        config = json.loads(event['Value'].decode('base64'))
        config['consul-generation'] = event['ModifyIndex']
        vm = config['name']
        if config['parameters']['machine'] != 'virtual':
            return
        try:
            agent = Agent(vm, config)
        except RuntimeError:
            log.warning('[%s] Ignoring VM check: cannot load config', vm)
            return
        log.info('[%s] Checking VM', vm)
        with agent:
            agent.save_enc()
            agent.ensure()

    def snapshot(self, event):
        value = json.loads(event['Value'].decode('base64'))
        vm = value['vm']
        snapshot = value['snapshot'].encode('ascii')
        try:
            agent = Agent(vm)
        except RuntimeError:
            log.warning('Ignoring snapshot for %s: failed to load config', vm)
            return
        with agent:
            if not agent.belongs_to_this_host():
                log.debug('Ignoring snapshot for %s as it belongs to another '
                          'host.', vm)
                return
            log.info('[%s] Processing snapshot request %s', vm, snapshot)
            agent.snapshot(snapshot)


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
    """Returns the swap partition size in bytes."""
    swap_mib = max(1024, 32 * math.sqrt(memory))
    return int(swap_mib) * MiB


def tmp_size(disk):
    """Returns the tmp partition size in bytes."""
    tmp_gib = max(5, math.sqrt(disk))
    return int(tmp_gib) * GiB


class Agent(object):
    """The agent to control a single VM."""

    # Attributes on this class can be overriden (in a controlled fashion
    # from the sysconfig module. See this class' __init__. The defaults
    # are here to support testing.

    this_host = ''
    migration_ctl_address = None
    accelerator = ''
    vhost = False
    ceph_id = 'admin'
    timeout_graceful = 80
    vm_config_template_path = [
        '/etc/qemu/qemu.vm.cfg.in',
        pkg_resources.resource_filename(__name__, 'qemu.vm.cfg.in')
    ]
    consul_token = None

    _configfile_fd = None

    def __init__(self, name, enc=None):
        # Update configuration values from system or test config.
        self.__dict__.update(sysconfig.agent)

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
        log.info('Processing %d consul event(s)', len(events))
        for e in events:
            p = multiprocessing.Process(target=_handle_consul_event, args=(e,))
            p.start()
            time.sleep(0.1)
        for proc in multiprocessing.active_children():
            proc.join()
        log.info('Finished processing %d consul event(s)', len(events))

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

    def belongs_to_this_host(self):
        return self.cfg['kvm_host'] == self.this_host

    @locked
    def ensure(self):
        if not self.cfg['online'] or not self.cfg['kvm_host']:
            self.ensure_offline()
        elif not self.belongs_to_this_host():
            if self.qemu.is_running():
                self.outmigrate()
            else:
                pass
        else:
            self.ensure_online()

        if self.state_is_consistent():
            if self.qemu.is_running():
                # We moved this from running directly after ensure_online().
                # But the result of ensure online is not guanteed to be
                # consistent nor running and running the online disk size
                # has caused spurious errors previously.
                self.ensure_online_disk_size()
            else:
                self.qemu.clean_run_files()
        else:
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
        target_size = self.cfg['disk'] * (1024 ** 3)
        if self.ceph.root.size >= target_size:
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
        self.ceph.start(self.enc)
        log.info('Starting VM %s', self.name)
        self.qemu.start()
        self.consul_register()
        # We exit here without releasing the ceph lock in error cases
        # because the start may have failed because of an already running
        # process. Removing the lock in that case is dangerous. OTOH leaving
        # the lock is never dangerous. Lets go with that.

    @contextlib.contextmanager
    def frozen_vm(self):
        try:
            try:
                log.debug('[%s] Freezing root disk.', self.name)
                self.qemu.freeze()
            except socket.timeout:
                log.warning('[%s] Timed out freezing the machine. '
                            'Continuing with unclean snapshot.', self.name)
            yield
        finally:
            try:
                log.debug('[%s] Thawing root disk.', self.name)
                self.qemu.thaw()
            except socket.timeout:
                log.warning('[%s] Failed to thaw. Retrying.', self.name)
                self.qemu.thaw()

    @locked
    def snapshot(self, snapshot):
        if snapshot in [x.snapname for x in self.ceph.root.snapshots]:
            return
        if self.qemu.is_running():
            with self.frozen_vm():
                self.ceph.root.snapshots.create(snapshot)
        else:
            log.info('[%s] VM not running, creating snapshot without freezing',
                     self.name)
            self.ceph.root.snapshots.create(snapshot)

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
        except (socket.error, RuntimeError):
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
        log.info('[%s] current state: qemu=%s, proc=%s, locked=%s',
                 *([self.name] + substates))
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

        vhost = '  vhost = "on"' if self.vhost else ''

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
""".format(ifname=ifname, mac=net_config['mac'], vhost=vhost))

        with open(self.vm_config_template) as f:
            tpl = f.read()
        accelerator = (' accel = "{}"'.format(self.accelerator)
                       if self.accelerator else '')
        self.qemu.config = tpl.format(
            accelerator=accelerator,
            network=''.join(netconfig), **self.cfg)
