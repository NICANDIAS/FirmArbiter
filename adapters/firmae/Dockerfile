FROM --platform=linux/amd64 ubuntu:22.04@sha256:ce941a2a18bbb922e434d6d6d2b31e571a5c3826eaf6ada0a41dcc905bd2d906

ARG FIRMAE_COMMIT=dd98505984feac91cf113e13d5de01a4fc1574f2
ARG FIRMAE_PATCH_SHA256=09a68175811a400cbd9584680153c9a6636bf2b806c4a2554f85b9be01ecdbc3
ARG UBUNTU_SNAPSHOT=20260615T120000Z

LABEL org.opencontainers.image.title="FIRMARBITER FirmAE Adapter"
LABEL org.opencontainers.image.source="https://github.com/pr0v3rbs/FirmAE"
LABEL org.opencontainers.image.revision="${FIRMAE_COMMIT}"
LABEL firmarbiter.adapter.id="firmae"
LABEL firmarbiter.adapter.contract-version="1.0"
LABEL firmarbiter.dependencies.ubuntu-snapshot="${UBUNTU_SNAPSHOT}"

ENV DEBIAN_FRONTEND=noninteractive
ENV FIRMAE_HOME=/opt/firmae

COPY dependencies/apt-direct.txt \
    /opt/firmarbiter-adapter/dependencies/apt-direct.txt

COPY scripts/install_apt_dependencies.sh \
    /usr/local/bin/firmarbiter-install-firmae-dependencies

RUN echo 'Acquire::Check-Date "false";' > /etc/apt/apt.conf.d/99no-check-date

RUN chmod 0755 \
        /usr/local/bin/firmarbiter-install-firmae-dependencies \
    && /usr/local/bin/firmarbiter-install-firmae-dependencies \
        "${UBUNTU_SNAPSHOT}" \
        /opt/firmarbiter-adapter/dependencies/apt-direct.txt

COPY dependencies/python-source-lock.tsv \
    /opt/firmarbiter-adapter/dependencies/python-source-lock.tsv

COPY scripts/install_python_dependencies.sh \
    /usr/local/bin/firmarbiter-install-firmae-python-dependencies

RUN chmod 0755 \
        /usr/local/bin/firmarbiter-install-firmae-python-dependencies \
    && /usr/local/bin/firmarbiter-install-firmae-python-dependencies \
        /opt/firmarbiter-adapter/dependencies/python-source-lock.tsv

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
        > "${FIRMAE_HOME}/.firmarbiter_commit"

COPY patches/0001-container-loop-partition-compatibility.patch \
    /opt/firmarbiter-adapter/patches/0001-container-loop-partition-compatibility.patch

RUN test "$(sha256sum \
        /opt/firmarbiter-adapter/patches/0001-container-loop-partition-compatibility.patch \
        | awk '{print $1}')" \
        = "${FIRMAE_PATCH_SHA256}" \
    && git -C "${FIRMAE_HOME}" apply \
        --check \
        /opt/firmarbiter-adapter/patches/0001-container-loop-partition-compatibility.patch \
    && git -C "${FIRMAE_HOME}" apply \
        /opt/firmarbiter-adapter/patches/0001-container-loop-partition-compatibility.patch \
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
        > "${FIRMAE_HOME}/.firmarbiter_patch_sha256"

RUN test -f "${FIRMAE_HOME}/core/unstuff" \
    && install \
        -o root \
        -g root \
        -m 0755 \
        "${FIRMAE_HOME}/core/unstuff" \
        /usr/local/bin/unstuff \
    && sha256sum /usr/local/bin/unstuff \
        > /opt/firmarbiter-adapter/provenance/unstuff.sha256

RUN mkdir -p /opt/firmarbiter-adapter

COPY scripts/database_control.sh \
    /usr/local/bin/firmarbiter-firmae-database

RUN chmod 0755 \
        /usr/local/bin/firmarbiter-firmae-database \
    && test -s "${FIRMAE_HOME}/database/schema" \
    && sha256sum "${FIRMAE_HOME}/database/schema" \
        > /opt/firmarbiter-adapter/provenance/firmae-database-schema.sha256

COPY scripts/unpack_firmware.py \
    /usr/local/bin/firmarbiter-firmae-unpack

RUN chmod 0755 \
        /usr/local/bin/firmarbiter-firmae-unpack

COPY entrypoint.py     /opt/firmarbiter-adapter/entrypoint.py

RUN chmod 0755     /opt/firmarbiter-adapter/entrypoint.py

ENTRYPOINT ["python3", "/opt/firmarbiter-adapter/entrypoint.py"]

# Install FirmAE release assets required for image creation and emulation.
COPY scripts/install_firmae_release_assets.sh /usr/local/bin/install_firmae_release_assets
RUN chmod 0755 /usr/local/bin/install_firmae_release_assets \
    && /usr/local/bin/install_firmae_release_assets

# Verify the separately distributed FirmAE runtime assets against
# the immutable checksum lock captured during clean-room discovery.
COPY dependencies/firmae-release-assets.sha256 /tmp/firmae-release-assets.sha256
RUN mkdir -p /opt/firmarbiter-adapter/locks \
    && install \
        -o root \
        -g root \
        -m 0644 \
        /tmp/firmae-release-assets.sha256 \
        /opt/firmarbiter-adapter/locks/firmae-release-assets.sha256 \
    && cd /opt/firmae \
    && sha256sum -c \
        /opt/firmarbiter-adapter/locks/firmae-release-assets.sha256 \
    && install \
        -o root \
        -g root \
        -m 0644 \
        /opt/firmarbiter-adapter/locks/firmae-release-assets.sha256 \
        /opt/firmarbiter-adapter/provenance/firmae-release-assets.locked.sha256 \
    && rm -f /tmp/firmae-release-assets.sha256

# Candidate execution helpers proven during the first real firmware run.
COPY scripts/prepare_image.sh /usr/local/bin/firmarbiter-firmae-prepare-image
COPY scripts/infer_network.sh /usr/local/bin/firmarbiter-firmae-infer-network
COPY scripts/run_persistent.sh /usr/local/bin/firmarbiter-firmae-run-persistent

RUN chmod 0755 \
    /usr/local/bin/firmarbiter-firmae-prepare-image \
    /usr/local/bin/firmarbiter-firmae-infer-network \
    /usr/local/bin/firmarbiter-firmae-run-persistent \
    && bash -n /usr/local/bin/firmarbiter-firmae-prepare-image \
    && bash -n /usr/local/bin/firmarbiter-firmae-infer-network \
    && bash -n /usr/local/bin/firmarbiter-firmae-run-persistent

# Fail image construction when a required FirmAE runtime dependency is absent.
COPY scripts/verify_dependencies.py \
    /usr/local/bin/firmarbiter-firmae-dependency-doctor

RUN chmod 0755 \
        /usr/local/bin/firmarbiter-firmae-dependency-doctor \
    && /usr/local/bin/firmarbiter-firmae-dependency-doctor \
        --output /opt/firmarbiter-adapter/provenance/dependency-check.json
