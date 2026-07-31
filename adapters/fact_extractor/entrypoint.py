#!/usr/bin/env python3
# adapters/_template/entrypoint.py
"""
FIRMARBITER Adapter Template — entrypoint.py

HOW TO USE THIS TEMPLATE:
  Copy this whole adapters/_template/ directory to adapters/<your_tool>/,
  then edit ONLY the three functions marked "FILL IN" below:
    - run_unpack(request, event_writer)
    - run_emulate(request, event_writer)
    - run_endpoint_discovery(request, event_writer)

  Everything else (heartbeats, shutdown handling, event validation,
  request parsing) is already correct and should not need changes.
  This is deliberate: every bug that cost hours on FirmAE/FIRMADYNE
  (malformed events, missed heartbeats, ad-hoc SIGTERM handling) lived
  in exactly this boilerplate, so it now lives in lifecycle/ instead,
  tested once, and reused by every adapter.

  If your tool doesn't do a given stage (e.g. a static-analysis tool
  like EMBA has no 'emulate' or 'endpoint-discovery' stage), just
  return stage_outcome="not_applicable" from that function immediately.
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lifecycle import (
    EventWriter, EventContractError,
    HeartbeatWorker,
    load_and_validate_request, RequestContractError,
    ShutdownCoordinator,
)

SCHEMA_PATH = "/firmarbiter_adapter/schemas/adapter-event-v1.schema.json"  # baked into image at build time — see Dockerfile
REQUEST_PATH = "/firmarbiter/input/request.json"


# ---------------------------------------------------------------------
# FILL IN: tool-specific pipeline stages.
# Each must return a (stage_outcome, message) tuple.
# stage_outcome should be one of: "completed", "failed", "not_applicable"
# (confirm exact allowed values against the schema before onboarding).
# ---------------------------------------------------------------------

def run_unpack(request, event_writer):
    """
    Extract the firmware using fact_extractor (fkiecad/fact_extractor).

    IMPORTANT SEMANTIC NOTE: fact_extractor is a raw binary carver, not a
    filesystem-aware unpacker like FirmAE/FIRMADYNE. Its output is a set of
    flat, offset-named chunks (elf32, elf64, ubi, unknown, ...), not a
    mounted rootfs tree. We place these chunks at the contract-required
    unpack/rootfs/ path for measurement consistency, but "unpack success"
    here means "fact_extractor carved N byte-ranges out of the binary" —
    a materially different and weaker claim than "produced a bootable
    filesystem". This distinction should be preserved in any paper
    comparison against FirmAE/FIRMADYNE.

    fact_extractor runs as a SIBLING container (launched via the Docker
    socket), not embedded in this adapter's own image — mirroring the
    identical-host-path mount pattern required for EMBA. The adapter's
    own container must have /var/run/docker.sock mounted and must itself
    be started with identical host-side paths (see adapter.yaml runtime
    notes) for this sibling-container launch to resolve paths correctly.
    """
    import shutil
    import subprocess
    from pathlib import Path

    firmware_path = Path(request.firmware.path)
    artifacts_path = Path(request.paths.artifacts)

    # fact_extractor needs its own working folder with input/files/reports
    # pre-created — confirmed by manual testing (missing dirs cause a
    # FileNotFoundError writing meta.json, not a clean error message).
    work_root = artifacts_path / "unpack" / "_fact_extractor_run"
    input_dir = work_root / "input"
    files_dir = work_root / "files"
    reports_dir = work_root / "reports"
    for d in (input_dir, files_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    # fact_extractor grabs the first file in input/ — filename doesn't matter.
    shutil.copy(firmware_path, input_dir / firmware_path.name)

    # NOTE: work_root must be a real host path (not a container-private
    # path) for the sibling container's bind mount to resolve correctly,
    # per the identical-path-mount requirement discovered onboarding EMBA.
    import os
    current_uid = os.getuid()
    current_gid = os.getgid()

    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--privileged",
                "-v", "/dev:/dev",
                "-v", f"{work_root}:/tmp/extractor",
                "fkiecad/fact_extractor",
                # fact_extractor runs as root inside its privileged container,
                # leaving root-owned output on the host mount. Its own
                # docker_extraction.py provides --chown for exactly this —
                # discovered when the smoke test harness's own tempdir
                # cleanup failed with PermissionError on root-owned files.
                "--chown", f"{current_uid}:{current_gid}",
            ],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return "failed", "fact_extractor sibling container timed out after 600s"

    # The sibling container runs as root inside its own namespace, leaving
    # root-owned files on the shared host mount. --chown didn't fix this,
    # because os.getuid()/os.getgid() measured *inside this adapter's own
    # container* are also 0 (root) by default — telling fact_extractor to
    # chown to 0:0 is a no-op. Since this adapter container is also root,
    # it can chmod the tree open regardless of ownership, so downstream
    # cleanup (e.g. the smoke test harness, running as a normal host user)
    # can delete it. Discovered onboarding fact_extractor.
    subprocess.run(["chmod", "-R", "a+rwX", str(work_root)], check=False)

    meta_json_path = reports_dir / "meta.json"
    if not meta_json_path.exists():
        return "failed", (
            f"fact_extractor produced no meta.json (exit code {result.returncode}). "
            f"stderr: {result.stderr[-500:]}"
        )

    import json
    try:
        meta = json.loads(meta_json_path.read_text())
    except json.JSONDecodeError as e:
        return "failed", f"meta.json was produced but is not valid JSON: {e}"

    num_unpacked = meta.get("number_of_unpacked_files", 0)

    # Place carved chunks at the contract-required rootfs path. This is a
    # flat copy, not a filesystem reconstruction — see semantic note above.
    rootfs_dir = artifacts_path / "unpack" / "rootfs"
    rootfs_dir.mkdir(parents=True, exist_ok=True)
    for chunk_file in files_dir.iterdir():
        shutil.copy(chunk_file, rootfs_dir / chunk_file.name)

    if num_unpacked == 0:
        return "failed", "fact_extractor ran but extracted 0 files"

    return "completed", (
        f"fact_extractor carved {num_unpacked} chunk(s) from firmware "
        f"(NOTE: raw binary carving, not filesystem-aware unpacking — "
        f"see code comment for semantic distinction from FirmAE/FIRMADYNE)"
    )


def run_emulate(request, event_writer):
    """fact_extractor is a pure static binary carver — it does not boot
    or emulate firmware in any way."""
    return "not_applicable", "fact_extractor does not boot or emulate firmware (static extraction only)"


def run_endpoint_discovery(request, event_writer):
    """fact_extractor never boots the firmware, so it cannot discover or
    claim any running network endpoints."""
    return "not_applicable", "fact_extractor does not run firmware, so no endpoints can be discovered"


# ---------------------------------------------------------------------
# Contract wiring — should not need edits for a normal adapter.
# ---------------------------------------------------------------------

STAGE_FUNCTIONS = {
    "unpack": run_unpack,
    "emulate": run_emulate,
    "endpoint-discovery": run_endpoint_discovery,
}


def main():
    try:
        request = load_and_validate_request(REQUEST_PATH)
    except RequestContractError as e:
        # Nothing to emit events through yet if the request itself is broken —
        # write to stderr and exit non-zero so the coordinator sees a clear
        # infrastructure/contract failure rather than a silent hang.
        print(f"FATAL: request.json invalid: {e}", file=sys.stderr)
        sys.exit(1)

    event_writer = EventWriter(
        events_path=request.paths.events,
        schema_path=SCHEMA_PATH,
        run_id=request.run.run_id,
        adapter_id=request.run.adapter_id,
        schema_version=request.schema_version,
        contract_version=request.contract_version,
    )

    current_stage_holder = {"stage": None}
    heartbeat = HeartbeatWorker(
        event_writer,
        interval_seconds=request.lifecycle.heartbeat_interval_seconds,
        state_provider=lambda: current_stage_holder["stage"],
    )

    shutdown = ShutdownCoordinator(control_dir=request.paths.control)
    shutdown.start_polling()

    try:
        event_writer.adapter_started(message="Adapter starting")
        heartbeat.start()

        for stage in request.run.requested_stages:
            if shutdown.shutdown_requested():
                break  # coordinator asked us to stop early

            current_stage_holder["stage"] = stage
            fn = STAGE_FUNCTIONS.get(stage)
            if fn is None:
                event_writer.error(
                    code="UNKNOWN_STAGE",
                    phase="contract",
                    message=f"Unknown requested stage: {stage}",
                    recoverable=True,
                )
                continue

            try:
                stage_outcome, message = fn(request, event_writer)
            except NotImplementedError as e:
                event_writer.error(
                    code="STAGE_NOT_IMPLEMENTED",
                    phase="adapter_setup",
                    message=str(e),
                    recoverable=False,
                )
                # Fatal, but the adapter must still report its terminal
                # lifecycle events before exiting — an early exit that
                # skips shutdown_started/cleanup_complete/adapter_stopped
                # is exactly the "unexpected_early_exit" failure class
                # seen during FIRMADYNE debugging, where the coordinator
                # could not distinguish a hung adapter from one that had
                # already failed cleanly.
                heartbeat.stop()
                event_writer.shutdown_started(message="Fatal error during stage execution")
                event_writer.cleanup_complete(message="No cleanup required after fatal error")
                event_writer.adapter_stopped(outcome="failed", message=str(e))
                sys.exit(1)
            except Exception as e:
                event_writer.error(
                    code="STAGE_EXECUTION_FAILED",
                    phase="candidate_execution",
                    message=f"Stage '{stage}' raised: {e}\n{traceback.format_exc()}",
                    recoverable=True,
                )
                stage_outcome, message = "failed", f"Unhandled exception: {e}"

            event_writer.stage_completed(
                stage=stage, stage_outcome=stage_outcome, message=message
            )

        # Wait for the coordinator's explicit shutdown signal before tearing
        # down, honouring shutdown_grace_seconds from the request.
        shutdown.wait_for_shutdown(timeout=request.lifecycle.shutdown_grace_seconds)

        event_writer.shutdown_started(message="Shutdown requested or stages complete")
        heartbeat.stop()
        # FILL IN (optional): any tool-specific cleanup goes here, e.g.
        # removing loop devices, killing lingering QEMU processes, etc.
        event_writer.cleanup_complete(message="Cleanup finished")
        event_writer.adapter_stopped(outcome="completed", message="Adapter exiting normally")

    except EventContractError as e:
        # An event itself was malformed — this is an adapter_failure, not a
        # candidate_outcome. Fail loudly rather than silently dropping it.
        print(f"FATAL: event contract violation: {e}", file=sys.stderr)
        heartbeat.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
