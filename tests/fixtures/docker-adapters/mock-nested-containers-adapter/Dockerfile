FROM python@sha256:6c4dd321d176d61ea848dc8c73a4f7dbae8f70e0ee48bb411ea2f045b599fa8e

# docker-cli explicitly, not just docker.io — Debian trixie only
# *recommends* docker-cli alongside docker.io, and --no-install-recommends
# skips it, which was the real cause of fact_extractor's "docker: not
# found" bug earlier tonight. Naming it explicitly here from the start.
RUN apt-get update && \
    apt-get install -y --no-install-recommends docker.io docker-cli && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /adapter

COPY mock_nested_adapter.py /adapter/mock_nested_adapter.py

ENTRYPOINT ["python3", "/adapter/mock_nested_adapter.py"]
