# Flying Circus QEMU virtual machine management

This package provides a utility to manage virtual machines and their life cycle
in the Flying Circus. We try to keep specifics of our environment out of there,
but we make a few assumptions:

* VM disks (root, swap, tmp) are stored in Ceph
* There is a script 'create-vm' that will prepare a fresh root disk image.

The utility allows you to

* start, stop and migrate VMs between hosts
* run a daemon that enforces the policy about running VMs
  given by a set of config files
* resize disks


## Config format

Expects 1 config file for each VM in /etc/qemu/vm/*.cfg.

The config file format is YAML.

Format:

    name: test00
        parameters:
        id: 12345
        resource_group: test

        online: true
        kvm_host: bob

        disk: 5
        memory: 512
        cores: 1

        nics:
        - srv: 00-01-02-03-05-06
        - fe: 00-01-02-03-05-06



