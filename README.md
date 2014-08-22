# Flying Circus QEMU virtual machine management

This package provides a utility to manage virtual machines and their life cycle in the Flying Circus.
We try to keep specific of our environment out of there, but we make a few assumptions:

* VM disks (root, swap, tmp) are stored in Ceph
* There is a script 'create-vm' that will create a new disk image.

The utility allows you to

* start, stop and migrate VMs between hosts
* run a daemon that enforces the policy about running VMs
  given by a set of config files

