COPY ../../../../fc.qemu /home/developer/
RUN chown developer: -R /home/developer/fc.qemu
# Use this if you want to work with a developer checkout
# of the fc-nixos platform.
#COPY ../../../../fc-nixos /home/developer/
#RUN chown developer: -R /home/developer/fc-nixos
#RUN rm -rf /home/developer/fc-nixos/channels
#RUN /home/developer/fc-nixos/dev-setup
