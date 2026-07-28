FROM kalilinux/kali-rolling

RUN apt-get update && \
    apt-get -y install wget kmod procps sudo dialog apt curl git ca-certificates && \
    git clone https://github.com/e-m-b-a/emba.git /emba_src && \
    cd /emba_src && \
    git checkout 5496d5c0782190f6dfad7cfd598d5826a1b4d620

COPY patches/arm64-compat.patch /tmp/arm64-compat.patch
RUN cd /emba_src && git apply /tmp/arm64-compat.patch && rm /tmp/arm64-compat.patch

COPY installer.sh /emba_src/installer.sh
WORKDIR /emba_src

RUN --mount=type=bind,target=/installer,source=./installer \
    yes | /emba_src/installer.sh -s -g && \
    ulimit -c 0 && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip && \
    pip3 install --no-cache-dir --break-system-packages jsonschema && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /firmarbiter_adapter
COPY lifecycle/ ./lifecycle/
COPY entrypoint.py ./entrypoint.py

ENTRYPOINT ["python3", "/firmarbiter_adapter/entrypoint.py"]
