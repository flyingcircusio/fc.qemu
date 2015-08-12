import ConfigParser
import os.path


class SysConfig(object):
    """A global config state registry.

    This is used to manage system-specific configuration overrides
    for values used in various places within fc.qemu.

    This provides the code for loading all those options from a central
    config file and to allow tests overriding those values gracefully.

    """

    def __init__(self):
        self.qemu = {}
        self.ceph = {}
        self.agent = {}

    def load_system_config(self):
        # System-wide config - pretty hacky
        sysconfig = ConfigParser.SafeConfigParser()
        sysconfig.read(os.path.dirname(__file__) + '/default.conf')
        sysconfig.read('/etc/qemu/fc-qemu.conf')

        self.qemu['migration_address'] = sysconfig.get(
            'qemu', 'migration-address')
        self.qemu['require_kvm'] = bool(sysconfig.get('qemu', 'accelerator'))
        self.qemu['vnc'] = sysconfig.get('qemu', 'vnc')
        self.qemu['max_downtime'] = sysconfig.getfloat('qemu', 'max-downtime')

        self.agent['accelerator'] = sysconfig.get('qemu', 'accelerator')
        self.agent['ceph_id'] = sysconfig.get('ceph', 'client-id')
        self.agent['consul_token'] = sysconfig.get('consul', 'access-token')
        self.agent['migration_ctl_address'] = sysconfig.get(
            'qemu', 'migration-ctl-address')
        self.agent['timeout_graceful'] = sysconfig.getint(
            'qemu', 'timeout-graceful')
        self.agent['this_host'] = sysconfig.get('ceph', 'lock_host')
        self.agent['vhost'] = sysconfig.getboolean('qemu', 'vhost')

        self.ceph['CEPH_CLIENT'] = sysconfig.get('ceph', 'client-id')
        self.ceph['CEPH_CLUSTER'] = sysconfig.get('ceph', 'cluster', 'ceph')
        self.ceph['CEPH_LOCK_HOST'] = sysconfig.get('ceph', 'lock_host')
        self.ceph['CREATE_VM'] = sysconfig.get('ceph', 'create-vm')
        self.ceph['SHRINK_VM'] = sysconfig.get('ceph', 'shrink-vm')


sysconfig = SysConfig()
