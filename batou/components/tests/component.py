from batou.component import Component
from batou.lib.file import File


class Tests(Component):
    def configure(self):
        self |= (fakedir := File("/etc/local/nixos/fakedirectory.py"))
        self.provide("nixos-config", fakedir)

        self |= (tests_nix := File("/etc/local/nixos/tests.nix"))
        self.provide("nixos-config", tests_nix)
