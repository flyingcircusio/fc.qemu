{
  config,
  lib,
  pkgs,
  ...
}:
let
  fclib = config.fclib;
in
{

  flyingcircus.encServices = [
    {
      address = "host1";
      ips = [ "{{component.host1_addr.listen.host}}" ];
      location = "test";
      service = "consul_server-server";
    }
    {
      address = "host1";
      ips = [ "{{component.host1_addr.listen.host}}" ];
      location = "test";
      service = "ceph_mon-mon";
    }
  ];

  environment.systemPackages = [ pkgs.uv ];
  systemd.timers.logrotate.enable = lib.mkForce false;
  flyingcircus.agent.enable = lib.mkForce false;

  networking.extraHosts = ''
    {{component.host1_addr.listen.host}} host1.srv.test.gocept.net host1
    {{component.host2_addr.listen.host}} host2.srv.test.gocept.net host2
  '';

  flyingcircus.static.ceph.fsids.test.test = "d118a9a4-8be5-4703-84c1-87eada2e6b60";

  flyingcircus.services.consul.advertiseAddr = lib.head fclib.network.srv.v4.addresses;
  flyingcircus.services.consul.bindAddr = lib.head fclib.network.srv.v4.addresses;
  flyingcircus.services.consul.dc = "test";

  system.activationScripts.updateTransientHostname = ''
    ${pkgs.systemd}/bin/hostnamectl set-hostname --transient $(${pkgs.systemd}/bin/hostnamectl status --static)
    '';


  # Lets' Encrypt Staging certificates

  security.pki.certificates = [
    ''
    Subject: O = (STAGING) Internet Security Research Group, CN = (STAGING) Pretend Pear X1
    -----BEGIN CERTIFICATE-----
    MIIFmDCCA4CgAwIBAgIQU9C87nMpOIFKYpfvOHFHFDANBgkqhkiG9w0BAQsFADBm
    MQswCQYDVQQGEwJVUzEzMDEGA1UEChMqKFNUQUdJTkcpIEludGVybmV0IFNlY3Vy
    aXR5IFJlc2VhcmNoIEdyb3VwMSIwIAYDVQQDExkoU1RBR0lORykgUHJldGVuZCBQ
    ZWFyIFgxMB4XDTE1MDYwNDExMDQzOFoXDTM1MDYwNDExMDQzOFowZjELMAkGA1UE
    BhMCVVMxMzAxBgNVBAoTKihTVEFHSU5HKSBJbnRlcm5ldCBTZWN1cml0eSBSZXNl
    YXJjaCBHcm91cDEiMCAGA1UEAxMZKFNUQUdJTkcpIFByZXRlbmQgUGVhciBYMTCC
    AiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBALbagEdDTa1QgGBWSYkyMhsc
    ZXENOBaVRTMX1hceJENgsL0Ma49D3MilI4KS38mtkmdF6cPWnL++fgehT0FbRHZg
    jOEr8UAN4jH6omjrbTD++VZneTsMVaGamQmDdFl5g1gYaigkkmx8OiCO68a4QXg4
    wSyn6iDipKP8utsE+x1E28SA75HOYqpdrk4HGxuULvlr03wZGTIf/oRt2/c+dYmD
    oaJhge+GOrLAEQByO7+8+vzOwpNAPEx6LW+crEEZ7eBXih6VP19sTGy3yfqK5tPt
    TdXXCOQMKAp+gCj/VByhmIr+0iNDC540gtvV303WpcbwnkkLYC0Ft2cYUyHtkstO
    fRcRO+K2cZozoSwVPyB8/J9RpcRK3jgnX9lujfwA/pAbP0J2UPQFxmWFRQnFjaq6
    rkqbNEBgLy+kFL1NEsRbvFbKrRi5bYy2lNms2NJPZvdNQbT/2dBZKmJqxHkxCuOQ
    FjhJQNeO+Njm1Z1iATS/3rts2yZlqXKsxQUzN6vNbD8KnXRMEeOXUYvbV4lqfCf8
    mS14WEbSiMy87GB5S9ucSV1XUrlTG5UGcMSZOBcEUpisRPEmQWUOTWIoDQ5FOia/
    GI+Ki523r2ruEmbmG37EBSBXdxIdndqrjy+QVAmCebyDx9eVEGOIpn26bW5LKeru
    mJxa/CFBaKi4bRvmdJRLAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNVHRMB
    Af8EBTADAQH/MB0GA1UdDgQWBBS182Xy/rAKkh/7PH3zRKCsYyXDFDANBgkqhkiG
    9w0BAQsFAAOCAgEAncDZNytDbrrVe68UT6py1lfF2h6Tm2p8ro42i87WWyP2LK8Y
    nLHC0hvNfWeWmjZQYBQfGC5c7aQRezak+tHLdmrNKHkn5kn+9E9LCjCaEsyIIn2j
    qdHlAkepu/C3KnNtVx5tW07e5bvIjJScwkCDbP3akWQixPpRFAsnP+ULx7k0aO1x
    qAeaAhQ2rgo1F58hcflgqKTXnpPM02intVfiVVkX5GXpJjK5EoQtLceyGOrkxlM/
    sTPq4UrnypmsqSagWV3HcUlYtDinc+nukFk6eR4XkzXBbwKajl0YjztfrCIHOn5Q
    CJL6TERVDbM/aAPly8kJ1sWGLuvvWYzMYgLzDul//rUF10gEMWaXVZV51KpS9DY/
    5CunuvCXmEQJHo7kGcViT7sETn6Jz9KOhvYcXkJ7po6d93A/jy4GKPIPnsKKNEmR
    xUuXY4xRdh45tMJnLTUDdC9FIU0flTeO9/vNpVA8OPU1i14vCz+MU8KX1bV3GXm/
    fxlB7VBBjX9v5oUep0o/j68R/iDlCOM4VVfRa8gX6T2FU7fNdatvGro7uQzIvWof
    gN9WUwCbEMBy/YhBSrXycKA8crgGg3x1mIsopn88JKwmMBa68oS7EHM9w7C4y71M
    7DiA+/9Qdp9RBWJpTS9i/mDnJg1xvo8Xz49mrrgfmcAXTCJqXi24NatI3Oc=
    -----END CERTIFICATE-----
    ''

    ''
    Subject: O = (STAGING) Internet Security Research Group, CN = (STAGING) Bogus Broccoli X2
    -----BEGIN CERTIFICATE-----
    MIIEmTCCAoGgAwIBAgIRAJJVIr2Em/sOzhBD2bEnEJwwDQYJKoZIhvcNAQELBQAw
    ZjELMAkGA1UEBhMCVVMxMzAxBgNVBAoTKihTVEFHSU5HKSBJbnRlcm5ldCBTZWN1
    cml0eSBSZXNlYXJjaCBHcm91cDEiMCAGA1UEAxMZKFNUQUdJTkcpIFByZXRlbmQg
    UGVhciBYMTAeFw0yMDA5MDQwMDAwMDBaFw0yNTA5MTUxNjAwMDBaMGgxCzAJBgNV
    BAYTAlVTMTMwMQYDVQQKEyooU1RBR0lORykgSW50ZXJuZXQgU2VjdXJpdHkgUmVz
    ZWFyY2ggR3JvdXAxJDAiBgNVBAMTGyhTVEFHSU5HKSBCb2d1cyBCcm9jY29saSBY
    MjB2MBAGByqGSM49AgEGBSuBBAAiA2IABDr0vsNZAswMWDiWwNOgMNBxT9rSwSyj
    6BUKkfQDLJJdZwtve+XkKsnEfgAr2HpQPK38BVzmzB2Fydt1ywfnQIzyVTidjnLI
    01ajuHXA1rvq0NlSC3ZyUWMqZ1dTDE4VcaOB7TCB6jAOBgNVHQ8BAf8EBAMCAQYw
    DwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4EFgQU3tGjWWQOwZo2o0busBB2766XlWYw
    HwYDVR0jBBgwFoAUtfNl8v6wCpIf+zx980SgrGMlwxQwNgYIKwYBBQUHAQEEKjAo
    MCYGCCsGAQUFBzAChhpodHRwOi8vc3RnLXgxLmkubGVuY3Iub3JnLzArBgNVHR8E
    JDAiMCCgHqAchhpodHRwOi8vc3RnLXgxLmMubGVuY3Iub3JnLzAiBgNVHSAEGzAZ
    MAgGBmeBDAECATANBgsrBgEEAYLfEwEBATANBgkqhkiG9w0BAQsFAAOCAgEAMkp5
    etLOxM4+a6EqX2hmAd+yNUSNCA7+MYn/VrwJnpkWe8zuC+fILYMYRuByWs/zeFmo
    56Jc7td5N9I+QN0rYSeEbgdTAMeaBjZ3P6eJxM1Aa76Abrj5ULfq8XhOE37SYgFb
    ZS9YPOQ4wuisCXHrrmu4ZdZJmzXIQX562xBeJxf0o4LBqS2C3SmpkPY+f8lTtmFO
    /I6qSSl8T5XyNE385zNXaRd8rMJqNC9fIHDjPeJMIaou0TZYT0uNb9OZ7ZhT7smQ
    SaHcGxtK0SVmJvGNagc6RldrHFbemLbwVpeI4NopRHynQqzkVtsfAlK8VD92SYbp
    olFsJZWuHVkHgccuI1Hx0+RUp1VGj1PPV+0JmGZeG2ybLloU2rjjMbRmkNjTxub2
    U1vzCGpBSaBfYQLjLHDwQk1AqRENlZxDqCkXFro8eqT6TFHdtw27KIT+ov1Qyofi
    q3Uj1w7tPpcFMSDfiWNRE0XGYCjELDo19oPqQthIMQ5X+/3YpCqZceR4vMR6n9ol
    Lp/0KmjAzqU+LqD2fmFLttKvZUxW8aECTGIcDHGCPJDklwDW3l7DUQ08Wj5Fh/KE
    f5c9fF3u87WUAJu4Vh9C+ewXZtzL0LD46lYgpn7fv5w9sLS4zQ3CIC3udjJ5Gc/v
    8VhPQaU1Enn7NW+4IHnfSeP6G5rzLEtl0PreC4k=
    -----END CERTIFICATE-----
    ''
  ];
}
