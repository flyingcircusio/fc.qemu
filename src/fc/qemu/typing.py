from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict

from structlog import BoundLogger


class SnapshotInfo(TypedDict):
    name: str
    id: str
    size: int


class ConsulEvent(TypedDict):
    Key: str
    Value: str
    ModifyIndex: int


class SnapshotEventDict(TypedDict):
    vm: str
    snapshot: str


EncDict = TypedDict(
    "EncDict",
    {
        "parameters": "EncParametersDict",
        "consul-generation": int,
        "name": str,
    },
)


VolumeSuffix = Literal["cidata", "root", "swap", "tmp"]

# The size of a volume is stored in the ENC parameters under a key derived
# from the volume suffix. Type checkers can only index a TypedDict with
# literal keys, so volume specifications have to name their key explicitly
# instead of computing `f"{suffix}_size"`.
VolumeSizeKey = Literal["cidata_size", "root_size", "swap_size", "tmp_size"]


class RouteDict(TypedDict):
    dst: str
    gateway: NotRequired[str]
    dev: NotRequired[str]
    scope: NotRequired[str]
    flags: list[str]
    protocol: NotRequired[str]
    pref: NotRequired[str]
    metric: NotRequired[int]


class DefaultRouteDict(TypedDict):
    dst: Literal["default"]
    gateway: str


class EncParametersDict(TypedDict):
    id: int
    # keep-sorted: start
    agent: "AgentDict"
    binary_generation: int
    ceph_id: str
    cidata_size: int
    config_file: str
    cores: int
    cpu_model: str
    directory_password: str
    directory_ring: Literal[0, 1]
    directory_url: NotRequired[str]
    disk: int
    environment_class: str
    environment_class_type: str
    interfaces: dict[str, "InterfaceDict"]
    kvm_host: str | None
    machine: Literal["physical", "virtual"]
    memory: int
    name: str
    online: bool
    pid_file: str
    rbd_pool: str
    resource_group: str
    root_size: int
    swap_size: int
    tmp_size: int
    # keep-sorted: end


class AgentDict(TypedDict):
    prefix: Path
    network: "AgentNetworkConfigDict"


class AgentNetworkConfigDict(TypedDict):
    use_vhost: bool


class InterfaceDict(TypedDict):
    # keep-sorted: start
    linktype: NotRequired[str]
    gateways: dict[str, str]
    routed: bool
    networks: dict[str, list[str]]
    network_number: int | None
    mac: str
    # keep-sorted: end


class GuestPropertiesDict(TypedDict):
    # keep-sorted: start
    binary_generation: int
    cpu_model: str
    rbd_pool: str
    # keep-sorted: end


class SupportsLocalLock(Protocol):
    # keep-sorted: start
    log: BoundLogger
    lock_file_fd: int | None
    lock_count: int
    # keep-sorted: end

    @property
    def lock_file(self) -> Path: ...


class SupportsGlobalLock(Protocol):
    # keep-sorted: start
    prefix: Path
    log: BoundLogger
    global_lock_fd: int | None
    global_lock_count: int
    # keep-sorted: end
