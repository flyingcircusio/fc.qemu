"""Lock vocabulary shared by the Ceph and volume layers.

Lives in its own module because `volume.py` needs `LockMode` at runtime and
cannot import `ceph.py`, which imports `volume.py`.
"""

import enum


class LockMode(enum.Enum):
    UNKNOWN = 0
    RBD = 1
    RADOS = 2

    def __repr__(self) -> str:
        return super().__str__()
