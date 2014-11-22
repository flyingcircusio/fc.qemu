#!/usr/bin/env bash

apt-get update
apt-get install -y qemu python-virtualenv ceph-deploy librbd1 python-dev ceph

if ! grep -q LC_ALL /etc/environment; then
    echo 'LC_ALL="en_US.utf8"' >> /etc/environment
fi

rm ceph*
ceph-deploy purge $HOSTNAME
rm -rf /var/lib/ceph

ceph-deploy new $HOSTNAME
cat >> ceph.conf <<EOF
osd pool default size = 1
EOF
rm -rf /var/local/osd0 /var/local/osd1
mkdir -p /var/local/osd0 /var/local/osd1

ceph-deploy install $HOSTNAME
ceph-deploy mon create-initial $HOSTNAME
ceph-deploy osd prepare $HOSTNAME:/var/local/osd0
ceph-deploy osd activate $HOSTNAME:/var/local/osd0
ceph-deploy osd prepare $HOSTNAME:/var/local/osd1
ceph-deploy osd activate $HOSTNAME:/var/local/osd1
ceph-deploy admin $HOSTNAME
chmod +r /etc/ceph/ceph.client.admin.keyring

# Create demo VM
ceph osd pool create test 128

# Dummy versions of required helper scripts
cat > /usr/local/sbin/create-vm <<_EOF_
#!/bin/sh
test "\$1" = -I || exit 1
rbd create --size 5120 "test/\${2}.root"
_EOF_
chmod +x /usr/local/sbin/create-vm
rm -f /usr/sbin/create-vm  # old script location

cat > /usr/local/sbin/shrink-vm <<_EOF_
#!/bin/sh
echo "fake shrink-vm pool=\$1 image=\$2 disk=\$3"
_EOF_
chmod +x /usr/local/sbin/shrink-vm

rm -rf /etc/qemu/vm
mkdir -p /etc/qemu/vm
ln -s /vagrant/foobar00.cfg /etc/qemu/vm/

mkdir -p /etc/kvm
cat > /etc/kvm/kvm-ifup <<_EOF_
#!/bin/sh
_EOF_
chmod +x /etc/kvm/kvm-ifup
cp /etc/kvm/kvm-ifup /etc/kvm/kvm-ifdown

cd /vagrant
sudo -u vagrant virtualenv -p python2.7 --system-site-packages .
sudo -u vagrant /vagrant/bin/pip install -r requirements.txt

touch /dev/kvm
