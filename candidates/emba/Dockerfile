# candidates/emba/Dockerfile
# Clones emba from GitHub. No BuildKit required.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

ARG TOOL_REPO=https://github.com/e-m-b-a/emba.git

RUN echo 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/99firmarbiter &&     echo 'Acquire::http::Pipeline-Depth "0";' >> /etc/apt/apt.conf.d/99firmarbiter

RUN apt-get update -qq || true &&     apt-get install -y --no-install-recommends --fix-missing     git wget curl sudo python3 python3-pip     qemu-system-arm qemu-system-mips qemu-system-x86 qemu-utils     busybox-static fakeroot kpartx uml-utilities util-linux vlan     arping net-tools iproute2 iputils-ping     postgresql postgresql-client binwalk unzip p7zip-full     liblzma-dev liblzo2-dev zlib1g-dev     && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN git clone --depth 1 "$TOOL_REPO" emba &&     cd emba &&     git rev-parse HEAD > .firmarbiter_commit

WORKDIR /opt/emba

RUN sudo bash ./installer.sh -d 2>/dev/null || true

COPY run_adapter.sh /opt/run_adapter.sh
RUN chmod +x /opt/run_adapter.sh

WORKDIR /opt/emba
ENTRYPOINT ["bash", "/opt/run_adapter.sh"]
