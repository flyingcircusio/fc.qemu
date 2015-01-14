=============================================
Flying Circus QEMU virtual machine management
=============================================

This package provides a utility to manage virtual machines and their life cycle
in the Flying Circus. We try to keep specifics of our environment out of there,
but we make a few assumptions:

* VM disks (root, swap, tmp) are stored in Ceph
* There is a script `create-vm` that will prepare a fresh root disk image.

The utility allows you to

* start, stop and migrate VMs between hosts
* run a daemon that enforces the policy about running VMs
  given by a set of config files
* resize disks.


Config format
=============

Generic template
----------------

The Qemu config file will be generated from a template. If no template is found
in `/etc/qemu/qemu.vm.cfg.in`, a built-in default template will be used. Refer
to qemu.vm.cfg.in in the source distribution.


Per-VM configuration
--------------------

Expects a config file for each VM in `/etc/qemu/vm/*.cfg`.

The config file format is YAML.

Format::

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


.. vim: set ft=rst:
