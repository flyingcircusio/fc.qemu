COPY ../../../../fc.qemu /home/developer/
RUN chown developer: -R /home/developer/fc.qemu
COPY ../../../../fc-nixos /home/developer/
RUN chown developer: -R /home/developer/fc-nixos
RUN /home/developer/fc-nixos/dev-setup
