class LockError(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


class DestructionError(RuntimeError):
    """Failed to destroy a running VM."""
    pass
