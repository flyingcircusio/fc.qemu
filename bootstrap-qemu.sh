#!/usr/bin/env bash
set -ex

rm -rf /usr/src/vc-*.zip
rm -rf /usr/src/qemu-vc*
rm -f /usr/local/bin/qemu-system-x86_64

cd /usr/src
wget -c https://github.com/plieven/qemu/archive/vc-2.6.0.zip
unzip vc-2.6.0.zip
cd qemu-vc-2.6.0
mkdir build
cd build
../configure --target-list=x86_64-softmmu --enable-debug --enable-rbd
make -j2
cd /usr/local/bin
ln -s /usr/src/qemu-vc-2.6.0/build/x86_64-softmmu/qemu-system-x86_64 .