# adapters/firmadyne/Dockerfile
# Builds the FIRMARBITER FIRMADYNE adapter image.
# Platform: linux/amd64
FROM --platform=linux/amd64 ubuntu:22.04@sha256:ce941a2a18bbb922e434d6d6d2b31e571a5c3826eaf6ada0a41dcc905bd2d906

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

RUN echo 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/99firmarbiter && \
    echo 'Acquire::http::Pipeline-Depth "0";' >> /etc/apt/apt.conf.d/99firmarbiter

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        git wget curl sudo build-essential \
        python3 python3-pip python-is-python3 \
        qemu-system-arm qemu-system-mips qemu-system-x86 qemu-utils \
        busybox-static fakeroot kpartx uml-utilities util-linux vlan \
        arping net-tools iproute2 iputils-ping \
        postgresql postgresql-client python3-psycopg2 \
        python3-magic squashfs-tools bzip2 file fdisk \
        python3-six binwalk unzip p7zip-full \
        liblzma-dev liblzo2-dev zlib1g-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Create firmadyne system user required by makeImage.sh
RUN useradd -m -s /bin/bash firmadyne && echo 'firmadyne ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Pre-initialise PostgreSQL cluster at build time so runtime start is fast
# and does not require amd64 pg_createcluster under QEMU translation.
RUN pg_createcluster 14 main --start-conf=manual || true

# Sasquatch — FIRMADYNE-documented revision for big-endian LZMA SquashFS
ARG SASQUATCH_REPO=https://github.com/firmadyne/sasquatch.git
ARG SASQUATCH_COMMIT=287e4a8e059d3ee7a5f643211fcf00c292cd6f4d

RUN git clone "${SASQUATCH_REPO}" /tmp/sasquatch && \
    cd /tmp/sasquatch && \
    git checkout "${SASQUATCH_COMMIT}" && \
    sed -i '/#include "unsquashfs.h"/i #include <sys/sysmacros.h>' unsquashfs.c && \
    sed -i 's/-Werror/-Werror -Wno-error=misleading-indentation -fcommon/g' Makefile && \
    make -j"$(nproc)" && \
    make install && \
    rm -rf /tmp/sasquatch

# FIRMADYNE — pinned upstream commit
ARG FIRMADYNE_REPO=https://github.com/firmadyne/firmadyne.git
ARG FIRMADYNE_COMMIT=11cb574cda0a5540ab7e36aa9b43679dc02c5788

RUN git clone "${FIRMADYNE_REPO}" /opt/firmadyne && \
    cd /opt/firmadyne && \
    git checkout "${FIRMADYNE_COMMIT}" && \
    git submodule update --init --recursive

# Download FIRMADYNE runtime binaries (kernels, console, libnvram)
RUN cd /opt/firmadyne && sh ./download.sh

# Apply FIRMARBITER compatibility patch to makeImage.sh
COPY patches/0001-makeimage-cleanup-compatibility.patch /tmp/
RUN cd /opt/firmadyne && \
    patch -p1 < /tmp/0001-makeimage-cleanup-compatibility.patch && \
    rm /tmp/0001-makeimage-cleanup-compatibility.patch

# PostgreSQL setup for FIRMADYNE
RUN service postgresql start && \
    sleep 3 && \
    su postgres -c "psql -c \"CREATE USER firmadyne WITH PASSWORD 'firmadyne';\"" && \
    su postgres -c "createdb -O firmadyne firmware" && \
    su postgres -c "psql -d firmware -f /opt/firmadyne/database/schema" && \
    service postgresql stop

# FIRMARBITER entrypoint
COPY entrypoint.py /firmarbiter_entrypoint.py
RUN chmod +x /firmarbiter_entrypoint.py

WORKDIR /opt/firmadyne
ENTRYPOINT ["python3", "/firmarbiter_entrypoint.py"]
