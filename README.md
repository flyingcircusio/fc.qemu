# Flying Circus QEMU virtual machine management

This package provides a utility to manage virtual machines and their life cycle in the Flying Circus.
We try to keep specific of our environment out of there, but we make a few assumptions:

* VM disks (root, swap, tmp) are stored in Ceph
* There is a script 'create-vm' that will create a new disk image.

The utility allows you to

* start, stop and migrate VMs between hosts
* run a daemon that enforces the policy about running VMs
  given by a set of config files
* resize disks


## Config format

Expects 1 config file for each VM in /etc/qemu/*.cfg.

The config file format is YAML.

Format:


    id: 12345
    uuid: 2134-fdasfd-e21d-fdsa-fdsa-fds
    name: test00
    resource_group: test

    online: true
    kvm_host: bob

    disk: 5
    memory: 512
    cores: 1

    nics:
    - srv: 00-01-02-03-05-06
    - fe: 00-01-02-03-05-06



