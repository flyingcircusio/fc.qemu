from .qemu import Qemu
from fc.qemu.timeout import TimeOut, TimeoutError
from fc.qemu.util import log
import itertools
import os
import subprocess

FNULL = open(os.devnull, 'w')


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
    # Determine processor models/flavours supported by Qemu
    result = subprocess.check_output([Qemu.executable, "-cpu", "help"])
    lines = result.decode("ascii").splitlines()

    models = []

    for line in lines:
        if not line.strip():
            # Empty lines signal that the list of supported CPUs is finished
            # and we're now entering the section where known flags are shown.
            break
        splitted = line.split(None, 2)
        architecture = splitted[0]
        identifier = splitted[1]
        if len(splitted) == 3:
            description = splitted[2].strip()
        else:
            description = ""
        models.append(Model(architecture, identifier, description))

    # Reduce list of models by hard limits
    models = [m for m in models if m.architecture == "x86"]
    IGNORED_MODELS = [
        "486",
        "kvm32",
        "kvm64",
        "qemu32",
        "host",
        "coreduo",
        "core2duo",
        "pentium",
        "pentium2",
        "pentium3",
        "athlon",
        "Penryn",
        "base",
        "max",
        "-Client",
    ]
    for ignore in IGNORED_MODELS:
        models = [m for m in models if ignore not in m.identifier]
    IGNORED_DESCRIPTION = ["AMD", "Atom", "Celeron", "alias", "Hygon"]
    for ignore in IGNORED_DESCRIPTION:
        models = [m for m in models if ignore not in m.description]

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
