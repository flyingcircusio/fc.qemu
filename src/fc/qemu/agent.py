from .hazmat.ceph import Ceph
from .hazmat.qemu import Qemu, detect_current_machine_type
from .incoming import IncomingServer
from .outgoing import Outgoing
from .sysconfig import sysconfig
from .timeout import TimeOut
from .util import rewrite, locate_live_service, MiB, GiB, log
import consulate
import contextlib
import copy
import distutils.spawn
import fcntl
import json
import math
import os
import os.path as p
import pkg_resources
import socket
import sys
import threading
import yaml


class InvalidCommand(RuntimeError):
    pass


class VMConfigNotFound(RuntimeError):
    pass


def _handle_consul_event(event):
    handler = ConsulEventHandler()
    handler.handle(event)


class ConsulEventHandler(object):

    def handle(self, event):
        """Actual handling of a single Consul event in a
        separate process."""
        try:
            log.debug("handle-key", key=event['Key'])
            prefix = event['Key'].split('/')[0]
            if not event['Value']:
                log.debug("ignore-key", key=event['Key'], reason='empty value')
                return
            getattr(self, prefix)(event)
        except Exception:
            log.exception('consul-handle-event', exc_info=True)

    def node(self, event):
        config = json.loads(event['Value'].decode('base64'))
        config['consul-generation'] = event['ModifyIndex']
        vm = config['name']
        if config['parameters']['machine'] != 'virtual':
            log.debug('ignore-consul-event',
                      machine=vm, reason='is a physical machine')
            return
        agent = Agent(vm, config)
        log.info('processing-consul-event', consul_event='node', machine=vm)
        with agent:
            agent.save_enc()
            agent.ensure()

    def snapshot(self, event):
        value = json.loads(event['Value'].decode('base64'))
        vm = value['vm']
        snapshot = value['snapshot'].encode('ascii')
        log_ = log.bind(snapshot=snapshot, machine=vm)
        try:
            agent = Agent(vm)
        except RuntimeError:
            log_.warning('snapshot-ignore', reason='failed loading config')
            return
        with agent:
            if not agent.belongs_to_this_host():
                log_.debug('snapshot-ignore', reason='foreign host')
                return
            log_.info('snapshot')
            agent.snapshot(snapshot)


def running(expected=True):
    def wrap(f):
        def checked(self, *args, **kw):
            if self.qemu.is_running() != expected:
                self.log.error(
                    f.__name__,
                    expected='VM {}running'.format('' if expected else 'not '))
                raise InvalidCommand(
                    'Invalid command: VM must {}be running to use `{}`.'.
                    format('' if expected else 'not ', f.__name__))
            return f(self, *args, **kw)
        return checked
    return wrap


def locked(f):
    # This is thread-safe *AS LONG* as every thread uses a separate instance
    # of the agent. Using multiple file descriptors will guarantee that the
    # lock can only be held once even within a single process.
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
    machine_type = 'pc-i440fx'
    vhost = False
    ceph_id = 'admin'
    timeout_graceful = 10
    vm_config_template_path = [
        '/etc/qemu/qemu.vm.cfg.in',
        pkg_resources.resource_filename(__name__, 'qemu.vm.cfg.in')
    ]
    consul_token = None

    # The binary generation is used to signal into a VM that the host
    # environment has changed in a way that requires a _cold_ reboot, thus
    # asking the VM to shut down cleanly so we can start it again. Change this
    # introducing a new Qemu machine type, or providing security fixes that
    # can't be applied by live migration.
    # The counter should be increased through the platform management as
    # this may change independently from fc.qemu releases.
    # If a VM is booted without a generation counter, then we assume that it's
    # at generation '' and it should perform a reboot at a generation different
    # from that.
    # We assume nothing about the generation itself - it's purely a string,
    # and we suggest a reboot if the current one differs from the booted one.
    # You could use numbers and count, you can use UUIDs, you can use speaking
    # names, release numbers, ...
    binary_generation = ''

    _configfile_fd = None

    def __init__(self, name, enc=None):
        # Update configuration values from system or test config.
        self.log = log.bind(machine=name)

        self.__dict__.update(sysconfig.agent)

        self.name = name
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

    @property
    def configfile(self):
        return '/etc/qemu/vm/{}.cfg'.format(self.name)

    def _load_enc(self):
        try:
            with open(self.configfile) as f:
                return yaml.safe_load(f)
        except IOError:
            self.log.error('unknown-vm')
            raise VMConfigNotFound("Could not load {}".format(self.configfile))

    @classmethod
    def handle_consul_event(cls, input=sys.stdin):
        events = json.load(input)
        if not events:
            return
        log.info('start-consul-events', count=len(events))
        threads = []
        for e in events:
            t = threading.Thread(target=_handle_consul_event, args=(e,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        log.info('finish-consul-events')

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
                self.log.exception('leave-subsystems', exc_info=True)

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
                self.mark_qemu_binary_generation()
                self.ensure_online_disk_size()
                self.ensure_online_disk_throttle()
            else:
                self.qemu.clean_run_files()
        else:
            self.log.warning('inconsistent-state')
            self.qemu.destroy()

    def ensure_offline(self):
        if not self.qemu.is_running():
            self.log.info('ensure-state', wanted='offline', found='offline',
                          action='none')
            return
        self.log.info('ensure-state', wanted='offline', found='online',
                      action='stop')
        self.stop()

    def ensure_online(self):
        if self.qemu.is_running():
            self.log.info('ensure-state', wanted='online', found='online',
                          action='')
            # re-register in case services got lost during Consul restart
            self.consul_register()
            return
        self.log.info('ensure-state', wanted='online', found='offline',
                      action='start')
        existing = locate_live_service(self.consul, 'qemu-' + self.name)
        if existing and existing['Address'] != self.this_host:
            self.log.info('check-migration', action='inmigration',
                          remote=existing['Address'])
            self.inmigrate()
        else:
            self.start()

    def mark_qemu_binary_generation(self):
        self.log.info('mark-qemu-binary-generation',
                      generation=self.binary_generation)
        try:
            self.qemu.write_file('/run/qemu-binary-generation-current',
                                 str(self.binary_generation))
        except socket.timeout:
            self.log.exception('mark-qemu-binary-generation')

    def ensure_online_disk_size(self):
        """Trigger block resize action for the root disk."""
        target_size = self.cfg['disk'] * (1024 ** 3)
        if self.ceph.root.size >= target_size:
            self.log.info('check-disk-size',
                          wanted=target_size,
                          found=self.ceph.root.size,
                          action='none')
            return
        self.log.info('check-disk-size', wanted=target_size,
                      found=self.ceph.root.size, action='resize')
        self.qemu.resize_root(target_size)

    def ensure_online_disk_throttle(self):
        """Ensure throttling settings."""
        target = self.cfg.get(
            'iops',
            self.qemu.throttle_by_pool.get(self.cfg['rbd_pool'], 250))
        devices = self.qemu.block_info()
        for device in devices.values():
            current = device['inserted']['iops']
            if current != target:
                self.log.info('ensure-throttle', device=device['device'],
                              target_iops=target, current_iops=current,
                              action='throttle')
                self.qemu.block_io_throttle(device['device'], target)
            else:
                self.log.info('ensure-throttle', device=device['device'],
                              target_iops=target, current_iops=current,
                              action='none')

    def consul_register(self):
        """Register running VM with Consul."""
        self.log.debug('register-consul')
        self.consul.agent.service.register(
            'qemu-{}'.format(self.name),
            address=self.this_host,
            interval='5s',
            check=('test -e /proc/$(< /run/qemu.{}.pid )/mem || exit 2'.
                   format(self.name)))

    def consul_deregister(self):
        """De-register non-running VM with Consul."""
        self.log.info('deregister-consul')
        self.consul.agent.service.deregister('qemu-{}'.format(self.name))

    @locked
    @running(False)
    def start(self):
        self.generate_config()
        self.ceph.start(self.enc, self.binary_generation)
        self.qemu.start()
        self.ensure_online_disk_throttle()
        self.consul_register()
        # We exit here without releasing the ceph lock in error cases
        # because the start may have failed because of an already running
        # process. Removing the lock in that case is dangerous. OTOH leaving
        # the lock is never dangerous. Lets go with that.

    @contextlib.contextmanager
    def frozen_vm(self):
        """Ensure a VM isn't making changes to its root disk.

        If the VM is running then freeze it (and thaw it upon exit).
        If a VM isn't running, this is a noop.

        """
        try:
            if self.qemu.is_running():
                try:
                    self.log.info('freeze', volume='root')
                    self.qemu.freeze()
                except socket.error as e:
                    self.log.error('freeze-failed',
                                   reason=str(e), action='continue',
                                   machine=self.name)
            yield
        finally:
            if self.qemu.is_running():
                try:
                    self.log.info('thaw', volume='root')
                    self.qemu.thaw()
                except socket.error as e:
                    self.log.error('thaw-failed', reason=str(e),
                                   action='retry')
                    try:
                        self.qemu.thaw()
                    except socket.error as e:
                        self.log.error('thaw-failed', reason=str(e),
                                       action='continue')
                        raise

    @locked
    def snapshot(self, snapshot):
        if snapshot in [x.snapname for x in self.ceph.root.snapshots]:
            self.log.info('snapshot-exists', snapshot=snapshot)
            return
        with self.frozen_vm():
            self.ceph.root.snapshots.create(snapshot)

    def status(self):
        """Determine status of the VM."""
        if self.qemu.is_running():
            status = 0
            self.log.info('vm-status', result='online')
        else:
            status = 1
            self.log.info('vm-status', result='offline')
        for volume in self.ceph.volumes:
            locker = volume.lock_status()
            self.log.info('rbd-status', volume=volume.fullname, locker=locker)
        return status

    def telnet(self):
        """Open telnet connection to the VM monitor."""
        self.log.info('connect-via-telnet')
        telnet = distutils.spawn.find_executable('telnet')
        os.execv(telnet, ('telnet', 'localhost', str(self.qemu.monitor_port)))

    @locked
    @running(True)
    def stop(self):
        timeout = TimeOut(self.timeout_graceful, interval=3)
        self.log.info('graceful-shutdown')
        try:
            self.qemu.graceful_shutdown()
        except (socket.error, RuntimeError):
            pass
        while timeout.tick():
            self.log.debug('checking-offline', remaining=timeout.remaining)
            if not self.qemu.is_running():
                self.log.info('vm-offline')
                self.ceph.stop()
                self.qemu.clean_run_files()
                self.consul_deregister()
                self.log.info('graceful-shutdown-completed')
                break
        else:
            self.log.warn('graceful-shutdown-failed', reason='timeout')
            self.kill()

    @locked
    def restart(self):
        self.log.info('restart-vm')
        self.stop()
        self.start()

    @locked
    @running(True)
    def kill(self):
        self.log.info('kill-vm')
        timeout = TimeOut(15, interval=1, raise_on_timeout=True)
        self.qemu.destroy()
        while timeout.tick():
            if not self.qemu.is_running():
                self.log.info('killed-vm')
                self.ceph.stop()
                self.qemu.clean_run_files()
                self.consul_deregister()
                break
        else:
            self.log.warning('kill-vm-failed', note='Check lock consistency.')

    @locked
    @running(False)
    def inmigrate(self):
        self.log.info('inmigrate')
        if self.ceph.is_unlocked():
            self.log.info('start-instead-of-inmigrate',
                          reason='no locks found')
            self.start()
            return
        server = IncomingServer(self)
        exitcode = server.run()
        if not exitcode:
            self.consul_register()
        self.log.info('inmigrate-finished', exitcode=exitcode)
        return exitcode

    @locked
    @running(True)
    def outmigrate(self):
        self.log.info('outmigrate')
        # re-register in case services got lost during Consul restart
        self.consul_register()
        client = Outgoing(self)
        exitcode = client()
        if not exitcode:
            self.consul_deregister()
        self.log.info('outmigrate-finished', exitcode=exitcode)
        return exitcode

    @locked
    def lock(self):
        self.log.info('assume-all-locks')
        for vol in self.ceph.volumes:
            vol.lock()

    @locked
    @running(False)
    def unlock(self):
        self.log.info('release-all-locks')
        self.ceph.stop()

    @running(False)
    def force_unlock(self):
        self.log.info('break-all-locks')
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
        result = any(substates) == all(substates)
        self.log.info('check-state-consistency',
                      is_consistent=result,
                      qemu=substates[0],
                      proc=substates[1],
                      ceph_lock=substates[2])
        return result

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
        self.log.debug('generate-config')
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
        machine_type = detect_current_machine_type(self.machine_type)
        self.qemu.config = tpl.format(
            accelerator=accelerator,
            machine_type=machine_type,
            network=''.join(netconfig), **self.cfg)
