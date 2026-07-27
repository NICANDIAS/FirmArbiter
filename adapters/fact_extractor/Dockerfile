FROM python:3.11-slim

# docker CLI needed to launch the fact_extractor sibling container via
# the mounted /var/run/docker.sock — this adapter orchestrates rather
# than embeds its candidate tool.
RUN apt-get update && \
    apt-get install -y --no-install-recommends docker.io && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir jsonschema

WORKDIR /veritas_adapter
COPY lifecycle/ ./lifecycle/
COPY entrypoint.py ./entrypoint.py

ENTRYPOINT ["python3", "/veritas_adapter/entrypoint.py"]
