import ConfigParser
import socket
import StringIO

CEPH_CLUSTER = 'ceph'
CEPH_ID = socket.gethostname()


class Config(object):
    """Kludgy parser for /etc/conf.d/kvm.{VM}.

    We pretend that this is a INI file by prepending a section
    identifier. Of course the code is supposed to be source-able by the
    shell, but the syntax is similar enough to be accepted by
    ConfigParser. I wonder if this is going to break someday...
    """

    @classmethod
    def from_file(cls, fn):
        with open(fn) as f:
            ini = StringIO.StringIO('[vm]\n' + f.read())
        cp = ConfigParser.SafeConfigParser()
        cp.readfp(ini)
        return cls(cp)

    def __init__(self, config):
        self.config = config

    @property
    def rg(self):
        return self.config.get('vm', 'vmrg').strip('"')

    @property
    def monitor_port(self):
        return int(self.config.get('vm', 'monitor_port').strip('"'))
