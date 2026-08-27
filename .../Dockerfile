# adapters/_template/Dockerfile
#
# Starter Dockerfile for a new adapter. The lifecycle/, entrypoint.py,
# and schemas/ COPY lines below are the same working pattern used by
# every real adapter in this project — you should not need to change
# them. What you DO need to add: installing your actual candidate tool
# and its real dependencies.
#
# Prefer scaffolding this automatically where possible:
#   python3 run_firmarbiter.py --new-adapter <name>
# which generates this same file for you.

FROM ubuntu:22.04

# Avoids a real mirror clock-skew failure mode hit during this
# project's own development (GPG signature date validation against a
# snapshot mirror).
RUN echo 'Acquire::Check-Date "false";' > /etc/apt/apt.conf.d/99no-check-date

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# --- FILL IN: install your actual candidate tool and its real
# --- dependencies here. This is the genuinely tool-specific part no
# --- template can do for you.

WORKDIR /firmarbiter_adapter
COPY lifecycle/ ./lifecycle/
COPY entrypoint.py ./entrypoint.py
COPY schemas/adapter-event-v1.schema.json ./schemas/adapter-event-v1.schema.json
RUN chmod +x ./entrypoint.py

ENTRYPOINT ["python3", "./entrypoint.py"]
