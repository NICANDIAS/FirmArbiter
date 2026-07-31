# Open investigation: graceful shutdown for long-blocking-call adapters

## Finding (2026-07-31, real coordinator-driven run)
EMBA and FIRMADYNE both fail their controlled-shutdown sequence when the
coordinator sends SIGTERM after a stage completes/is requested to stop.
FirmAE's entrypoint.py handles the identical SIGTERM cleanly (verified:
full adapter_started -> ... -> shutdown_started -> cleanup_complete ->
adapter_stopped sequence, status=completed).

## Evidence
- EMBA: SIGTERM sent almost immediately after adapter_started + one
  heartbeat (00:00:01 elapsed). Only 2 events in events.jsonl, no
  stderr output at all — looks like the container was force-stopped
  from outside rather than crashing.
- FIRMADYNE: unpack genuinely succeeded (real verified rootfs), but
  "received signal 15" in stderr and status=shutdown_failed — signal
  received, but the shutdown event sequence did not complete in time.

## Working hypothesis (NOT YET CONFIRMED — needs real investigation)
EMBA's run_unpack() runs the ENTIRE default-scan.emba profile as one
single, long, blocking subprocess.run() call (confirmed: real runtime
~13h). If SIGTERM arrives while this call is still blocking, Python's
registered signal handler (see lifecycle/signals.py's
ShutdownCoordinator) may not be able to act on it — control doesn't
return to the point where shutdown_started/cleanup_complete/
adapter_stopped can be emitted until the blocking call itself returns,
which for a multi-hour scan is far later than the coordinator will wait.
The coordinator likely then force-kills the container, never seeing a
graceful shutdown.

FIRMADYNE's failure may be a related but distinct instance of the same
underlying pattern — needs separate investigation of FIRMADYNE's own
entrypoint.py signal handling, since it's a different (older,
hand-rolled) implementation, not the shared _template lifecycle code.

## What needs investigation next session
1. Confirm the hypothesis: does EMBA's subprocess.run() call block the
   main thread from responding to SIGTERM? (Test: send SIGTERM to a
   running EMBA container manually mid-scan, observe whether ANY
   shutdown events get emitted, with what delay.)
2. If confirmed, the real fix likely requires either:
   a. Running EMBA's scan in a subprocess/thread that can be
      interrupted/killed directly on SIGTERM, with the main thread free
      to immediately emit the shutdown event sequence, OR
   b. Increasing whatever shutdown-grace-period the coordinator allows
      before force-killing, specifically for long-running adapters
      (may need a per-adapter or per-manifest configurable grace period
      — check firmarbiter_core/run_coordinator.py for
      --shutdown-grace and whether it's actually being respected)
   c. Redesigning EMBA's run_unpack() to poll/check a shutdown flag
      periodically rather than blocking uninterruptibly for hours
3. Check FIRMADYNE's entrypoint.py signal handling separately, since
   it's a different code path from EMBA/fact_extractor/_template.

## Why this matters
Every long-running adapter (any real firmware analysis tool doing
genuine multi-hour static/dynamic analysis, not just EMBA) will likely
hit this same problem. This is a real SDK-level gap, not specific to
one candidate.
