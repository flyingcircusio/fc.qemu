"""
Consul client object

"""

from consulate.api.agent import Agent
from consulate.api.catalog import Catalog
from consulate.api.health import Health

DEFAULT_HOST = ...
DEFAULT_PORT = ...
DEFAULT_ADDR = ...
DEFAULT_SCHEME = ...
DEFAULT_TOKEN = ...
API_VERSION = ...

class Consul:
    """Access the Consul HTTP API via Python.

    The default values connect to Consul via ``localhost:8500`` via http. If
    you want to connect to Consul via a local UNIX socket, you'll need to
    override both the ``scheme``, ``port`` and the ``adapter`` like so:

    .. code:: python

        consul = consulate.Consul('/path/to/socket', None, scheme='http+unix',
                                  adapter=consulate.adapters.UnixSocketRequest)
        services = consul.agent.services()

    :param str addr: The CONSUL_HTTP_ADDR if available (Default: None)
    :param str host: The host name to connect to (Default: localhost)
    :param int port: The port to connect on (Default: 8500)
    :param str datacenter: Specify a specific data center
    :param str token: Specify a ACL token to use
    :param str scheme: Specify the scheme (Default: http)
    :param class adapter: Specify to override the request adapter
        (Default: :py:class:`consulate.adapters.Request`)
    :param bool/str verify: Specify how to verify TLS certificates
    :param tuple cert: Specify client TLS certificate and key files
    :param float timeout: Timeout in seconds for API requests (Default: None)

    """
    def __init__(
        self,
        addr=...,
        host=...,
        port=...,
        datacenter=...,
        token=...,
        scheme=...,
        adapter=...,
        verify=...,
        cert=...,
        timeout=...,
    ) -> None:
        """Create a new instance of the Consul class"""
        ...

    @property
    def acl(self) -> ACL:
        """Access the Consul
        `ACL <https://www.consul.io/docs/agent/http/acl.html>`_ API

        :rtype: :py:class:`consulate.api.acl.ACL`

        """
        ...

    @property
    def agent(self) -> Agent:
        """Access the Consul
        `Agent <https://www.consul.io/docs/agent/http/agent.html>`_ API

        :rtype: :py:class:`consulate.api.agent.Agent`

        """
        ...

    @property
    def catalog(self) -> Catalog:
        """Access the Consul
        `Catalog <https://www.consul.io/docs/agent/http/catalog.html>`_ API

        :rtype: :py:class:`consulate.api.catalog.Catalog`

        """
        ...

    @property
    def event(self) -> Event:
        """Access the Consul
        `Events <https://www.consul.io/docs/agent/http/event.html>`_ API

        :rtype: :py:class:`consulate.api.event.Event`

        """
        ...

    @property
    def health(self) -> Health:
        """Access the Consul
        `Health <https://www.consul.io/docs/agent/http/health.html>`_ API

        :rtype: :py:class:`consulate.api.health.Health`

        """
        ...

    @property
    def coordinate(self) -> Coordinate:
        """Access the Consul
        `Coordinate <https://www.consul.io/api/coordinate.html#read-lan-coordinates-for-a-node>`_ API

        :rtype: :py:class:`consulate.api.coordinate.Coordinate`

        """
        ...

    @property
    def kv(self) -> KV:
        """Access the Consul
        `KV <https://www.consul.io/docs/agent/http/kv.html>`_ API

        :rtype: :py:class:`consulate.api.kv.KV`

        """
        ...

    @property
    def lock(self) -> Lock:
        """Wrapper for easy :class:`~consulate.api.kv.KV` locks.
        `Semaphore <https://www.consul.io/docs/guides/semaphore.html>` _Guide
        Example:

        .. code:: python

            import consulate

            consul = consulate.Consul()
            with consul.lock.acquire('my-key'):
                print('Locked: {}'.format(consul.lock.key))
                # Do stuff

        :rtype: :class:`~consulate.api.lock.Lock`

        """
        ...

    @property
    def session(self) -> Session:
        """Access the Consul
        `Session <https://www.consul.io/docs/agent/http/session.html>`_ API

        :rtype: :py:class:`consulate.api.session.Session`

        """
        ...

    @property
    def status(self) -> Status:
        """Access the Consul
        `Status <https://www.consul.io/docs/agent/http/status.html>`_ API

        :rtype: :py:class:`consulate.api.status.Status`

        """
        ...
