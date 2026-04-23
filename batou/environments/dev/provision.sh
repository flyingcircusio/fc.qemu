COPY ../../../../fc.qemu /home/developer/
RUN chown developer: -R /home/developer/fc.qemu
# Use this if you want to work with a developer checkout
# of the fc-nixos platform. Also see environment.cfg
#COPY ~/PATH/TO/LOCAL/fc-nixos/ /home/developer/fc-nixos
#RUN chown developer: -R /home/developer/fc-nixos
#RUN rm -rf /home/developer/fc-nixos/channels
## un-confuse nix flake references
#RUN rm -rf /home/developer/fc-nixos/.git
#RUN /home/developer/fc-nixos/dev-setup
