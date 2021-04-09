#!/bin/bash

set -ex

add-apt-repository -y ppa:fkrull/deadsnakes-python2.7
apt update 
apt-get -y --force-yes upgrade
apt-get -y --force-yes install python-dev libyaml-dev

