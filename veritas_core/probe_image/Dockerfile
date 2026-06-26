FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -m 0555 /opt/veritas-probe

COPY namespace_probe_worker.py \
    /opt/veritas-probe/worker.py

RUN chmod 0555 /opt/veritas-probe/worker.py \
    && dpkg-query -W \
        -f='${Package}\t${Version}\n' \
        ca-certificates \
        python3 \
        python3-minimal \
        > /opt/veritas-probe/runtime-packages.txt \
    && chmod 0444 \
        /opt/veritas-probe/runtime-packages.txt

USER 65532:65532

ENTRYPOINT ["/usr/bin/python3", "/opt/veritas-probe/worker.py"]
