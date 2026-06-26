FROM --platform=linux/amd64 ubuntu:22.04@sha256:ce941a2a18bbb922e434d6d6d2b31e571a5c3826eaf6ada0a41dcc905bd2d906

ARG FIRMAE_COMMIT=dd98505984feac91cf113e13d5de01a4fc1574f2
ARG FIRMAE_PATCH_SHA256=09a68175811a400cbd9584680153c9a6636bf2b806c4a2554f85b9be01ecdbc3
ARG UBUNTU_SNAPSHOT=20260615T120000Z

LABEL org.opencontainers.image.title="VERITAS FirmAE Adapter"
LABEL org.opencontainers.image.source="https://github.com/pr0v3rbs/FirmAE"
LABEL org.opencontainers.image.revision="${FIRMAE_COMMIT}"
LABEL veritas.adapter.id="firmae"
LABEL veritas.adapter.contract-version="1.0"
LABEL veritas.dependencies.ubuntu-snapshot="${UBUNTU_SNAPSHOT}"

ENV DEBIAN_FRONTEND=noninteractive
ENV FIRMAE_HOME=/opt/firmae

COPY dependencies/apt-direct.txt \
    /opt/veritas-adapter/dependencies/apt-direct.txt

COPY scripts/install_apt_dependencies.sh \
    /usr/local/bin/veritas-install-firmae-dependencies

RUN chmod 0755 \
        /usr/local/bin/veritas-install-firmae-dependencies \
    && /usr/local/bin/veritas-install-firmae-dependencies \
        "${UBUNTU_SNAPSHOT}" \
        /opt/veritas-adapter/dependencies/apt-direct.txt

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

COPY patches/0001-container-loop-partition-compatibility.patch \
    /opt/veritas-adapter/patches/0001-container-loop-partition-compatibility.patch

RUN test "$(sha256sum \
        /opt/veritas-adapter/patches/0001-container-loop-partition-compatibility.patch \
        | awk '{print $1}')" \
        = "${FIRMAE_PATCH_SHA256}" \
    && git -C "${FIRMAE_HOME}" apply \
        --check \
        /opt/veritas-adapter/patches/0001-container-loop-partition-compatibility.patch \
    && git -C "${FIRMAE_HOME}" apply \
        /opt/veritas-adapter/patches/0001-container-loop-partition-compatibility.patch \
    && git -C "${FIRMAE_HOME}" diff --check \
    && grep -F \
        'SYSDEV="/sys/block/${LOOP_BASE}/${PART_BASE}/dev"' \
        "${FIRMAE_HOME}/firmae.config" \
    && grep -F \
        'mknod "${DEV_PATH}" b "${MAJ}" "${MIN}"' \
        "${FIRMAE_HOME}/firmae.config" \
    && grep -F \
        'kpartx -avs "${LOOP_DEV}"' \
        "${FIRMAE_HOME}/firmae.config" \
    && printf '%s\n' "${FIRMAE_PATCH_SHA256}" \
        > "${FIRMAE_HOME}/.veritas_patch_sha256"

RUN test -f "${FIRMAE_HOME}/core/unstuff" \
    && install \
        -o root \
        -g root \
        -m 0755 \
        "${FIRMAE_HOME}/core/unstuff" \
        /usr/local/bin/unstuff \
    && sha256sum /usr/local/bin/unstuff \
        > /opt/veritas-adapter/provenance/unstuff.sha256

RUN mkdir -p /opt/veritas-adapter

COPY scripts/database_control.sh \
    /usr/local/bin/veritas-firmae-database

RUN chmod 0755 \
        /usr/local/bin/veritas-firmae-database \
    && test -s "${FIRMAE_HOME}/database/schema" \
    && sha256sum "${FIRMAE_HOME}/database/schema" \
        > /opt/veritas-adapter/provenance/firmae-database-schema.sha256

COPY scripts/unpack_firmware.py \
    /usr/local/bin/veritas-firmae-unpack

RUN chmod 0755 \
        /usr/local/bin/veritas-firmae-unpack

COPY entrypoint.py     /opt/veritas-adapter/entrypoint.py

RUN chmod 0755     /opt/veritas-adapter/entrypoint.py

ENTRYPOINT ["python3", "/opt/veritas-adapter/entrypoint.py"]

# Install FirmAE release assets required for image creation and emulation.
COPY scripts/install_firmae_release_assets.sh /usr/local/bin/install_firmae_release_assets
RUN chmod 0755 /usr/local/bin/install_firmae_release_assets \
    && /usr/local/bin/install_firmae_release_assets

# Verify the separately distributed FirmAE runtime assets against
# the immutable checksum lock captured during clean-room discovery.
COPY dependencies/firmae-release-assets.sha256 /tmp/firmae-release-assets.sha256
RUN mkdir -p /opt/veritas-adapter/locks \
    && install \
        -o root \
        -g root \
        -m 0644 \
        /tmp/firmae-release-assets.sha256 \
        /opt/veritas-adapter/locks/firmae-release-assets.sha256 \
    && cd /opt/firmae \
    && sha256sum -c \
        /opt/veritas-adapter/locks/firmae-release-assets.sha256 \
    && install \
        -o root \
        -g root \
        -m 0644 \
        /opt/veritas-adapter/locks/firmae-release-assets.sha256 \
        /opt/veritas-adapter/provenance/firmae-release-assets.locked.sha256 \
    && rm -f /tmp/firmae-release-assets.sha256

# Candidate execution helpers proven during the first real firmware run.
COPY scripts/prepare_image.sh /usr/local/bin/veritas-firmae-prepare-image
COPY scripts/infer_network.sh /usr/local/bin/veritas-firmae-infer-network
COPY scripts/run_persistent.sh /usr/local/bin/veritas-firmae-run-persistent

RUN chmod 0755 \
    /usr/local/bin/veritas-firmae-prepare-image \
    /usr/local/bin/veritas-firmae-infer-network \
    /usr/local/bin/veritas-firmae-run-persistent \
    && bash -n /usr/local/bin/veritas-firmae-prepare-image \
    && bash -n /usr/local/bin/veritas-firmae-infer-network \
    && bash -n /usr/local/bin/veritas-firmae-run-persistent
