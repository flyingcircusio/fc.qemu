#!/usr/bin/env bash

apt-get update
apt-get autoremove -y
apt-get install -y qemu python-virtualenv ceph-deploy librbd1 python-dev

rm ceph*

ceph-deploy purge $HOSTNAME
rm -rf /var/lib/ceph

ceph-deploy new $HOSTNAME
cat >> ceph.conf <<EOF
osd pool default size = 1
EOF

ceph-deploy install $HOSTNAME
ceph-deploy mon create-initial $HOSTNAME
rm -rf /var/local/osd0
mkdir /var/local/osd0
ceph-deploy osd prepare $HOSTNAME:/var/local/osd0
ceph-deploy osd activate $HOSTNAME:/var/local/osd0
rm -rf /var/local/osd1
mkdir /var/local/osd1
ceph-deploy osd prepare $HOSTNAME:/var/local/osd1
ceph-deploy osd activate $HOSTNAME:/var/local/osd1
ceph-deploy admin $HOSTNAME
chmod +r /etc/ceph/ceph.client.admin.keyring

# Create demo VM
ceph osd pool create foobar 128
rbd create --size 100 foobar/foobar00.root
rbd create --size 100 foobar/foobar00.swap
rbd create --size 100 foobar/foobar00.tmp

rm -rf /etc/qemu/vm
mkdir -p /etc/qemu/vm
ln -s /vagrant/foobar00.cfg /etc/qemu/vm/

cat >> /etc/qemu/ifup <<EOF
#!/bin/bash
EOF
chmod +x /etc/qemu/ifup
cp /etc/qemu/ifup /etc/qemu/ifdown

cd /vagrant
virtualenv --system-site-packages .
bin/pip install -e .


