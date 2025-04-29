from fc.qemu.aramaki import AramakiBeaconSender


def test_message_construction(now):
    status = {"vm": "simplevm", "status": "online"}
    beacon = AramakiBeaconSender(status)
    beacon.construct_message()
    del beacon.message["@id"]
    assert beacon.message == {
        "@context": "https://flyingcircus.io/ns/aramaki",
        "@version": 1,
        "@signature": {"alg": "HS256"},
        "@principal": "host1",
        "@type": "vm.status",
        "@issued": "2025-04-20T10:01:02.000003",
        "@expiry": "2025-04-20T11:01:02.000003",
        "vm": "simplevm",
        "status": "online",
    }
    beacon.sign_message()
    assert beacon.message == {
        "@context": "https://flyingcircus.io/ns/aramaki",
        "@version": 1,
        "@signature": {"alg": "HS256", "signature": "42d30b6d13c7e533517aed20397b46419c068864478c83e52e3cb26a83402077"},
        "@principal": "host1",
        "@type": "vm.status",
        "@issued": "2025-04-20T10:01:02.000003",
        "@expiry": "2025-04-20T11:01:02.000003",
        "vm": "simplevm",
        "status": "online",
    }
