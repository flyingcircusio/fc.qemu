#!/bin/bash
set -ex

kill_vms() {
    pkill -f qemu || true
    ssh host2 "/usr/bin/pkill -f qemu" || true
    bin/fc-qemu force-unlock test00
}

kill_vms

bin/fc-qemu start test00
ssh host2  "cd /vagrant; bin/fc-qemu inmigrate test00"&
bin/fc-qemu outmigrate test00 host2:9000
wait
bin/fc-qemu status test00 || true
ssh host2  "cd /vagrant; bin/fc-qemu status test00" || true

kill_vms