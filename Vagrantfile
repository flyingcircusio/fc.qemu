# -*- mode: ruby -*-
# vi: set ft=ruby :

# Vagrantfile API/syntax version. Don't touch unless you know what you're doing!
VAGRANTFILE_API_VERSION = "2"

Vagrant.configure(VAGRANTFILE_API_VERSION) do |config|
  config.vm.box = "ubuntu/trusty64"
  config.vm.provision "puppet"

  config.vm.provider "virtualbox" do |vb|
    vb.customize ["modifyvm", :id, "--memory", "1024"]
  end

  config.vm.define "host1", primary: true do |host1|
    host1.vm.network "private_network", ip: "192.168.50.4"
    host1.vm.hostname = "host1"
    host1.vm.provision "shell", path: "bootstrap-ceph.sh"
  end

  config.vm.define "host2" do |host2|
    host2.vm.network "private_network", ip: "192.168.50.5"
    host2.vm.hostname = "host2"
  end

end
