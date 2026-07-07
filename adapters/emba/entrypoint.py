#!/usr/bin/env python3
# adapters/_template/entrypoint.py
"""
VERITAS Adapter Template — entrypoint.py

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

SCHEMA_PATH = "/veritas/schemas/adapter-event-v1.schema.json"
REQUEST_PATH = "/veritas/input/request.json"


# ---------------------------------------------------------------------
# FILL IN: tool-specific pipeline stages.
# Each must return a (stage_outcome, message) tuple.
# stage_outcome should be one of: "completed", "failed", "not_applicable"
# (confirm exact allowed values against the schema before onboarding).
# ---------------------------------------------------------------------

def run_unpack(request, event_writer):
    """Extract the firmware. Must leave the rootfs at
    <artifacts_path>/unpack/rootfs/ — VERITAS measures unpack success by
    inspecting that path directly, not by trusting this return value."""
    raise NotImplementedError("Fill in run_unpack for your tool")


def run_emulate(request, event_writer):
    """Boot/emulate the firmware, if your tool does this. Return
    ('not_applicable', '...') if your tool is static-analysis only."""
    raise NotImplementedError("Fill in run_emulate for your tool")


def run_endpoint_discovery(request, event_writer):
    """Report any endpoints your tool's own instrumentation finds.
    Note: VERITAS's neutral probe independently verifies reachability —
    this stage is about your tool's *claims*, not the verified result.
    Return ('not_applicable', '...') if not relevant to your tool."""
    raise NotImplementedError("Fill in run_endpoint_discovery for your tool")


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
