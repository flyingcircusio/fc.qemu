class LockError(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


class DestructionError(RuntimeError):
    """Failed to destroy a running VM."""
    pass


class QemuNotRunning(Exception):
    """Something happened and we're sure Qemu isn't running."""
    pass


class InvalidCommand(RuntimeError):
    pass


class VMConfigNotFound(RuntimeError):
    pass


class VMStateInconsistent(RuntimeError):
    pass
