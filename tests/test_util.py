from fc.qemu.util import parse_export_format


def test_export_format():
    data = """DEVNAME="test"
    UUID='a5370804-c9b1-4610-8f99-02a7841b8393'
    BLOCK_SIZE=512
    TYPE=xfs
    """
    assert parse_export_format(data) == {
        "BLOCK_SIZE": "512",
        "TYPE": "xfs",
        "UUID": "a5370804-c9b1-4610-8f99-02a7841b8393",
        "DEVNAME": "test",
    }
