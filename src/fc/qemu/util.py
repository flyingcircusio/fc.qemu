import contextlib
import os
import tempfile


@contextlib.contextmanager
def rewrite(filename):
    """Rewrite an existing file atomically to avoid programs running in
    parallel to have race conditions while reading."""
    fd, filename_tmp = tempfile.mkstemp(dir=os.path.dirname(filename))
    os.close(fd)
    with open(filename_tmp, 'w') as f:
        yield f
    if not os.path.exists(filename_tmp):
        # Allow our clients to remove the file in case it doesn't want it to be
        # put in place actually but also doesn't want to error out.
        return
    if os.path.exists(filename):
        os.chmod(filename_tmp, os.stat(filename).st_mode)
    os.rename(filename_tmp, filename)
