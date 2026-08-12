FROM embeddedanalyzer/emba:2.0.2b

# This image ships as EMBA's own official, maintainer-built analyzer —
# fully populated with binwalk, unblob, a pre-loaded CVE database, and
# every static/dynamic analysis dependency. See DIND_INVESTIGATION_NOTES.md
# for the full investigation that led here: building from source via
# installer.sh was abandoned after two sessions of flag conflicts and
# missing-tool failures — this official image is the correct, maintainer-
# endorsed path.
#
# NOTE: amd64-only (no ARM64 manifest published). Runs via QEMU
# translation on ARM64 hosts — same accepted tradeoff as FirmAE,
# FIRMADYNE, and fact_extractor. Expect materially slower runtimes
# (confirmed: ~13 hours for a full default-scan.emba pass on real
# router firmware under this translation layer).

USER root

# EMBA's own script/module source must be present at /emba at runtime —
# the base image ships this directory empty by design (matches upstream's
# own docker-compose.yml convention of mounting EMBA's repo at runtime).
# We bake it in here instead, at the pinned commit, so this adapter is
# fully self-contained rather than depending on a separate host-side clone.
RUN apt-get update -o Acquire::Check-Date=false 2>/dev/null; \
    (apt-get install -y --no-install-recommends git ca-certificates python3 python3-pip 2>&1 | tail -20) && \
    pip3 install --no-cache-dir --break-system-packages jsonschema && \
    git clone https://github.com/e-m-b-a/emba.git /emba && \
    cd /emba && git checkout 5496d5c0782190f6dfad7cfd598d5826a1b4d620

WORKDIR /firmarbiter_adapter
COPY lifecycle/ ./lifecycle/
COPY entrypoint.py ./entrypoint.py
# The Adapter Contract's runtime paths (see request.json's "paths" key)
# never include a schema mount — confirmed by inspecting
# run_coordinator.py directly, not assumed. In-container event
# validation is a real SDK safety feature (a malformed event should
# never even reach disk), so the schema must be baked into the image
# at build time instead, same as lifecycle/ and entrypoint.py above.
COPY schemas/adapter-event-v1.schema.json ./schemas/adapter-event-v1.schema.json

ENTRYPOINT ["python3", "/firmarbiter_adapter/entrypoint.py"]
