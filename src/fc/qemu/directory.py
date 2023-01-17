import json
import os.path
import urllib.parse
import xmlrpc.client


def load_default_enc_json():
    if os.path.exists("/etc/nixos/enc.json"):
        with open("/etc/nixos/enc.json") as f:
            return json.load(f)
    else:
        with open("/etc/puppet/enc.json") as f:
            data = json.load(f)
        with open("/etc/directory.secret") as f:
            data["parameters"]["directory_password"] = f.read().strip()
        return data
    raise RuntimeError("No ENC file found.")


def connect(enc=None, ring=1):
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
        ring = enc["parameters"]["directory_ring"]
    base_url = enc["parameters"].get(
        "directory_url", "https://directory.fcio.net/v2/api"
    )
    url_parts = urllib.parse.urlsplit(base_url)

    url = (
        # fmt: off
        url_parts.scheme + "://"
        + enc["name"] + ":" + enc["parameters"]["directory_password"] + "@"
        + url_parts.netloc + url_parts.path
        # fmt: on
    )
    if ring == 1:
        url += "/rg-" + enc["parameters"]["resource_group"]

    return xmlrpc.client.ServerProxy(url, allow_none=True, use_datetime=True)
