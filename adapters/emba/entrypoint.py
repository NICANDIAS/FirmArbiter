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

def run_unpack(request, event_writer, shutdown=None):
    """
    Run EMBA's default-scan.emba profile against the firmware, then locate
    the real extracted rootfs and copy it to the contract-required path.

    IMPORTANT — confirmed via a full real run (DIR-868L REVB, ~13 hours
    under QEMU amd64-on-arm64 translation, see DIND_INVESTIGATION_NOTES.md):

    - EMBA runs its ENTIRE scan (unpack + all static analysis modules) in
      one invocation — there is no separate "just unpack" mode in the
      default-scan.emba profile. So this function does the full EMBA run;
      run_emulate/run_endpoint_discovery below just report on what EMBA
      already did, rather than triggering separate stages.
    - This is SLOW. Real confirmed runtime: ~13 hours for one firmware
      sample under QEMU translation. This must be reflected in whatever
      timeout the calling coordinator uses — do not assume a short-lived
      process.
    - The real extracted rootfs lands at an unpredictable nested path:
        firmware/binwalk_extracted/<file>.extracted/0/<inner>.extracted/<hex_offset>/squashfs-root/
      The hex offset varies per firmware sample, so we search recursively
      for a directory containing markers of a real Linux rootfs (both an
      'etc' and a 'bin' subdirectory) rather than assume a fixed path.
    - Must run with an explicit memory cap (handled by the caller/adapter
      contract's resource limits, not inside this function) — an
      uncapped EMBA run was confirmed to trigger the HOST machine's OOM
      killer, not just the container's, during real testing.
    """
    import shutil
    import subprocess
    from pathlib import Path

    firmware_path = Path(request.firmware.path)
    artifacts_path = Path(request.paths.artifacts)

    log_dir = artifacts_path / "unpack" / "_emba_run"
    log_dir.mkdir(parents=True, exist_ok=True)

    # IMPORTANT (confirmed via manual, isolated testing — see
    # GRACEFUL_SHUTDOWN_INVESTIGATION.md): a plain blocking subprocess.run()
    # here makes the adapter completely unresponsive to SIGTERM/shutdown
    # requests for the entire ~13-hour scan duration, since the main
    # thread cannot act on the shutdown flag while blocked inside the
    # call, AND the real EMBA subprocess is never actually terminated by
    # a signal sent to this Python process alone. Confirmed via direct
    # test: docker kill --signal=SIGTERM had zero effect after 20+
    # seconds, with real EMBA child processes still consuming CPU.
    #
    # Fix: launch via Popen in its own process group, then poll in a loop
    # that checks BOTH process completion and the shutdown flag, so a
    # shutdown request can actually kill the real work promptly instead
    # of being silently ignored for hours.
    import os
    import signal as signal_module

    proc = subprocess.Popen(
        [
            "/emba/emba",
            "-l", str(log_dir),
            "-f", str(firmware_path),
            "-p", "/emba/scan-profiles/default-scan.emba",
            "-F",
            "-i",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,  # own process group, so we can kill EMBA's
                                    # many child workers together, not just
                                    # this one immediate child
    )

    poll_interval_seconds = 5
    max_wait_seconds = 50000  # generous safety net; real confirmed runtime ~13h
    elapsed = 0
    was_shutdown_requested = False

    while True:
        try:
            proc.wait(timeout=poll_interval_seconds)
            break  # process finished naturally
        except subprocess.TimeoutExpired:
            elapsed += poll_interval_seconds
            if shutdown is not None and shutdown.shutdown_requested():
                was_shutdown_requested = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal_module.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal_module.SIGKILL)
                    except ProcessLookupError:
                        pass
                break
            if elapsed >= max_wait_seconds:
                try:
                    os.killpg(os.getpgid(proc.pid), signal_module.SIGKILL)
                except ProcessLookupError:
                    pass
                return "failed", (
                    f"EMBA exceeded the {max_wait_seconds}s safety-net timeout "
                    f"and was killed."
                )

    if was_shutdown_requested:
        return "failed", (
            "EMBA was terminated early because the coordinator requested "
            "shutdown before the scan completed naturally."
        )

    stdout, stderr = proc.communicate()

    class _Result:
        pass
    result = _Result()
    result.returncode = proc.returncode
    result.stdout = stdout
    result.stderr = stderr

    # Search for the real extracted rootfs — path depth/naming is not
    # fixed (binwalk names nested dirs by hex offset), so search by
    # content markers instead of a hardcoded path.
    binwalk_root = log_dir / "firmware" / "binwalk_extracted"
    found_rootfs = None
    if binwalk_root.exists():
        for candidate in binwalk_root.rglob("*"):
            if candidate.is_dir() and (candidate / "etc").is_dir() and (candidate / "bin").is_dir():
                found_rootfs = candidate
                break

    if found_rootfs is None:
        return "failed", (
            f"EMBA ran (exit code {result.returncode}) but no directory "
            f"matching rootfs markers (etc/ + bin/) was found under "
            f"{binwalk_root}. stderr: {result.stderr[-500:]}"
        )

    rootfs_dir = artifacts_path / "unpack" / "rootfs"
    if rootfs_dir.exists():
        shutil.rmtree(rootfs_dir)
    shutil.copytree(found_rootfs, rootfs_dir)

    file_count = sum(1 for _ in rootfs_dir.rglob("*") if _.is_file())

    return "completed", (
        f"EMBA full scan completed, extracted rootfs found at "
        f"{found_rootfs.relative_to(log_dir)} with {file_count} files "
        f"copied to unpack/rootfs/. Full analysis artifacts (SBOM, CVE "
        f"matches, per-binary reports) remain in {log_dir} for reference."
    )


def run_emulate(request, event_writer):
    """default-scan.emba is static-analysis only. EMBA does have dynamic/
    emulation capability (S115_usermode_emulator, and a separate
    default-scan-emulation.emba profile), but that ran as PART of
    run_unpack's single full-scan invocation above — there is no separate
    boot/emulate stage to trigger independently in this adapter version."""
    return "not_applicable", (
        "This adapter (v0.1.0) uses EMBA's default-scan.emba profile, which "
        "is static-analysis only. EMBA's own S115_usermode_emulator module "
        "does perform limited per-binary emulation, but as part of the "
        "single full-scan run in run_unpack, not as an independently "
        "triggerable stage."
    )


def run_endpoint_discovery(request, event_writer):
    """EMBA's default-scan.emba profile does not boot the firmware into a
    running state, so it cannot claim any live network endpoints —
    S75_network_check inspects config files for network references
    statically, it does not verify anything is actually reachable."""
    return "not_applicable", (
        "EMBA's static scan profile does not boot the firmware, so it "
        "cannot claim live endpoints. S75_network_check reports network "
        "configuration found in files, not verified running services."
    )


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

    current_state_holder = {"state": "starting"}  # one of: starting, running, waiting_for_shutdown, shutting_down (schema-enforced enum)
    heartbeat = HeartbeatWorker(
        event_writer,
        interval_seconds=request.lifecycle.heartbeat_interval_seconds,
        state_provider=lambda: current_state_holder["state"],
    )

    shutdown = ShutdownCoordinator(control_dir=request.paths.control)
    shutdown.start_polling()

    try:
        event_writer.adapter_started(message="Adapter starting")
        heartbeat.start()

        for stage in request.run.requested_stages:
            if shutdown.shutdown_requested():
                break  # coordinator asked us to stop early

            current_state_holder["state"] = "running"
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
                import inspect
                fn_params = inspect.signature(fn).parameters
                if "shutdown" in fn_params:
                    stage_outcome, message = fn(request, event_writer, shutdown=shutdown)
                else:
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
        current_state_holder["state"] = "waiting_for_shutdown"
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
