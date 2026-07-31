{ lib, ...}:
{
  qemu.binary-generation = lib.mkForce 2;
  qemu.timeout-graceful = lib.mkForce 5;

  network.underlay_loopback = lib.mkForce "172.21.64.23";
}
