import contextlib
import filecmp
import os
import tempfile


@contextlib.contextmanager
def rewrite(filename):
    """Rewrite an existing file atomically.

    Clients are allowed to delete the tmpfile to signal that they don't
    want to have it updated.
    """

    with tempfile.NamedTemporaryFile(
            dir=os.path.dirname(filename), prefix=os.path.basename(filename),
            delete=False) as tf:
        if os.path.exists(filename):
            os.chmod(tf.name, os.stat(filename).st_mode & 0o7777)
        yield tf
        if not os.path.exists(tf.name):
            return
        filename_tmp = tf.name
    if (os.path.exists(filename) and
            filecmp.cmp(filename, filename_tmp, shallow=False)):
        os.unlink(filename_tmp)
    else:
        os.rename(filename_tmp, filename)


def parse_address(addr):
    if addr.startswith('['):
        host, port = addr[1:].split(']:')
    else:
        host, port = addr.split(':')
    return host, int(port)


def locate_live_service(consul, service_id):
    """Locate Consul service with at least one passing health check.

    It is an error if multiple live services with the same service name
    are found.
    """
    passing = lambda checks: (
        any(check['Status'] == 'passing' for check in checks) and
        not any(check['Status'] == 'critical' for check in checks))
    live = [svc for svc in consul.health.service(service_id)
            if passing(svc['Checks'])]
    if len(live) > 1:
        raise RuntimeError('multiple services with passing checks found',
                           service_id)
    return live[0]['Service'] if len(live) else None
