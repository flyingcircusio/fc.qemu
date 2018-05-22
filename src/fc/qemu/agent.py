from . import util
from .exc import InvalidCommand, VMConfigNotFound, VMStateInconsistent
from .exc import ConfigChanged
from .hazmat.ceph import Ceph
from .hazmat.qemu import Qemu, detect_current_machine_type
from .incoming import IncomingServer
from .outgoing import Outgoing
from .sysconfig import sysconfig
from .timeout import TimeOut
from .util import rewrite, locate_live_service, MiB, GiB, log
from multiprocessing.pool import ThreadPool
import consulate
import contextlib
import copy
import datetime
import distutils.spawn
import fcntl
import glob
import json
import math
import os
import os.path as p
import pkg_resources
import requests
import shutil
import socket
import subprocess
import sys
import time
import yaml


def _handle_consul_event(event):
    handler = ConsulEventHandler()
    handler.handle(event)


OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3


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
        except:  # noqa
            # This must be a bare-except as it protects threads and the main
            # loop from dying. It could be that I'm wrong, but I'm leaving this
            # in for good measure.
            log.exception('handle-key-failed',
                          key=event.get('Key', None),
                          exc_info=True)
        log.debug('finish-handle-key', key=event.get('Key', None))

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
        if agent.stage_new_config():
            log.info('launch-ensure', machine=vm)
            subprocess.Popen([sys.argv[0], '-D', 'ensure', vm], close_fds=True)
        else:
            log.info('ignore-consul-event',
                     machine=vm, reason='config is unchanged')

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
            log_.info('snapshot')
            agent.snapshot(snapshot, keep=0)


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


def locked(blocking=True):
    # This is thread-safe *AS LONG* as every thread uses a separate instance
    # of the agent. Using multiple file descriptors will guarantee that the
    # lock can only be held once even within a single process.
    #
    # However, we ensure locking status even for re-entrant / recursive usage.
    # For that we keep a counter how often we successfully acquired the lock
    # and then unlock when we're back to zero.
    def lock_decorator(f):
        def locked_func(self, *args, **kw):
            # New lockfile behaviour: lock with a global file that is really
            # only used for this purpose and is never replaced.
            self.log.debug('acquire-lock', target=self.lockfile)
            if not self._lockfile_fd:
                if not os.path.exists(self.lockfile):
                    open(self.lockfile, 'a+').close()
                self._lockfile_fd = os.open(self.lockfile, os.O_RDONLY)
            mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0)
            try:
                fcntl.flock(self._lockfile_fd, mode)
            except IOError:
                # This happens in nonblocking mode and we just give up as
                # that's what's expected to speed up things.
                self.log.info('acquire-lock', result='failed', action='exit',
                              mode='nonblocking')
                return os.EX_TEMPFAIL
            self.log.debug('acquire-lock',
                           target=self.lockfile, result='locked')

            self._lock_count += 1
            self.log.debug('lock-status', count=self._lock_count)
            try:
                return f(self, *args, **kw)
            finally:
                self._lock_count -= 1
                self.log.debug('lock-status', count=self._lock_count)

                if self._lock_count == 0:
                    # New
                    try:
                        self.log.debug('release-lock', target=self.lockfile)
                        fcntl.flock(self._lockfile_fd, fcntl.LOCK_UN)
                        self.log.debug('release-lock', target=self.lockfile,
                                       result='unlocked')
                    except:  # noqa
                        self.log.debug('release-lock', exc_info=True,
                                       target=self.lockfile, result='error')
                        pass
        return locked_func
    return lock_decorator


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
    binary_generation = 0

    # For upgrade-purposes we're running an old and a new locking mechanism
    # in step-lock. We used to lock the configfile but we're using rename to
    # update it atomically. That's not compatible and will result in a consul
    # event replacing the file and then getting a lock on the new file while
    # there still is another process running. This will result in problems
    # accessing the QMP socket as that only accepts a single connection and
    # will then time out.
    _lock_count = 0
    _lockfile_fd = None
    _configfile_fd = None

    def __init__(self, name, enc=None):
        # Update configuration values from system or test config.
        self.log = log.bind(machine=name)

        self.__dict__.update(sysconfig.agent)

        self.name = name

        # The ENC data is the parsed configuration from the config file, or
        # from consul (or from wherever). This is intended to stay true to
        # the config file format (not adding computed values or manipulating
        # them) so that we can generate a new config file based on this data
        # through `stage_new_config()`.
        if enc is not None:
            self.enc = enc
        else:
            self.enc = self._load_enc()

        self.consul = consulate.Consul(token=self.consul_token)

    @property
    def configfile(self):
        return '/etc/qemu/vm/{}.cfg'.format(self.name)

    @property
    def configfile_staging(self):
        return '/etc/qemu/vm/.{}.cfg.staging'.format(self.name)

    @property
    def lockfile(self):
        return '/run/qemu.{}.lock'.format(self.name)

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
        pool = ThreadPool(sysconfig.agent.get('consul_event_threads', 3))
        for event in sorted(events, key=lambda e: e.get('Key')):
            pool.apply_async(_handle_consul_event, (event,))
            time.sleep(0.05)
        pool.close()
        pool.join()
        log.info('finish-consul-events')

    @classmethod
    def _vm_agents_for_host(cls):
        for candidate in sorted(glob.glob('/run/qemu.*.pid')):
            name = candidate.replace('/run/qemu.', '')
            name = name.replace('.pid', '')
            try:
                agent = Agent(name)
                yield agent
            except Exception:
                log.exception('load-agent', machine=name, exc_info=True)

    @classmethod
    def ls(cls):
        """List all VMs that this host knows about.

        This means specifically that we have status (PID) files for the
        VM in the expected places.

        Gives a quick status for each VM.
        """
        for vm in cls._vm_agents_for_host():
            with vm:
                running = vm.qemu.process_exists()

                if running:
                    vm_mem = vm.qemu.proc().memory_full_info()

                    expected_size = (
                        vm.cfg['memory'] * 1024 * 1024 +
                        vm.qemu.vm_expected_overhead * 1024 * 1024)

                    log.info('online',
                             machine=vm.name,
                             cores=vm.cfg['cores'],
                             memory_booked='{:,.0f}'.format(vm.cfg['memory']),
                             memory_pss='{:,.0f}'.format(vm_mem.pss / MiB),
                             memory_swap='{:,.0f}'.format(vm_mem.swap / MiB))

                else:
                    log.info('offline', machine=vm.name)

    @classmethod
    def check(cls):
        """Perform a health check of this host from a Qemu perspective.

        Checks:

        * no VM exceeds their PSS by guest memory + 2x expected overhead
        * VMs have very little swap (<1 GiB)
        * the total of the VMs PSS does not exceed
          total of guest + expected overhead

        Sets exit code according to the Nagios specification.

        """
        vms = list(cls._vm_agents_for_host())

        large_overhead_vms = []
        swapping_vms = []
        total_guest_and_overhead = 0
        expected_guest_and_overhead = 0

        # individual VMs ok?
        for vm in vms:
            with vm:
                try:
                    vm_mem = vm.qemu.proc().memory_full_info()
                except Exception:
                    # It's likely that the process went away while we analyzed
                    # it. Ignore.
                    continue
                if vm_mem.swap > 1 * GiB:
                    swapping_vms.append(vm)
                expected_size = (
                    vm.cfg['memory'] * MiB +
                    2 * vm.qemu.vm_expected_overhead * MiB)
                expected_guest_and_overhead += (
                    vm.cfg['memory'] * MiB +
                    vm.qemu.vm_expected_overhead * MiB)
                total_guest_and_overhead += vm_mem.pss
                if vm_mem.pss > expected_size:
                    large_overhead_vms.append(vm)

        output = []
        result = OK
        if large_overhead_vms:
            result = WARNING
            output.append('VMs with large overhead: ' +
                          ','.join(x.name for x in large_overhead_vms))
        if swapping_vms:
            result = WARNING
            output.append('VMs swapping:' +
                          ','.join(x.name for x in swapping_vms))
        if total_guest_and_overhead > expected_guest_and_overhead:
            result = CRITICAL
            output.append('High total overhead')

        if result is OK:
            output.insert(0, 'OK')
        elif result is WARNING:
            output.insert(0, 'WARNING')
        elif result is CRITICAL:
            output.insert(0, 'CRITICAL')
        else:
            output.insert(0, 'UNKNOWN')

        output.insert(
            1, '{} VMs'.format(len(vms)))
        output.insert(
            2, '{:,.0f} MiB used'.format(total_guest_and_overhead / MiB))
        output.insert(
            3, '{:,.0f} MiB expected'.format(expected_guest_and_overhead / MiB))

        print(' - '.join(output))

        return result

    def stage_new_config(self):
        """Save the current config on the agent into a staging config file.

        This method is safe to call outside of the agent's context manager.

        """
        # Ensure the config directory exists
        if not p.isdir(p.dirname(self.configfile)):
            os.makedirs(p.dirname(self.configfile))

        # Ensure the staging file exists.
        open(self.configfile_staging, 'a+').close()
        staging_lock = os.open(self.configfile_staging, os.O_RDONLY)
        fcntl.flock(staging_lock, fcntl.LOCK_EX)
        self.log.debug(
            'acquire-staging-lock', target=self.configfile_staging,
            result='locked')
        try:
            # Verify generation of config to protect against lost updates.
            with open(self.configfile_staging, 'r') as current_staging:
                try:
                    current_staging_config = yaml.safe_load(current_staging)
                    current_generation = current_staging_config[
                        'consul-generation']
                except Exception:
                    self.log.debug('inconsistent-staging-config')
                    pass
                else:
                    if current_generation >= self.enc['consul-generation']:
                        # Stop right here, do not write a new config if the
                        # existing one is newer (or as new) already.
                        self.log.debug('ignore-old-update',
                                       existing=current_generation,
                                       update=self.enc['consul-generation'])
                        return False

            # The config is either newer, or there is no staging config, or it
            # is inconsistent. Let's update it.
            self.log.debug('save-staging-config')
            # Update the file in place to avoid lock breaking.
            with open(self.configfile_staging, 'w') as new_staging:
                yaml.safe_dump(self.enc, new_staging)
            os.chmod(self.configfile_staging, 0o644)
            return True
        finally:
            fcntl.flock(staging_lock, fcntl.LOCK_UN)
            os.close(staging_lock)
            self.log.debug(
                'release-staging-lock',
                target=self.configfile_staging, result='released')

    @locked()
    def activate_new_config(self):
        """Activate the current staged config (if any).

        After calling this method, the agent's context manager must be
        re-entered to activate the changed configuration.

        """
        if not os.path.exists(self.configfile_staging):
            self.log.debug('check-staging-config', result='none')
            return False

        staging_lock = os.open(self.configfile_staging, os.O_RDONLY)
        fcntl.flock(staging_lock, fcntl.LOCK_EX)
        self.log.debug(
            'acquire-staging-lock', target=self.configfile_staging,
            result='locked')
        try:
            # Verify generation of config to protect against lost updates.
            with open(self.configfile_staging, 'r') as current_staging:
                try:
                    current_staging_config = yaml.safe_load(current_staging)
                    staging_generation = current_staging_config[
                        'consul-generation']
                except Exception:
                    self.log.debug('update-check',
                                   result='inconsistent', action='purge')
                    os.unlink(self.configfile_staging)
                    return False
                else:
                    if staging_generation <= self.enc['consul-generation']:
                        # Stop right here, do not write a new config if the
                        # existing one is newer (or as new) already.
                        self.log.debug(
                            'update-check',
                            result='stale-update', action='ignore',
                            update=staging_generation,
                            current=self.enc['consul-generation'])
                        # The old staging file needs to stay around so that
                        # the consul writer knows whether to launch an ensure
                        # agent or not.
                        return False
                self.log.debug('update-check',
                               result='update-available',
                               action='update',
                               update=staging_generation,
                               current=self.enc['consul-generation'])

            # The config seems consistent and newer, lets update.
            # We can replace the config file because that one is protected
            # by the global VM lock.
            shutil.copy2(self.configfile_staging, self.configfile)
            self.enc = self._load_enc()
            return True
        finally:
            fcntl.flock(staging_lock, fcntl.LOCK_UN)
            os.close(staging_lock)
            self.log.debug(
                'release-staging-lock', target=self.configfile_staging,
                result='released')

    def has_new_config(self):
        if not os.path.exists(self.configfile_staging):
            self.log.debug('check-staging-config', result='none')
            return False

        staging_lock = os.open(self.configfile_staging, os.O_RDONLY)
        fcntl.flock(staging_lock, fcntl.LOCK_EX)
        self.log.debug(
            'acquire-staging-lock', target=self.configfile_staging,
            result='locked')
        try:
            # Verify generation of config to protect against lost updates.
            with open(self.configfile_staging, 'r') as current_staging:
                try:
                    current_staging_config = yaml.safe_load(current_staging)
                    staging_generation = current_staging_config[
                        'consul-generation']
                except Exception:
                    self.log.debug('update-check',
                                   result='inconsistent', action='purge')
                    os.unlink(self.configfile_staging)
                    return False
                else:
                    if staging_generation <= self.enc['consul-generation']:
                        # Stop right here, do not write a new config if the
                        # existing one is newer (or as new) already.
                        self.log.debug('update-check',
                                       result='stale-update',
                                       action='ignore',
                                       update=staging_generation,
                                       current=self.enc['consul-generation'])
                        # The old staging file needs to stay around so that
                        # the consul writer knows whether to launch an ensure
                        # agent or not.
                        return False
                self.log.debug('update-check',
                               result='update-available',
                               action='update',
                               update=staging_generation,
                               current=self.enc['consul-generation'])
            return True
        finally:
            fcntl.flock(staging_lock, fcntl.LOCK_UN)
            os.close(staging_lock)
            self.log.debug(
                'release-staging-lock', target=self.configfile_staging,
                result='released')

    def __enter__(self):
        # Allow updating our config by exiting/entering after setting new ENC
        # data.
        # This copy means we can't manipulate cfg to update ENC data, which is
        # OK. We did this at some point and we're adding computed data to the
        # `cfg` structure, that we do not want to accidentally reflect back
        # into the config file.
        self.cfg = copy.copy(self.enc['parameters'])
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

        for c in self.contexts:
            c.__enter__()

    def __exit__(self, exc_value, exc_type, exc_tb):
        for c in self.contexts:
            try:
                c.__exit__(exc_value, exc_type, exc_tb)
            except Exception:
                self.log.exception('leave-subsystems', exc_info=True)

    def ensure(self):
        # Run the config activation code at least once, but then only if there
        # are still config changes around. We do this because multiple updates
        # may be piling up and we want to catch up as quickly as possible but
        # we only stage the updates and don't want to spawn an agent for each
        # update of each VM. So if an agent is already running it checks again
        # after it did something and then keeps going. OTOH agents that are
        # spawned after staging a new config will give up immediately when the
        # notice another agent running.

        # The __exit__/__enter__ dance is to simplify handling this special
        # case where we have to handle this method differently as it keeps
        # entering/exiting while cycling through the config changes. This keeps
        # the API and unit tests a bit cleaner even though we enter/exit one
        # time too many.
        self.__exit__(None, None, None)
        try:
            first = True
            while self.has_new_config() or first:
                self.log.info('running-ensure',
                              generation=self.enc['consul-generation'])
                first = False
                try:
                    locking_code = self.ensure_()
                    if locking_code == os.EX_TEMPFAIL:
                        # We didn't get the lock, so someone else is
                        # already around who will pick up the (additional)
                        # changed config later.
                        break
                except ConfigChanged:
                    # Well then. Let's try this again.
                    continue
        finally:
            self.__enter__()
        self.log.debug('changes-settled')

    @locked(blocking=False)
    def ensure_(self):
        self.activate_new_config()
        with self:
            # Host assignment is a bit tricky: we decided to not interpret an
            # *empty* cfg['kvm_host'] as "should not be running here" for the
            # sake of not accidentally causing downtime.
            try:
                if not self.cfg['online']:
                    # Wanted offline.
                    self.ensure_offline()

                elif self.cfg['kvm_host']:
                    if self.cfg['kvm_host'] != self.this_host:
                        # Wanted online, but explicitly on a different host.
                        self.ensure_online_remote()
                    else:
                        # Wanted online, and it's OK if it's running here, but
                        # I'll only start it if it really is wanted here.
                        self.ensure_online_local()

                self.raise_if_inconsistent()
            except VMStateInconsistent:
                # Last-resort seat-belt to verify that we ended up in a
                # consistent state. Inconsistent states result in the VM being
                # forcefully terminated.
                self.log.error('inconsistent-state', action='destroy',
                               exc_info=True)
                self.qemu.destroy()

    def ensure_offline(self):
        if self.qemu.is_running():
            self.log.info(
                'ensure-state', wanted='offline', found='online',
                action='stop')
            self.stop()
        else:
            self.log.info(
                'ensure-state', wanted='offline', found='offline',
                action='none')

    def ensure_online_remote(self):
        if not self.qemu.is_running():
            # This cleans up potential left-overs from a previous migration.
            self.ceph.stop()
            self.consul_deregister()
            self.cleanup()
            return

        migration_errors = self.outmigrate()
        if migration_errors:
            # Stop here, do not clean up. This is fine as we may not have
            # made contact with our destination host. We keep trying later.
            return

        # Ensure ongoing maintenance for a VM that was potentially outmigrated.
        # But where the outmigration may have left bits and pieces.
        self.ceph.stop()
        self.consul_deregister()
        self.cleanup()

    def ensure_online_local(self):
        # Ensure state
        agent_likely_ready = True
        if not self.qemu.is_running():
            existing = locate_live_service(self.consul, 'qemu-' + self.name)
            if existing and existing['Address'] != self.this_host:
                self.log.info(
                    'ensure-state', wanted='online', found='offline',
                    action='inmigrate', remote=existing['Address'])
                exitcode = self.inmigrate()
                if exitcode:
                    # This is suboptimal: I hate error returns,
                    # but the main method is also a command. If we did
                    # not succeed in migrating, then I also don't want the
                    # consul registration to happen.
                    return
            else:
                self.log.info(
                    'ensure-state', wanted='online', found='offline',
                    action='start')
                self.start()
                agent_likely_ready = False
        else:
            self.log.info(
                'ensure-state', wanted='online', found='online', action='')

        # Perform ongoing adjustments of the operational parameters of the
        # running VM.
        self.consul_register()
        self.ensure_online_disk_size()
        self.ensure_online_disk_throttle()
        self.ensure_watchdog()
        if agent_likely_ready:
            # This requires guest agent interaction and we should only
            # perform this when we haven't recently booted the machine to
            # reduce the time we're unnecessarily waiting for timeouts.
            self.ensure_thawed()
            self.mark_qemu_binary_generation()

    def cleanup(self):
        """Removes various run and tmp files."""
        self.qemu.clean_run_files()
        for tmp in glob.glob(self.configfile + '?*'):
            os.unlink(tmp)

    def ensure_thawed(self):
        self.log.info('ensure-thawed', volume='root')
        try:
            self.qemu.thaw()
        except Exception as e:
            self.log.error('ensure-thawed-failed', reason=str(e))

    def mark_qemu_binary_generation(self):
        self.log.info(
            'mark-qemu-binary-generation', generation=self.binary_generation)
        try:
            self.qemu.write_file('/run/qemu-binary-generation-current',
                                 str(self.binary_generation) + '\n')
        except Exception as e:
            self.log.error('mark-qemu-binary-generation', reason=str(e))

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

    def ensure_watchdog(self, action='none'):
        """Ensure watchdog settings."""
        self.log.info('ensure-watchdog', action=action)
        self.qemu.watchdog_action(action)

    @property
    def svc_name(self):
        """Consul service name."""
        return 'qemu-{}'.format(self.name)

    def consul_register(self):
        """Register running VM with Consul."""
        self.log.debug('consul-register')
        self.consul.agent.service.register(
            self.svc_name,
            address=self.this_host,
            interval='5s',
            check=('test -e /proc/$(< /run/qemu.{}.pid )/mem || exit 2'.
                   format(self.name)))

    def consul_deregister(self):
        """De-register non-running VM with Consul."""
        try:
            if self.svc_name not in self.consul.agent.services()[0]:
                return
            self.log.info('consul-deregister')
            self.consul.agent.service.deregister('qemu-{}'.format(self.name))
        except requests.exceptions.ConnectionError:
            pass
        except Exception:
            self.log.exception('consul-deregister-failed', exc_info=True)

    @locked()
    @running(False)
    def start(self):
        self.generate_config()
        self.ceph.start(self.enc, self.binary_generation)
        self.qemu.start()
        self.consul_register()
        self.ensure_online_disk_throttle()
        self.ensure_watchdog()
        # We exit here without releasing the ceph lock in error cases
        # because the start may have failed because of an already running
        # process. Removing the lock in that case is dangerous. OTOH leaving
        # the lock is never dangerous. Lets go with that.

    @contextlib.contextmanager
    def frozen_vm(self):
        """Ensure a VM has a non-changing filesystem, that is clean enough
        to be mounted with a straight-forward journal replay.

        This is implemented using the 'fs-freeze' API through the Qemu guest
        agent.

        As we can't indicate the cleanliness of a non-running
        (potentially crashed) VM we do only signal a successful freeze iff
        the freeze happened and succeeded.

        """
        frozen = False
        try:
            if self.qemu.is_running():
                self.log.info('freeze', volume='root')
                try:
                    self.qemu.freeze()
                    frozen = True
                except Exception as e:
                    self.log.error('freeze-failed',
                                   reason=str(e), action='continue',
                                   machine=self.name)
            yield frozen
        finally:
            if self.qemu.is_running():
                try:
                    self.log.info('thaw', volume='root')
                    self.qemu.thaw()
                except Exception as e:
                    self.log.error('thaw-failed', reason=str(e),
                                   action='retry')
                    try:
                        self.qemu.thaw()
                    except Exception as e:
                        self.log.error('thaw-failed', reason=str(e),
                                       action='continue')
                        raise

    @locked()
    def snapshot(self, snapshot, keep=0):
        """Guarantees a _consistent_ snapshot to be created.

        If we can't properly freeze the VM then whoever needs a (consistent)
        snapshot needs to figure out whether to go forward with an
        inconsistent snapshot.
        """
        if keep:
            until = util.today() + datetime.timedelta(days=keep)
            snapshot = snapshot + '-keep-until-' + until.strftime('%Y%m%d')
        if snapshot in [x.snapname for x in self.ceph.root.snapshots]:
            self.log.info('snapshot-exists', snapshot=snapshot)
            return
        self.log.debug('snapshot-create', name=snapshot)
        with self.frozen_vm() as frozen:
            if frozen:
                self.ceph.root.snapshots.create(snapshot)
            else:
                self.log.debug('snapshot-ignore', reason='not frozen')

    @locked()
    def status(self):
        """Determine status of the VM."""
        if self.qemu.is_running():
            status = 0
            self.log.info('vm-status', result='online')
            for device in self.qemu.block_info().values():
                self.log.info('disk-throttle',
                              device=device['device'],
                              iops=device['inserted']['iops'])
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

    @locked()
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
                self.consul_deregister()
                self.cleanup()
                self.log.info('graceful-shutdown-completed')
                break
        else:
            self.log.warn('graceful-shutdown-failed', reason='timeout')
            self.kill()

    @locked()
    def restart(self):
        self.log.info('restart-vm')
        self.stop()
        self.start()

    @locked()
    @running(True)
    def kill(self):
        self.log.info('kill-vm')
        timeout = TimeOut(15, interval=1, raise_on_timeout=True)
        self.qemu.destroy()
        while timeout.tick():
            if not self.qemu.is_running():
                self.log.info('killed-vm')
                self.ceph.stop()
                self.consul_deregister()
                self.cleanup()
                break
        else:
            self.log.warning('kill-vm-failed', note='Check lock consistency.')

    @locked()
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

    @locked()
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

    @locked()
    def lock(self):
        self.log.info('assume-all-locks')
        for vol in self.ceph.volumes:
            vol.lock()

    @locked()
    @running(False)
    def unlock(self):
        self.log.info('release-all-locks')
        self.ceph.stop()

    @running(False)
    def force_unlock(self):
        self.log.info('break-all-locks')
        self.ceph.force_unlock()

    # Helper methods
    def raise_if_inconsistent(self):
        """Raise an VMStateInconsistent error if the VM state is not
        consistent.

        Either all of the following or none of the following must be true:

        - The process belonging to the PID we know exists.
        - The QMP monitor reliably tells us that the Qemu status of the
          VM/CPUs is "running"
        - We have locked the volumes in Ceph.

        If they are not the same, then the state is considered inconsistent
        and this method will return False.

        """
        state = VMStateInconsistent()
        state.qemu = self.qemu.is_running()
        state.proc = bool(self.qemu.proc())
        state.ceph_lock = self.ceph.locked_by_me()
        self.log.debug('check-state-consistency',
                       is_consistent=state.is_consistent(),
                       qemu=state.qemu,
                       proc=state.proc,
                       ceph_lock=state.ceph_lock)
        if not state.is_consistent():
            raise state

    # CAREFUL: changing anything in this config files will require maintenance
    # w/ reboot of all VMs in the infrastructure. Need to increase the
    # binary generation for that, though.
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
            # We use this '-m' flag to find what a running VM is actually
            # using at the moment. If this flag is changed then that code must
            # be adapted as well. This is used in incoming.py and qemu.py.
            '-m {memory}',
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
        accelerator = ('  accel = "{}"'.format(self.accelerator)
                       if self.accelerator else '')
        machine_type = detect_current_machine_type(self.machine_type)
        self.qemu.config = tpl.format(
            accelerator=accelerator,
            machine_type=machine_type,
            disk_cache_mode=self.qemu.disk_cache_mode,
            network=''.join(netconfig), **self.cfg)
