#!/usr/bin/env bash

set -e

apt-get install -y ceph-deploy librbd1 ceph python-dev

ceph-deploy install host1 host2
ceph-deploy new host1 host2
ceph-deploy --overwrite-conf mon create-initial host1 host2
ceph-deploy --overwrite-conf osd create host1:/var/local/osd0 host2:/var/local/osd1
ceph-deploy --overwrite-conf osd activate host1:/var/local/osd0 host2:/var/local/osd1
ceph-deploy --overwrite-conf admin host1 host2

# Create demo VM pool
ceph osd pool create rbd.hdd 8
ceph osd pool set rbd.hdd size 2
ceph osd pool set rbd.hdd min_size 1

ceph osd pool create rbd.ssd 8
ceph osd pool set rbd.ssd size 2
ceph osd pool set rbd.ssd min_size 1
