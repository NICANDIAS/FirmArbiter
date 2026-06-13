# candidates/firmae/Dockerfile
# VERITAS FirmAE Docker image.
# Builds FirmAE, then applies the VERITAS Docker partition fix after install.sh.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

ARG TOOL_REPO=https://github.com/pr0v3rbs/FirmAE.git

RUN echo 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/99veritas && \
    echo 'Acquire::http::Pipeline-Depth "0";' >> /etc/apt/apt.conf.d/99veritas

RUN apt-get update -qq || true && \
    apt-get install -y --no-install-recommends --fix-missing \
    git wget curl sudo python3 python3-pip \
    qemu-system-arm qemu-system-mips qemu-system-x86 qemu-utils \
    busybox-static fakeroot kpartx uml-utilities util-linux vlan \
    arping net-tools iproute2 iputils-ping \
    postgresql postgresql-client binwalk unzip p7zip-full \
    liblzma-dev liblzo2-dev zlib1g-dev fdisk e2fsprogs mtd-utils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN git clone --depth 1 "$TOOL_REPO" firmae && \
    cd firmae && \
    git rev-parse HEAD > .veritas_commit

WORKDIR /opt/firmae

RUN bash ./download.sh 2>/dev/null || true

RUN bash ./install.sh 2>/dev/null || true

# VERITAS permanent FirmAE Docker partition fix.
# This is applied after install.sh because install.sh may rewrite firmae.config.
COPY veritas_patch_firmae_config.py /tmp/veritas_patch_firmae_config.py

RUN python3 /tmp/veritas_patch_firmae_config.py && \
    grep -n "SYSDEV=\"/sys/block" /opt/firmae/firmae.config && \
    grep -n "mknod.*DEV_PATH" /opt/firmae/firmae.config && \
    grep -n "kpartx -avs" /opt/firmae/firmae.config

COPY run_adapter.sh /opt/run_adapter.sh
RUN chmod +x /opt/run_adapter.sh

WORKDIR /opt/firmae

ENTRYPOINT ["bash", "/opt/run_adapter.sh"]
