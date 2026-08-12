# adapters/_template/Dockerfile
#
# Reference Dockerfile pattern for a FIRMARBITER adapter. Copy this as a
# starting point — the FROM line and any tool-installation steps are
# entirely candidate-specific, but the final block (lifecycle/, entrypoint.py,
# and the event schema) is REQUIRED boilerplate every adapter needs.
#
# CRITICAL: the Adapter Contract's runtime paths (see request.json's
# "paths" key, and firmarbiter_core/run_coordinator.py) never mount a
# schema file into your container at runtime — confirmed by inspecting
# the coordinator directly. If you hardcode a schema path expecting it
# to be mounted, your adapter WILL crash with FileNotFoundError on its
# first real run (this exact bug was found and fixed in the emba and
# fact_extractor adapters). The schema must be baked into your image at
# BUILD time instead, via the COPY step below.

FROM your-base-image:tag

# ... your tool-specific installation steps here ...

WORKDIR /firmarbiter_adapter
COPY lifecycle/ ./lifecycle/
COPY entrypoint.py ./entrypoint.py
COPY schemas/adapter-event-v1.schema.json ./schemas/adapter-event-v1.schema.json

ENTRYPOINT ["python3", "/firmarbiter_adapter/entrypoint.py"]
