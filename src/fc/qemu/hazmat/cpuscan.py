from .qemu import Qemu
from fc.qemu.timeout import TimeOut, TimeoutError
from fc.qemu.util import log
import itertools
import os
import subprocess

FNULL = open(os.devnull, 'w')


IDENTIFIERS = {
    'AuthenticAMD': [
        'qemu64-v1',
        'EPYC-v1',
        'EPYC-v2'
    ],

    'GenuineIntel': [
        'Broadwell-v1',
        'Broadwell-v2',
        'Broadwell-v3',
        'Broadwell-v4',
        'Cascadelake-Server-v1',
        'Cascadelake-Server-v2',
        'Haswell-v1',
        'Haswell-v2',
        'Haswell-v3',
        'Haswell-v4',
        'IvyBridge-v1',
        'IvyBridge-v2',
        'Nehalem-v1',
        'Nehalem-v2',
        'SandyBridge-v1',
        'SandyBridge-v2',
        'Skylake-Server-v1',
        'Skylake-Server-v2',
        'Westmere-v1',
        'Westmere-v2',
        'qemu64-v1'
    ]

}

class Model(object):

    architecture = None
    identifier = None
    description = None

    def __init__(self, architecture, identifier, description):
        self.architecture = architecture
        self.identifier = identifier
        self.description = description


class Variation(object):

    model = None
    flags = ()

    def __init__(self, model, flags):
        self.model = model
        self.flags = tuple(sorted(set(flags)))

    @property
    def cpu_arg(self):
        return ",".join((self.model.identifier,) + self.flags)


def scan_cpus():
    for line in open('/proc/cpuinfo'):
        if not line.startswith('vendor_id'):
            continue
        _, vendor = line.split(':')
        vendor = vendor.strip()
        break
    else:
        raise RuntimeError('Could not determine CPU vendor.')

    models = []
    for identifier in IDENTIFIERS[vendor]:
        models.append(Model('x86', identifier, ''))

    # Determine combinations with additional desirable flags
    desirable_flags = ["pcid", "spec-ctrl", "ssbd", "pde1gb"]
    desirable_combinations = []
    for L in range(0, len(desirable_flags) + 1):
        desirable_combinations.extend(itertools.combinations(desirable_flags, L))

    variations = []

    for model in models:
        for combination in desirable_combinations:
            variations.append(Variation(model, combination))

    valid_models = []

    for variation in variations:
        log.debug('test-cpu', id=variation.cpu_arg, description=variation.model.description, architecture=variation.model.architecture)
        task = subprocess.Popen(
            [
                Qemu.executable,
                "-cpu",
                variation.cpu_arg + ',enforce',
                "-accel", "kvm",
                "-enable-kvm",
                "-monitor", "stdio",
                "-display", "none",
                "-nodefaults",
            ],
            stdin=subprocess.PIPE,
            stdout=FNULL,
            stderr=FNULL,
        )
        task.communicate(input="quit\n")
        if not task.wait():
            valid_models.append(variation)

    return valid_models
