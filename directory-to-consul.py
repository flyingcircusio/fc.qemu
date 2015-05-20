#!/usr/bin/python
from __future__ import print_function
import consulate
import gocept.net.directory
import os
import string
import sys


def enc_load():
    directory = gocept.net.directory.Directory()
    location = os.environ['PUPPET_LOCATION']
    res = {}
    for vm in directory.list_virtual_machines(location):
        rg = vm['parameters']['resource_group']
        key = 'node/{}/{}'.format(rg, vm['name'])
        res[key] = vm
    return res


def main():
    vms = enc_load()
    c = consulate.Consul()
    kv_present = c.kv.find('node/')
    for deleted in set(kv_present.keys()) - set(vms.keys()):
        print('deleting VM %s' % deleted)
        del c.kv[deleted]
    for updated, vm in vms.items():
        print('updating VM %s' % updated)
        c.kv[updated] = vm


if __name__ == '__main__':
    main()
