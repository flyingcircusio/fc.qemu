#!/usr/bin/env bash
set -ex

VERSION=4.1.0

rm -rf /usr/src/*.zip
rm -rf /usr/src/qemu-*
rm -f /usr/local/bin/qemu-system-x86_64

cd /usr/src
wget -c "https://download.qemu.org/qemu-${VERSION}.tar.xz"
tar -xf "qemu-${VERSION}.tar.xz"
cd "qemu-${VERSION}"
mkdir build
cd build
../configure --target-list=x86_64-softmmu --enable-debug --enable-rbd
make -j2
cd /usr/local/bin
ln -s "/usr/src/qemu-${VERSION}/build/x86_64-softmmu/qemu-system-x86_64" .
