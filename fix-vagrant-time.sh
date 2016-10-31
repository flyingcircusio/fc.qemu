#!/bin/bash
set -x
vagrant ssh -c "sudo bash -x -c '/etc/init.d/ntp stop; ntpdate 144.76.220.215; /etc/init.d/ntp start; restart ceph-all'" host1
vagrant ssh -c "sudo bash -x -c '/etc/init.d/ntp stop; ntpdate 144.76.220.215; /etc/init.d/ntp start; restart ceph-all'" host2
