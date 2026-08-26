FROM python:3.11-slim

# docker CLI needed to launch the fact_extractor sibling container via
# the mounted /var/run/docker.sock — this adapter orchestrates rather
# than embeds its candidate tool.
RUN apt-get update && \
    apt-get install -y --no-install-recommends docker.io docker-cli && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir jsonschema

WORKDIR /firmarbiter_adapter
COPY lifecycle/ ./lifecycle/
COPY entrypoint.py ./entrypoint.py
# The Adapter Contract's runtime paths never include a schema mount —
# confirmed by inspecting firmarbiter_core/run_coordinator.py directly.
# Schema must be baked into the image at build time instead.
COPY schemas/adapter-event-v1.schema.json ./schemas/adapter-event-v1.schema.json

ENTRYPOINT ["python3", "/firmarbiter_adapter/entrypoint.py"]
