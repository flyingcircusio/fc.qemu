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
        if os.path.exists(filename):
            os.chmod(tf.name, os.stat(filename).st_mode & 0o7777)
        yield tf
        if not os.path.exists(tf.name):
            # Allow our clients to remove the file in case it doesn't want it
            # to be put in place actually but also doesn't want to error out.
            return
        tf.flush()
        os.fdatasync(tf)
        filename_tmp = tf.name
    os.rename(filename_tmp, filename)
