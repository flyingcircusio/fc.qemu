import json
import os.path
import urllib.parse
import xmlrpc.client
from typing import Literal

from fc.qemu.typing import EncDict


def load_default_enc_json() -> EncDict:
    if os.path.exists("/etc/nixos/enc.json"):
        with open("/etc/nixos/enc.json") as f:
            return json.load(f)

    with open("/etc/puppet/enc.json") as f:
        data = json.load(f)
    with open("/etc/directory.secret") as f:
        data["parameters"]["directory_password"] = f.read().strip()
    return data


def connect(
    enc: EncDict | None = None,
    ring: Literal["max", 0, 1] = 1,
) -> xmlrpc.client.ServerProxy:
    """Returns XML-RPC directory connection.

    The directory secret is read from `/etc/nixos/enc.json`.
    Alternatively, the parsed JSON content can be passed directly as
    dict.

    Selects ring0/ring1 API according to the `ring` parameter. Giving `max`
    results in selecting the highest ring available according to the ENC.
    """
    if not enc:
        enc = load_default_enc_json()
    if ring == "max":
        assert enc
        ring = enc["parameters"]["directory_ring"]
    base_url: str = enc["parameters"].get(
        "directory_url", "https://directory.fcio.net/v2/api"
    )
    url_parts = urllib.parse.urlsplit(base_url)

    url: str = (
        url_parts.scheme + "://"
        + enc["name"] + ":" + enc["parameters"]["directory_password"] + "@"
        + url_parts.netloc + url_parts.path
    )  # fmt: skip
    if ring == 1:
        url += "/rg-" + enc["parameters"]["resource_group"]

    return xmlrpc.client.ServerProxy(url, allow_none=True, use_datetime=True)
