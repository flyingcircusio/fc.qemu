#!/usr/bin/env bash
set -ex

rm -rf /usr/src/*.zip
rm -rf /usr/src/qemu-*
rm -f /usr/local/bin/qemu-system-x86_64

cd /usr/src
wget -c https://github.com/qemu/qemu/archive/v2.7.0.zip
unzip v2.7.0.zip
cd qemu-2.7.0
mkdir build
cd build
../configure --target-list=x86_64-softmmu --enable-debug --enable-rbd
make -j2
cd /usr/local/bin
ln -s /usr/src/qemu-2.7.0/build/x86_64-softmmu/qemu-system-x86_64 .
