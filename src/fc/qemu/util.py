import contextlib
import os
import tempfile


@contextlib.contextmanager
def rewrite(filename):
    """Rewrite an existing file atomically to avoid programs running in
    parallel to have race conditions while reading."""
    with tempfile.NamedTemporaryFile(
            dir=os.path.dirname(filename), prefix=os.path.basename(filename),
            delete=False) as tf:
        os.chmod(tf.name, os.stat(filename).st_mode & 0o7777)
        yield tf
        tf.flush()
        os.fdatasync(tf)
        filename_tmp = tf.name
    if not os.path.exists(filename_tmp):
        # Allow our clients to remove the file in case it doesn't want it to be
        # put in place actually but also doesn't want to error out.
        return
    os.rename(filename_tmp, filename)
