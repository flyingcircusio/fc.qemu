from batou.component import Component
from batou.lib.file import File


class Kvm(Component):
    def configure(self):
        self.provide("enc", {"roles": ["kvm_host"]})

        self |= (kvm_nix := File("/etc/local/nixos/kvm.nix"))
        self.provide("nixos-config", kvm_nix)
