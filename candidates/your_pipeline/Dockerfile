# candidates/your_pipeline/Dockerfile
# Compatible with legacy Docker builder. No BuildKit required.
# Default is local mode since your pipeline is not yet public.
# Change TOOL_SOURCE to git and set TOOL_REPO when you publish it.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

ARG TOOL_SOURCE=local
ARG TOOL_REPO=https://github.com/yourusername/firmware-security.git

RUN apt-get update -qq || true && apt-get install -y --no-install-recommends \
    git python3 python3-pip binwalk \
    qemu-system-arm qemu-system-mips qemu-system-x86 qemu-utils \
    net-tools iproute2 unzip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN if [ "$TOOL_SOURCE" = "git" ]; then \
        git clone --depth 1 "$TOOL_REPO" pipeline && \
        cd pipeline && git rev-parse HEAD > .veritas_commit; \
    else \
        echo "local mode: pipeline must be mounted at runtime" && \
        mkdir -p /opt/pipeline && \
        echo "local-build" > /opt/pipeline/.veritas_commit; \
    fi

WORKDIR /opt/pipeline
RUN pip3 install --quiet -r requirements.txt 2>/dev/null || true

COPY run_adapter.sh /opt/run_adapter.sh
RUN chmod +x /opt/run_adapter.sh

ENTRYPOINT ["bash", "/opt/run_adapter.sh"]
