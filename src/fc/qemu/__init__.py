

# Patch
import consulate.api


def register(self, name, address=None, service_id=None, port=None,
             tags=None, check=None, interval=None, ttl=None):
    """Add a new service to the local agent.
    :param str name: The name of the service
    :param str service_id: The id for the service (optional)
    :param int port: The service port
    :param list tags: A list of tags for the service
    :param str check: The path to the check to run
    :param str interval: The script execution interval
    :param str ttl: The TTL for external script check pings
    :rtype: bool
    :raises: ValueError
    """
    # Validate the parameters
    if port and not isinstance(port, int):
        raise ValueError('port must be an integer')
    elif tags and not isinstance(tags, list):
        raise ValueError('tags must be a list of strings')
    elif check and ttl:
        raise ValueError('Can not specify both a check and ttl')

    # Build the payload to send to consul
    payload = {'id': service_id,
               'name': name,
               'port': port,
               'tags': tags,
               'address': address,
               'check': {'script': check,
                         'interval': interval,
                         'ttl': ttl}}

    for key in list(payload.keys()):
        if payload[key] is None:
            del payload[key]

    # Register the service
    result = self._adapter.put(self._build_uri(['register']), payload)
    return result.status_code == 200


consulate.api.Agent.Service.register = register
