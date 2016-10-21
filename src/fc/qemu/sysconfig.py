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
        self.cp = None

    def get(self, section, option, default=None):
        try:
            return self.cp.get(section, option)
        except ConfigParser.NoOptionError:
            if default:
                return default
            pass

    def read_config_files(self):
        """Tries to open fc-qemu.conf at various location."""
        self.cp = ConfigParser.SafeConfigParser()
        self.cp.read(os.path.dirname(__file__) + '/default.conf')
        self.cp.read('/etc/qemu/fc-qemu.conf')
        if 'qemu' not in self.cp.sections():
            raise RuntimeError('error while reading config file: '
                               'section [qemu] not found')

    def load_system_config(self):
        self.read_config_files()

        self.qemu['migration_address'] = self.get(
            'qemu', 'migration-address')
        self.qemu['require_kvm'] = bool(self.get('qemu', 'accelerator'))
        self.qemu['vnc'] = self.get('qemu', 'vnc')
        self.qemu['max_downtime'] = self.cp.getfloat('qemu', 'max-downtime')

        self.qemu['throttle_by_pool'] = tbp = {}
        for pool, iops in self.cp.items('qemu-throttle-by-pool'):
            tbp[pool] = int(iops)

        self.agent['accelerator'] = self.get('qemu', 'accelerator')
        self.agent['machine_type'] = self.get('qemu', 'machine-type')
        self.agent['ceph_id'] = self.get('ceph', 'client-id')
        self.agent['consul_token'] = self.get('consul', 'access-token')
        self.agent['migration_ctl_address'] = self.get(
            'qemu', 'migration-ctl-address')
        self.agent['binary_generation'] = self.cp.getint(
            'qemu', 'binary-generation')
        self.agent['timeout_graceful'] = self.cp.getint(
            'qemu', 'timeout-graceful')
        self.agent['this_host'] = self.get('ceph', 'lock_host')
        self.agent['vhost'] = self.cp.getboolean('qemu', 'vhost')

        self.ceph['CEPH_CLIENT'] = self.get('ceph', 'client-id', 'admin')
        self.ceph['CEPH_CLUSTER'] = self.get('ceph', 'cluster', 'ceph')
        self.ceph['CEPH_CONF'] = self.get(
            'ceph', 'ceph-conf',
            '/etc/ceph/{}.conf'.format(self.ceph['CEPH_CLUSTER']))
        self.ceph['CEPH_LOCK_HOST'] = self.get('ceph', 'lock_host')
        self.ceph['CREATE_VM'] = self.get('ceph', 'create-vm')
        self.ceph['MKFS_XFS'] = self.get(
            'ceph', 'mkfs-xfs', '-q -f -K -m crc=1,finobt=1 -d su=4m,sw=1')
        self.ceph['MKFS_EXT4'] = self.get(
            'ceph', 'mkfs-ext4', '-q -m 1 -E nodiscard')


sysconfig = SysConfig()
