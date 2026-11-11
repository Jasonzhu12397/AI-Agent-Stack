FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl debian-archive-keyring live-build debootstrap xorriso squashfs-tools dosfstools mtools rsync gnupg && rm -rf /var/lib/apt/lists/*
WORKDIR /work
ENTRYPOINT ["/bin/bash"]
