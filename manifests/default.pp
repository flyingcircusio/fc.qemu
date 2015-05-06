#########################
# General system stuff

exec { 'apt-get update': }

Exec {
    path => "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games
"
}

Package {
    require => Exec["apt-get update"]
}

file { "/etc/environment":
    content => "\
PATH=\"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games\"
LC_ALL=\"en_US.utf8\"
"
}

package {
    ["qemu",
     "python-virtualenv",
     "python-dev"]:
    ensure => installed;
}

file { "/run/local":
    ensure => directory,
    owner => "vagrant",
    group => "vagrant";
}

file { "/home/vagrant/.ssh/config":
    content => "\
UserKnownHostsFile /dev/null
StrictHostKeyChecking no
",
    owner => "vagrant",
    group => "vagrant";
}

file { "/root/.ssh/config":
    content => "\
UserKnownHostsFile /dev/null
StrictHostKeyChecking no
",
    owner => "root",
    group => "root";
}

exec { 'apt-get autoremove':
    command => "/usr/bin/apt-get -y autoremove"
}

host { "host1":
    ip => "192.168.50.4";
}

host { "host2":
    ip => "192.168.50.5";
}


file { "/root/.ssh/id_rsa":
    content => "\
-----BEGIN RSA PRIVATE KEY-----
MIIEogIBAAKCAQEAvHSfUCSbtkaPt3KdAU1/EO7c4ucbik35EVQrW8YwtNO9JERo
iSjV0iFkfFV139t/r91k8wRWY1RikIWqBI6AottFG4KEZqKr949cMZisRWMJhpwg
EKSG0U2o18l2hPUW8tFdg+lyL2WjFE9mASd/X9J3M3yUStFkDL4phbplBUAcQgMC
jg4vVHme7xuQUVOiNYKaQRXnFafVv4uby71XrvgygaNYYHUnSdd1OMwAcXyx0aP5
CZG80j3zg/3cbtr6O108W+1RDkMZccfpGSPwUOG63hmO+DWwH8qQC2W3eCHN80Bw
SxWTwfOQDLbyhuoRIEmAvOWGdkh++iqNT2yACwIDAQABAoIBAEvpck8XH/4ReEy+
B06CCAArJ6Di1S4l8IExdXG3aOE+NX9Jaw5s+4x0VQTca+nrggi2VrapdZ73W+i5
Xt4NBPYU+0Z0kZ7CQiErh0iXJjWhCjJF64iorYHcFXouteYiz8ap3VCIla1P9Jv2
y7EFVwKjRc7gjN+CbxnO8+zhQ1YUZfCTsNlKSLWHGGWX3qooQcN/7deCFMwhenmU
k2YnkaHS+wAmllOTH+aeXv5auPG8OC2t4DFUvPRnMkFXGMpxpAXgWqxTjb8e2NlN
ZZKit7WCSZaRGbqvAYDU7keNYi0PHj89O6zzKNdmYLuEpAqHGq07AgeH/cTecJhk
kRLbEvkCgYEA9kLdZWS0WFhLDN1LG+tSVgVc3Fs1IDvnqfP+dq5alSvtwTdFuAAV
AaBFMZquOjrjn8Ly429Wj1hzEnNhg8qGE21rqzeBg6I7FBmHc45Lvt4oPp8aKLZh
IgBLOzzsVnPkZ6QD9GPkJ3ROqvCP90tR2VmQnHQaNXP384UMFYZVv7cCgYEAw+iJ
TGSA3x97bDbWDCvU3XxSr4WzWfn93BKfZUPp4nN69mKadoLxNHkDnryKI50T6tAx
Hpv6M+TeeJmXhoFM+W2T2LiudhJQxUXh9bY1S0AkrcMZCUzyLcCiQo+odlE73U/Q
Pj7jPsJZdUWG9c9y0DIdFQSXtsiw5xbZgXCg2k0CgYBpL6xTd81Ugvojl45ScZRs
q8O7V7X8e7n7LP3/AYAtgWL/ibVc36QZWrTTeEd9FdROVD3dCZyGg/g65E+9tE7K
k41Ox9mpOS1U64agxCH0d/3mqZzJ0QTyOf/oYKBuWPgxkKwjwlscwyArAa/sqB5g
4VHUkf7z4AID4UuFEikkRwKBgDmIGnnysd3UcvxuhiGA5bw9fFLLYsYzohd06JZm
gVLdMukUP+Q5w/fy6ds95xtaT7UPer1QdQO0XJjyEguQATjmsxpb8e/+pPWp9heg
cLoulhbpSnruu9gvz/bYFVLZvEjb3X3KHhYaIQdNabraNw9pCB1aAevNuBXFIg7f
Mn2pAoGAfClGLds3P4Ef2VpM0xTKM4VwsiaU9NE390CYX7C3FfhYuNzUp4buDqN9
GDJMeOuJOZshvHCTpltrqVd+SKJXxl3FR1hcML28C+tpxWLQagmprkLwj/hPIFsz
aOuRkpJLmMX2aPhsxt1vE3Fv1gCPqjFqySkj/WMYjlnqmsSorJI=
-----END RSA PRIVATE KEY-----
",
    mode => 600;
}

file { "/root/.ssh/authorized_keys":
    content => "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC8dJ9QJJu2Ro+3cp0BTX8Q7tzi5xuKTfkRVCtbxjC0070kRGiJKNXSIWR8VXXf23+v3WTzBFZjVGKQhaoEjoCi20UbgoRmoqv3j1wxmKxFYwmGnCAQpIbRTajXyXaE9Rby0V2D6XIvZaMUT2YBJ39f0nczfJRK0WQMvimFumUFQBxCAwKODi9UeZ7vG5BRU6I1gppBFecVp9W/i5vLvVeu+DKBo1hgdSdJ13U4zABxfLHRo/kJkbzSPfOD/dxu2vo7XTxb7VEOQxlxx+kZI/BQ4breGY74NbAfypALZbd4Ic3zQHBLFZPB85AMtvKG6hEgSYC85YZ2SH76Ko1PbIAL root@host1
    ",
    owner => "root",
    mode => 600;
}

#########################
# qemu/kvm stuff


file { ["/etc/kvm", "/etc/qemu"]: ensure => directory }

file { ["/etc/kvm/kvm-ifup",
        "/etc/kvm/kvm-ifdown"]:
    content => "#!/bin/bash\n",
    mode => 755,
}

file { "/dev/kvm": ensure => present }


#########################
# fc.qemu stuff

file { "/usr/local/sbin/create-vm":
    content => '#!/bin/bash
set -e
test "$1" = -I
rbd create --size 5120 --image-format 2 "test/${2}.root"
',
    mode => 755,
}


file { "/usr/local/sbin/shrink-vm":
    content => '#!/bin/sh
echo "fake shrink-vm pool=$1 image=$2 disk=$3"
',
    mode => 755,
}

file { "/etc/qemu/vm": ensure => directory }

file { "/etc/qemu/vm/test00.cfg":
    ensure => symlink,
    target => "/vagrant/test00.cfg",
}

exec { "bootstrap-agent-project":
    creates => "/vagrant/bin",
    command => "\
sudo -u vagrant rm -rf bin/ include/ lib/ local/
sudo -u vagrant virtualenv -p python2.7 --system-site-packages .
sudo -u vagrant bin/pip install -r requirements.txt
",
    require => [Package["python-virtualenv"],
                Package["python-dev"]],
    cwd => "/vagrant",
}

file { "/etc/qemu/fc-qemu.conf":
    content => "\
[qemu]
accelerator =
vhost = false
vnc = ${hostname}:{id}
timeout-graceful = 120
migration-address = tcp:${hostname}:{id}
migration-ctl-address = ${hostname}:9000

[ceph]
client-id = admin
cluster = ceph
lock_host = ${hostname}
create-vm = /usr/local/sbin/create-vm -I {name}
shrink-vm = /usr/local/sbin/shrink-vm {resource_group} {image} {disk}
",
}


##### Consul

exec { 'download consul.zip':
    creates => '/root/consul.zip',
    command => 'wget -ck -O /root/consul.zip https://dl.bintray.com/mitchellh/consul/0.5.0_linux_amd64.zip'
}
