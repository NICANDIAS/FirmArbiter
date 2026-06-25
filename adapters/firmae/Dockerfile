FROM --platform=linux/amd64 ubuntu:22.04@sha256:ce941a2a18bbb922e434d6d6d2b31e571a5c3826eaf6ada0a41dcc905bd2d906

ARG FIRMAE_COMMIT=dd98505984feac91cf113e13d5de01a4fc1574f2

LABEL org.opencontainers.image.title="VERITAS FirmAE Adapter"
LABEL org.opencontainers.image.source="https://github.com/pr0v3rbs/FirmAE"
LABEL org.opencontainers.image.revision="${FIRMAE_COMMIT}"
LABEL veritas.adapter.id="firmae"
LABEL veritas.adapter.contract-version="1.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV FIRMAE_HOME=/opt/firmae

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        python3 \
    && rm -rf /var/lib/apt/lists/*

RUN git init "${FIRMAE_HOME}" \
    && git -C "${FIRMAE_HOME}" remote add origin \
        https://github.com/pr0v3rbs/FirmAE.git \
    && git -C "${FIRMAE_HOME}" fetch \
        --depth 1 origin "${FIRMAE_COMMIT}" \
    && git -C "${FIRMAE_HOME}" checkout \
        --detach FETCH_HEAD \
    && test "$(git -C "${FIRMAE_HOME}" rev-parse HEAD)" \
        = "${FIRMAE_COMMIT}" \
    && git -C "${FIRMAE_HOME}" \
        submodule update --init --recursive \
    && printf '%s\n' "${FIRMAE_COMMIT}" \
        > "${FIRMAE_HOME}/.veritas_commit"

RUN mkdir -p /opt/veritas-adapter

COPY entrypoint.py     /opt/veritas-adapter/entrypoint.py

RUN chmod 0755     /opt/veritas-adapter/entrypoint.py

ENTRYPOINT ["python3", "/opt/veritas-adapter/entrypoint.py"]
