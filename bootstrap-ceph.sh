#!/usr/bin/env bash

set -e

apt-get install -y ceph-deploy librbd1 ceph python-dev

ceph-deploy install host1 host2
ceph-deploy new host1 host2
ceph-deploy mon create-initial host1 host2
ceph-deploy osd create host1:/var/local/osd0 host2:/var/local/osd1
ceph-deploy osd activate host1:/var/local/osd0 host2:/var/local/osd1
ceph-deploy admin host1 host2

# Create demo VM pool
ceph osd pool create test 128
