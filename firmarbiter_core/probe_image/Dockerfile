FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

RUN echo 'Acquire::Check-Date "false";' > /etc/apt/apt.conf.d/99no-check-date
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -m 0555 /opt/firmarbiter-probe

COPY namespace_probe_worker.py \
    /opt/firmarbiter-probe/worker.py

RUN chmod 0555 /opt/firmarbiter-probe/worker.py \
    && dpkg-query -W \
        -f='${Package}\t${Version}\n' \
        ca-certificates \
        python3 \
        python3-minimal \
        > /opt/firmarbiter-probe/runtime-packages.txt \
    && chmod 0444 \
        /opt/firmarbiter-probe/runtime-packages.txt

USER 65532:65532

ENTRYPOINT ["/usr/bin/python3", "/opt/firmarbiter-probe/worker.py"]
