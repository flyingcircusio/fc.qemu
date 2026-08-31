{ lib, ...}:
{
  qemu.binary-generation = lib.mkForce 2;
  qemu.timeout-graceful = lib.mkForce 5;
  # Keep guest-agent interaction timeouts short. The test guests never run
  # a responsive guest agent, so the production defaults (30s/300s) would
  # make `fc-qemu` subprocesses (e.g. the supervised restart spawned after
  # a crash) hold the VM lock for a long time, causing flaky, timing-out
  # tests. Only the named_vm-based tests actually exercise this path.
  qemu.guest-agent-sync-timeout = 1;
  qemu.guest-agent-freeze-timeout = 1;

  network.underlay_loopback = lib.mkForce "172.21.64.23";
}
